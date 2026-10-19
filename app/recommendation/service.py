"""Полный конвейер: намерение → фильтры → баллы → проверенные ID → ответ из SQLite."""

import logging
from typing import Literal, TypedDict

from pydantic import ValidationError

from app.catalog.models import Wine
from app.catalog.repository import WineRepository
from app.llm.base import LLMError, TextLLM
from app.llm.schemas import ExplanationCandidate, ExplanationResponse, GroundedReason
from app.recommendation.fallback import clarification, merge_intents, parse_fallback
from app.recommendation.food import extract_dish_phrase, validated_food_categories
from app.recommendation.ranking import (
    has_meaningful_relevance,
    matches_constraints,
    rank_wines,
    score_wine,
)
from app.recommendation.schemas import (
    Recommendation,
    RecommendationRequest,
    RecommendationResponse,
    UserIntent,
)
from app.recommendation.taste import reconcile_taste_signals
from app.recommendation.vocabulary import CatalogVocabulary

logger = logging.getLogger(__name__)
BUDGET_UNAVAILABLE = "В каталоге нет подтверждённых цен. Учтены остальные пожелания, но соответствие вашему бюджету гарантировать нельзя."
LLM_FALLBACK = "Внешняя модель недоступна или её ответ не прошёл проверку. Используется ограниченный локальный разбор."


class ResponseContext(TypedDict):
    """Общие проверенные поля всех ветвей ответа."""

    intent: UserIntent
    budget_status: Literal["not_requested", "applied", "budget_constraint_unavailable"]
    warnings: list[str]
    extraction_source: Literal["llm", "fallback"]


class RecommendationService:
    """LLM не выбирает вина: вход и объяснения модели считаются недоверенными."""

    def __init__(self, repository: WineRepository, llm: TextLLM | None = None):
        self.repository = repository
        self.llm = llm

    def _catalog(self) -> list[Wine]:
        """Прочитать все страницы локального снимка без изменения каталожного слоя."""
        result = []
        for offset in range(0, self.repository.count(), 500):
            page = self.repository.list_wines(limit=500, offset=offset)
            result.extend(page)
        return result

    def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        """Извлечь намерение или перейти к ограниченному русскому словарю."""
        wines = self._catalog()
        vocabulary = CatalogVocabulary.from_wines(wines)
        fallback = parse_fallback(request.message, vocabulary)
        latest = fallback.intent
        source: Literal["llm", "fallback"] = "fallback"
        warnings = []
        use_llm_explanation = self.llm is not None
        if self.llm is not None:
            try:
                extracted = self.llm.extract_preferences(request.message, list(vocabulary.foods))
                latest = UserIntent.model_validate(extracted.model_dump())
                source = "llm"
                # Буквально распознанное требование пользователя сильнее предположений модели.
                if not fallback.ambiguous:
                    anchors = fallback.intent.model_copy(deep=True)
                    anchors.missing_information = latest.missing_information
                    # Резервные совпадения не должны сокращать блюдо или навязывать категории LLM.
                    anchors.dish = None
                    anchors.normalized_food_categories = []
                    anchors.taste_preferences = []
                    anchors.negative_taste_preferences = []
                    latest = merge_intents(latest, anchors)
            except LLMError, ValidationError:
                logger.warning("Извлечение предпочтений LLM недоступно или не прошло проверку.")
                warnings.append(LLM_FALLBACK)
                use_llm_explanation = False
        else:
            warnings.append("Используется ограниченный локальный разбор без внешней модели.")
        latest.dish = latest.dish or extract_dish_phrase(request.message)
        latest = reconcile_taste_signals(request.message, latest)
        intent = merge_intents(request.previous_intent, latest)
        return self.recommend_intent(
            intent,
            limit=request.limit,
            message=request.message,
            wines=wines,
            extraction_source=source,
            warnings=warnings,
            use_llm_explanation=use_llm_explanation,
        )

    def recommend_intent(
        self,
        intent: UserIntent,
        *,
        limit: int = 3,
        message: str = "",
        wines: list[Wine] | None = None,
        extraction_source: Literal["llm", "fallback"] = "fallback",
        warnings: list[str] | None = None,
        use_llm_explanation: bool = True,
    ) -> RecommendationResponse:
        """Тот же движок доступен для уже структурированных предпочтений без LLM."""
        if not 1 <= limit <= 5:
            raise ValueError("Число рекомендаций должно быть от 1 до 5.")
        intent = UserIntent.model_validate(intent.model_dump())
        wines = self._catalog() if wines is None else wines
        warnings = list(warnings or [])
        budget_status: Literal["not_requested", "applied", "budget_constraint_unavailable"] = (
            "not_requested"
        )
        if intent.budget_max_rub is not None:
            if any(w.price_rub is not None for w in wines):
                budget_status = "applied"
            else:
                budget_status = "budget_constraint_unavailable"
                warnings.append(BUDGET_UNAVAILABLE)
        common: ResponseContext = {
            "intent": intent,
            "budget_status": budget_status,
            "warnings": warnings,
            "extraction_source": extraction_source,
        }
        if not wines:
            return RecommendationResponse(
                status="empty_catalog",
                message="Локальный каталог пуст. Сначала выполните синхронизацию каталога.",
                **common,
            )
        valid_foods = validated_food_categories(
            intent.dish,
            intent.normalized_food_categories,
            (v for w in wines for v in w.food_pairings),
        )
        if len(valid_foods) != len(intent.normalized_food_categories) or (
            intent.dish and not valid_foods
        ):
            warnings.append(
                "Не все гастрономические пожелания удалось сопоставить с реальными категориями каталога; неподтверждённые категории не учитывались."
            )
        intent.normalized_food_categories = list(dict.fromkeys(valid_foods))
        if "unsupported_request" in intent.missing_information:
            intent.needs_clarification = True
            return RecommendationResponse(
                status="uninterpretable",
                message="Не удалось надёжно разобрать запрос. Укажите проще цвет, сладость, блюдо, сорт или регион; например: «красное сухое к стейку».",
                **common,
            )

        candidates = [wine for wine in wines if matches_constraints(wine, intent.hard_constraints)]
        if budget_status == "applied":
            assert intent.budget_max_rub is not None
            candidates = [
                wine
                for wine in candidates
                if wine.price_rub is not None and wine.price_rub <= intent.budget_max_rub
            ]
        if not candidates:
            return RecommendationResponse(
                status="no_candidates",
                message="В локальном каталоге нет вин, удовлетворяющих всем обязательным ограничениям. Попробуйте изменить одно из требований.",
                **common,
            )
        eligible = [
            candidate
            for candidate in rank_wines(candidates, intent)
            if has_meaningful_relevance(candidate)
        ]
        if not eligible:
            return RecommendationResponse(
                status="no_candidates",
                message="В локальном каталоге нет вин с подтверждённым совпадением с вашими пожеланиями. Попробуйте уточнить блюдо или предпочтения.",
                **common,
            )
        if question := clarification(intent):
            intent.needs_clarification = True
            intent.missing_information = [question[0]]
            return RecommendationResponse(status="clarification", message=question[1], **common)
        intent.needs_clarification = False
        intent.missing_information = []
        selected = eligible[:limit]
        # Повторное чтение обязательно: ни факты, ни итоговые ID не берутся из ответа модели.
        recommendations = []
        for candidate in selected:
            wine = self.repository.get(candidate.wine_id)
            if wine is None or not matches_constraints(wine, intent.hard_constraints):
                continue
            if budget_status == "applied":
                assert intent.budget_max_rub is not None
                if wine.price_rub is None or wine.price_rub > intent.budget_max_rub:
                    continue
            ranked = score_wine(wine, intent)
            if not has_meaningful_relevance(ranked):
                continue
            reasons = [part.reason for part in ranked.breakdown]
            recommendations.append(
                Recommendation(
                    wine_id=wine.id,
                    wine=wine,
                    score=ranked.score,
                    breakdown=ranked.breakdown,
                    reasons=reasons,
                    explanation=" ".join(reasons),
                    budget_satisfied=True if budget_status == "applied" else None,
                )
            )
        if not recommendations:
            return RecommendationResponse(
                status="no_candidates",
                message="Каталог изменился во время подбора. Повторите запрос.",
                **common,
            )
        recommendations.sort(key=lambda item: (-item.score, item.wine_id))
        if use_llm_explanation:
            self._explain(message, recommendations, warnings)
        if intent.taste_preferences and not any(
            part.key.startswith("taste:") for item in recommendations for part in item.breakdown
        ):
            warnings.append(
                "В описаниях выбранных вин не найдено подтверждений указанным вкусовым пожеланиям."
            )
        return RecommendationResponse(
            status="ok",
            message="Подобраны реальные вина из локального каталога.",
            recommendations=recommendations,
            **common,
        )

    def _explain(
        self, message: str, recommendations: list[Recommendation], warnings: list[str]
    ) -> None:
        """Принять только известные ID и причины; произвольный текст модели не отображать."""
        if self.llm is None:
            return
        candidates = [
            ExplanationCandidate(
                wine_id=item.wine_id,
                reasons=[
                    GroundedReason(reason_id=part.key, text=part.reason) for part in item.breakdown
                ],
            )
            for item in recommendations
        ]
        try:
            response = self.llm.explain_recommendations(message, candidates)
            response = ExplanationResponse.model_validate(response.model_dump())
        except LLMError, ValidationError:
            logger.warning("Объяснение LLM недоступно или не прошло проверку.")
            warnings.append("Использовано объяснение по данным каталога без помощи внешней модели.")
            return
        allowed = {item.wine_id: item for item in recommendations}
        seen: set[int] = set()
        for selection in response.explanations:
            if selection.wine_id not in allowed or selection.wine_id in seen:
                logger.warning(
                    "Отклонён неизвестный или повторный Wine ID в объяснении LLM: %s.",
                    selection.wine_id,
                )
                continue
            seen.add(selection.wine_id)
            item = allowed[selection.wine_id]
            reasons = {part.key: part.reason for part in item.breakdown}
            if any(key not in reasons for key in selection.reason_ids):
                logger.warning("Отклонены неподтверждённые причины объяснения LLM.")
                continue
            relevant = {
                key
                for key in reasons
                if key.startswith(("food:", "taste:", "soft:")) or key == "constraints"
            }
            if relevant and not relevant.intersection(selection.reason_ids):
                logger.warning(
                    "Объяснение LLM не учитывает пожелания; сохранено исходное обоснование."
                )
                continue
            # Модель не может скрыть штраф или неопределённость по нежелательному свойству.
            required = [key for key in reasons if key.startswith("negative_taste:")]
            item.reasons = [
                reasons[key] for key in dict.fromkeys([*selection.reason_ids, *required])
            ]
            item.explanation = " ".join(item.reasons)
