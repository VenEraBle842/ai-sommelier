"""Исходная фраза о блюде и консервативная проверка категорий без угадывания."""

import re
from collections.abc import Iterable

from app.recommendation.vocabulary import normalize

# Основной ингредиент отличается от способа подачи и соуса: ягоды не превращают птицу в десерт.
FOOD_FAMILIES = (
    (
        r"\b(?:утк\w*|утин\w*|индейк\w*|индюш\w*|куриц\w*|курин\w*|птиц\w*|гус(?:ь|я|ю|ем|е|и|ей)|гусин\w*)\b",
        r"\b(?:птиц\w*|утк\w*|утин\w*|куриц\w*|курин\w*|индейк\w*|индюш\w*|гус(?:ь|я|ю|ем|е|и|ей)|гусин\w*)\b",
    ),
    (
        r"\b(?:рыб\w*|лосос\w*|семг\w*|форел\w*|треск\w*|судак\w*|тун[еца]\w*|морепродукт\w*|кревет\w*)\b",
        r"\b(?:рыб\w*|морепродукт\w*|лосос\w*)\b",
    ),
    (
        r"\b(?:говядин\w*|баранин\w*|свинин\w*|телятин\w*|стейк\w*|рибай|мяс\w*)\b",
        r"\b(?:мяс\w*|стейк\w*|говядин\w*|баранин\w*|свинин\w*)\b",
    ),
    (r"\b(?:сыр(?:ы|а|у|ом|е)?|сырн\w*)\b", r"\bсыр\w*\b"),
    (r"\b(?:десерт\w*|торт\w*|пирожн\w*)\b", r"\b(?:десерт\w*|выпечк\w*)\b"),
)


def extract_dish_phrase(message: str) -> str | None:
    """Сохранить дословную фразу после к/под/для вместе с гарниром и соусом."""
    match = re.search(r"\b(?:к|под|для)\s+([^.!?;\n]+)", message, re.IGNORECASE)
    if match is None:
        return None
    # Отделяем следующий запрос о вине и бюджет, не разбивая перечисление ингредиентов.
    phrase = re.split(
        r",?\s+(?:(?:хочу|хочется|предпочитаю|нужно|ищу|бюджет|желательно|но)\b|(?:до|не дороже)\s+\d)|\s*[,—–]\s*(?=(?:\w+\s+){0,3}вин[оа]\b)",
        match[1],
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(' ,:«»"—–-')
    return phrase if phrase and len(phrase) <= 160 else None


def validated_food_categories(
    dish: str | None, categories: list[str], available: Iterable[str]
) -> list[str]:
    """Не добавлять категории: оставить только существующие и явно совместимые с блюдом."""
    actual = {normalize(value): value for value in available}
    allowed = [actual[normalize(value)] for value in categories if normalize(value) in actual]
    if dish:
        matches = [
            (match.start(), index, category_pattern)
            for index, (ingredient_pattern, category_pattern) in enumerate(FOOD_FAMILIES)
            if (match := re.search(ingredient_pattern, normalize(dish)))
        ]
        if matches:
            # Явная птица или рыба важнее общего слова «стейк»/«мясо» перед названием ингредиента.
            proteins = [item for item in matches if item[1] < 2]
            _, family, _ = min(proteins or matches)
            compatible = []
            for value in allowed:
                category_families = {
                    index
                    for index, (_, category_pattern) in enumerate(FOOD_FAMILIES)
                    if re.search(category_pattern, normalize(value))
                }
                # Категории кухни/способа приготовления не подменяем грубой классификацией белка.
                if not category_families or family in category_families:
                    compatible.append(value)
            allowed = compatible
    return list(dict.fromkeys(allowed))
