"""Детерминированная подмена модели для тестов без внешних запросов."""

from pydantic import ValidationError

from app.llm.base import LLMResponseError
from app.llm.schemas import ExplanationCandidate, ExplanationResponse
from app.recommendation.schemas import UserIntent


class FakeLLM:
    """Поддерживает проверенные объекты, сырой JSON и заранее заданные ошибки."""

    def __init__(
        self,
        intent: UserIntent | str | Exception,
        explanations: ExplanationResponse | str | Exception | None = None,
    ):
        self.intent = intent
        self.explanations = explanations
        self.extraction_calls: list[tuple[str, list[str]]] = []
        self.explanation_calls: list[list[ExplanationCandidate]] = []

    def close(self) -> None:
        """У подмены нет сетевых ресурсов."""

    def extract_preferences(self, message: str, food_categories: list[str]) -> UserIntent:
        """Имитировать проверку JSON и ошибку внешнего сервиса."""
        self.extraction_calls.append((message, food_categories))
        if isinstance(self.intent, Exception):
            raise self.intent
        try:
            return (
                UserIntent.model_validate_json(self.intent)
                if isinstance(self.intent, str)
                else UserIntent.model_validate(self.intent.model_dump())
            )
        except ValidationError:
            raise LLMResponseError("Учебный ответ модели не прошёл проверку.") from None

    def explain_recommendations(
        self, message: str, candidates: list[ExplanationCandidate]
    ) -> ExplanationResponse:
        """По умолчанию вернуть все переданные причины без изменения позиций."""
        self.explanation_calls.append(candidates)
        if isinstance(self.explanations, Exception):
            raise self.explanations
        if isinstance(self.explanations, str):
            try:
                return ExplanationResponse.model_validate_json(self.explanations)
            except ValidationError:
                raise LLMResponseError("Учебное объяснение не прошло проверку.") from None
        if self.explanations is not None:
            return ExplanationResponse.model_validate(self.explanations.model_dump())
        return ExplanationResponse.model_validate(
            {
                "explanations": [
                    {"wine_id": c.wine_id, "reason_ids": [r.reason_id for r in c.reasons]}
                    for c in candidates
                ]
            }
        )
