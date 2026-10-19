"""Ограниченный русский словарь на случай отсутствия или ошибки внешней модели."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.recommendation.schemas import UserIntent
from app.recommendation.vocabulary import TASTE_PATTERNS, CatalogVocabulary, normalize

FOOD_ALIASES = (
    (
        r"\b(?:стейк\w*|рибай|мяс[оуам]|мясом)\b",
        ("Мясо и стейки", "Мясное ассорти", "Блюда из мяса"),
    ),
    (r"\b(?:рыб\w*|лосос\w*|семг\w*|морепродукт\w*)\b", ("Рыба и морепродукты", "Блюда из рыбы")),
    (r"\b(?:сыр\w*)\b", ("Сыры",)),
    (r"\b(?:десерт\w*)\b", ("Десерты", "Выпечка и десерты")),
    (r"\b(?:куриц\w*|птиц\w*)\b", ("Блюда из птицы",)),
)
REGION_FORMS = {"Кубань": ("Кубань", "Кубани"), "Крым": ("Крым", "Крыма", "Крыму")}


@dataclass(frozen=True)
class FallbackResult:
    """Нераспознанная часть запроса запрещает делать вид, что всё понято."""

    intent: UserIntent
    supported: bool
    ambiguous: bool = False


def parse_fallback(message: str, vocabulary: CatalogVocabulary) -> FallbackResult:
    """Поддерживать небольшой словарь, не угадывая оставшиеся важные слова."""
    original = normalize(message)
    remaining = original
    hard: dict = {}
    soft: dict = {}
    tastes: list[str] = []
    foods: list[str] = []
    dish = None
    ambiguous = False
    budget = None
    defaults = bool(
        re.search(r"на (?:ваш|твой) вкус|(?:подбери|выбери|подберите|выберите) сам[иа]?", original)
    )
    remaining = re.sub(
        r"на (?:ваш|твой) вкус|(?:подбери|выбери|подберите|выберите) сам[иа]?", " ", remaining
    )

    budget_pattern = r"\b(?:до|не дороже|максимум)\s+(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:руб(?:лей|ля|ль|\.)?|₽|р\.)?"
    for budget_match in re.finditer(budget_pattern, remaining):
        amount = Decimal(re.sub(r"\s", "", budget_match[1]).replace(",", "."))
        if amount <= 0 or amount > 100000000 or (budget is not None and budget != amount):
            ambiguous = True
        else:
            budget = amount
    remaining = re.sub(budget_pattern, " ", remaining)
    if re.search(r"\b(?:без|кроме|исключи\w*|не)\b", remaining):
        return FallbackResult(
            UserIntent(missing_information=["unsupported_request"], needs_clarification=True),
            False,
            True,
        )

    def target(start: int) -> dict:
        """Явные слова предпочтения понижают требование до мягкого пожелания."""
        prefix = original[max(0, start - 40) : start]
        if re.search(
            r"(?:желательно|предпочитаю|по возможности|люблю)[^.;]*$", prefix
        ) and not re.search(r"(?:только|обязательно)[^.;]*$", prefix):
            return soft
        return hard

    for field, patterns in (
        (
            "color",
            {
                "красное": r"\bкрасн(?:ое|ого|ому|ым)\b",
                "белое": r"\bбел(?:ое|ого|ому|ым)\b",
                "розовое": r"\bрозов(?:ое|ого|ому|ым)\b",
                "оранжевое": r"\bоранжев(?:ое|ого|ому|ым)\b",
            },
        ),
        (
            "sweetness",
            {
                "экстра брют": r"\bэкстра[ -]брют\b",
                "полусладкое": r"\bполусладк(?:ое|ого|ому|им)\b",
                "полусухое": r"\bполусух(?:ое|ого|ому|им)\b",
                "сладкое": r"\bсладк(?:ое|ого|ому|им)\b",
                "сухое": r"\bсух(?:ое|ого|ому|им)\b",
                "брют": r"\bбрют\b",
            },
        ),
    ):
        found = []
        for canonical, pattern in patterns.items():
            if re.search(pattern, remaining):
                found.append(canonical)
                original_match = re.search(pattern, original)
                target(original_match.start() if original_match else 0)[field] = canonical
                remaining = re.sub(pattern, " ", remaining)
        if len(found) > 1:
            ambiguous = True

    for field, values in (
        ("grapes", vocabulary.grapes),
        ("regions", vocabulary.regions),
        ("producer", vocabulary.producers),
    ):
        for value in sorted(values, key=lambda v: (-len(v), v)):
            forms = REGION_FORMS.get(value, (value,)) if field == "regions" else (value,)
            pattern = (
                r"(?<!\w)(?:" + "|".join(re.escape(normalize(form)) for form in forms) + r")(?!\w)"
            )
            if re.search(pattern, remaining):
                original_match = re.search(pattern, original)
                destination = target(original_match.start() if original_match else 0)
                if field == "producer":
                    if field in destination:
                        ambiguous = True
                    destination[field] = value
                else:
                    destination.setdefault(field, []).append(value)
                remaining = re.sub(pattern, " ", remaining)

    actual_foods = {normalize(value): value for value in vocabulary.foods}
    for value in sorted(vocabulary.foods, key=lambda v: (-len(v), v)):
        pattern = r"(?<!\w)" + re.escape(normalize(value)) + r"(?!\w)"
        if match := re.search(pattern, remaining):
            dish = dish or match[0]
            foods.append(value)
            remaining = re.sub(pattern, " ", remaining)
    for pattern, category_options in FOOD_ALIASES:
        if match := re.search(pattern, remaining):
            dish = dish or match[0]
            foods.extend(
                actual_foods[normalize(value)]
                for value in category_options
                if normalize(value) in actual_foods
            )
            remaining = re.sub(pattern, " ", remaining)
    for taste, pattern in TASTE_PATTERNS.items():
        if re.search(pattern, remaining):
            tastes.append(taste)
            remaining = re.sub(pattern, " ", remaining)

    remaining = re.sub(
        r"\b(?:хочу|хочется|вино|вина|виноград|сорт|сорта|регион|региона|производитель|к|с|со|из|и|или|для|на|ужин|подбери|подберите|посоветуй|посоветуйте|порекомендуй|порекомендуйте|выбери|выберите|пожалуйста|желательно|предпочитаю|по|возможности|люблю|только|обязательно|ноты|нотами|ароматом|со|тогда|еще|лучше)\b",
        " ",
        remaining,
    )
    unsupported = ambiguous or bool(re.sub(r"[\s.,!?:;\-–—«»\"()]", "", remaining))
    recognized = any((hard, soft, tastes, dish, budget, defaults))
    intent = UserIntent.model_validate(
        {
            "hard_constraints": hard,
            "soft_preferences": soft,
            "taste_preferences": tastes,
            "dish": dish,
            "normalized_food_categories": list(dict.fromkeys(foods)),
            "budget_max_rub": budget,
            "allow_defaults": defaults,
            "missing_information": ["unsupported_request"] if unsupported or not recognized else [],
            "needs_clarification": unsupported or not recognized,
        }
    )
    if re.search(r"\bили\b", original) and len(hard.get("grapes", [])) > 1:
        intent.missing_information = ["unsupported_request"]
        intent.needs_clarification = True
        return FallbackResult(intent, False, True)
    return FallbackResult(intent, recognized and not unsupported, ambiguous)


def merge_intents(previous: UserIntent | None, latest: UserIntent) -> UserIntent:
    """Непустые новые значения заменяют старые; неповторённые ограничения сохраняются."""
    if previous is None:
        return latest.model_copy(deep=True)
    values = previous.model_dump()
    for field in ("hard_constraints", "soft_preferences"):
        for key, value in getattr(latest, field).model_dump().items():
            if value:
                values[field][key] = value
    for key in ("dish", "taste_preferences", "budget_max_rub"):
        if value := getattr(latest, key):
            values[key] = value
    if latest.dish or latest.normalized_food_categories:
        values["normalized_food_categories"] = latest.normalized_food_categories
    values.update(
        missing_information=latest.missing_information,
        needs_clarification=False,
        allow_defaults=latest.allow_defaults or previous.allow_defaults,
    )
    return UserIntent.model_validate(values)


def clarification(intent: UserIntent) -> tuple[Literal["color", "sweetness"], str] | None:
    """Задать один полезный вопрос; не доверять решению модели о необходимости анкеты."""
    hard, soft = intent.hard_constraints, intent.soft_preferences
    if intent.allow_defaults or hard.grapes or hard.producer or soft.grapes or soft.producer:
        return None
    axes = sum(
        bool(value)
        for value in (
            hard.color or soft.color,
            hard.sweetness or soft.sweetness or hard.source_categories or soft.source_categories,
            hard.regions or soft.regions,
            intent.normalized_food_categories,
            intent.taste_preferences,
        )
    )
    if axes >= 2:
        return None
    if hard.color or soft.color:
        return "sweetness", "Предпочитаете сухое, полусухое или сладкое вино?"
    return "color", "Какое вино предпочитаете: красное, белое или розовое?"
