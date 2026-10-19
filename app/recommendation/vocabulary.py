"""Словарь из текущего каталога и прозрачные признаки из описаний."""

import re
from dataclasses import dataclass

from app.catalog.models import Wine
from app.recommendation.schemas import NegativeTaste, Taste


def normalize(value: str) -> str:
    """Сравнивать значения без различий регистра, ё и повторных пробелов."""
    return " ".join(value.casefold().replace("ё", "е").split())


@dataclass(frozen=True)
class CatalogVocabulary:
    """Только существующие значения; в LLM отправляются лишь гастрономические категории."""

    foods: tuple[str, ...]
    grapes: tuple[str, ...]
    regions: tuple[str, ...]
    producers: tuple[str, ...]
    categories: tuple[str, ...]

    @classmethod
    def from_wines(cls, wines: list[Wine]) -> CatalogVocabulary:
        """Построить устойчивый словарь без обращения к сайту."""
        return cls(
            foods=tuple(sorted({v for w in wines for v in w.food_pairings})),
            grapes=tuple(sorted({v for w in wines for v in w.grapes})),
            regions=tuple(sorted({w.region for w in wines if w.region})),
            producers=tuple(sorted({w.producer for w in wines if w.producer})),
            categories=tuple(sorted({w.source_category for w in wines if w.source_category})),
        )


# Насыщенность подтверждается вкусом или телом, но не силой аромата, цветом или танинами.
# Прилагательные привязаны к своему свойству: «тело среднее» не означает полнотелость.
RICH_TASTE_PATTERN = (
    r"(?:насыщ[её]нн|интенсивн|плотн)(?:ый|ого|ому|ым|ом)\s+вкус(?:а|у|ом|е)?"
    r"|вкус(?:а|у|ом|е)?\s+(?:насыщ[её]нн|интенсивн|плотн)(?:ый|ого|ому|ым|ом)"
)
FULL_BODY_PATTERN = (
    r"полнотел(?:ое|ого|ому|ым|ость|ости)"
    r"|(?:полн(?:ое|ым)|плотн(?:ое|ым))\s+тел(?:о|ом)"
    r"|тел(?:о|ом)\s+(?:полн(?:ое|ым)|плотн(?:ое|ым))"
    r"|плотн(?:ое|ого|ому|ым)\s+вин(?:о|а|у|ом)"
    r"|вин(?:о|а|у|ом)\s+плотн(?:ое|ого|ому|ым)"
)

TASTE_PATTERNS: dict[Taste, str] = {
    "ягодное": r"\b(?:ягод\w*|вишн\w*|клюкв\w*|малин\w*|ежевик\w*|смородин\w*)\b",
    "фруктовое": r"\b(?:фрукт\w*|слив[аыуе]|яблок\w*|груш\w*|персик\w*|абрикос\w*)\b",
    "цветочное": r"\b(?:цветоч\w*|цветов|акаци\w*|жасмин\w*)\b",
    "цитрусовое": r"\b(?:цитрус\w*|лимон\w*|апельсин\w*|грейпфрут\w*)\b",
    "ванильное": r"\bванил\w*\b",
    "дубовое": r"\b(?:дуб[а-я]*|дубов\w*)\b",
    "пряное": r"\b(?:прян\w*|специ[йяи]|перечн\w*|кориц\w*)\b",
    "минеральное": r"\bминеральн\w*\b",
    "насыщенное": rf"\b(?:{RICH_TASTE_PATTERN}|{FULL_BODY_PATTERN})\b",
}

# Пользовательские формулировки отделены от признаков источника: ингредиент не является пожеланием.
TASTE_REQUEST_PATTERNS: dict[Taste, str] = {
    "ягодное": r"\bягодн(?:ое|ого|ому|ым)\b",
    "фруктовое": r"\bфруктов(?:ое|ого|ому|ым)\b",
    "цветочное": r"\bцветочн(?:ое|ого|ому|ым)\b",
    "цитрусовое": r"\bцитрусов(?:ое|ого|ому|ым)\b",
    "ванильное": r"\bванильн(?:ое|ого|ому|ым)\b",
    "дубовое": r"\bдубов(?:ое|ого|ому|ым)\b",
    "пряное": r"\bпрян(?:ое|ого|ому|ым)\b",
    "минеральное": r"\bминеральн(?:ое|ого|ому|ым)\b",
    "насыщенное": r"\b(?:насыщ[её]нн|полнотел|плотн)(?:ое|ого|ому|ым)\b",
}
# Для отнесения слова к блюду форма прилагательного не ограничивается вином среднего рода.
TASTE_MENTION_PATTERNS: dict[Taste, str] = {
    **TASTE_PATTERNS,
    "насыщенное": r"\b(?:насыщ[её]нн\w*|полнотел\w*|плотн\w*|интенсивн\w*)\b",
}
ROUGH_TASTE_PATTERN = r"\b(?:(?:груб\w*|ж[её]стк\w*|резк\w*)\s+(?:терпк\w*|танин\w*)|(?:терпк\w*|танин\w*)\s+(?:груб\w*|ж[её]стк\w*|резк\w*))\b"
NEGATIVE_TASTE_PATTERNS: dict[NegativeTaste, str] = {"грубая терпкость": ROUGH_TASTE_PATTERN}
NEGATIVE_TASTE_REQUEST_PATTERN = (
    r"\b(?:без|избегаю|не\s+(?:хочу|люблю))\s+(?:(?:слишком|чрезмерно)\s+)?" + ROUGH_TASTE_PATTERN
)


def taste_evidence(description: str | None, taste: str) -> str | None:
    """Совпадение означает упоминание в источнике, а не новую характеристику вина."""
    pattern = next((pattern for key, pattern in TASTE_PATTERNS.items() if key == taste), None)
    return _source_evidence(description, pattern) if pattern else None


def negative_taste_evidence(description: str | None, taste: NegativeTaste) -> str | None:
    """Искать явное нежелательное свойство, не выдавая молчание источника за его отсутствие."""
    return _source_evidence(description, NEGATIVE_TASTE_PATTERNS[taste])


def _source_evidence(description: str | None, pattern: str) -> str | None:
    """Вернуть буквальную цитату с учётом простых отрицаний в источнике."""
    if not description:
        return None
    for match in re.finditer(pattern, description, re.IGNORECASE):
        before = description[max(0, match.start() - 45) : match.start()]
        if re.search(r"\b(?:без|нет|не|отсутств\w*)\b(?:\s+\w+){0,3}\s*$", before, re.IGNORECASE):
            continue
        after = description[match.end() : match.end() + 45]
        if re.match(
            r"\s+(?:отсутств\w*|не\s+(?:выражен\w*|ощуща\w*|характер\w*))\b", after, re.IGNORECASE
        ):
            continue
        # Цитата берётся буквально: не приписываем всему вину упомянутую технологию или аромат.
        start = description.rfind(" ", max(0, match.start() - 35), match.start()) + 1
        end = description.find(" ", match.end() + 35)
        return description[start : end if end >= 0 else len(description)].strip()
    return None
