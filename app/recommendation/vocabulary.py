"""Словарь из текущего каталога и прозрачные признаки из описаний."""

import re
from dataclasses import dataclass

from app.catalog.models import Wine


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


TASTE_PATTERNS = {
    "ягодное": r"\b(?:ягод\w*|вишн\w*|клюкв\w*|малин\w*|ежевик\w*|смородин\w*)\b",
    "фруктовое": r"\b(?:фрукт\w*|слив[аыуе]|яблок\w*|груш\w*|персик\w*|абрикос\w*)\b",
    "цветочное": r"\b(?:цветоч\w*|цветов|акаци\w*|жасмин\w*)\b",
    "цитрусовое": r"\b(?:цитрус\w*|лимон\w*|апельсин\w*|грейпфрут\w*)\b",
    "ванильное": r"\bванил\w*\b",
    "дубовое": r"\b(?:дуб[а-я]*|дубов\w*)\b",
    "пряное": r"\b(?:прян\w*|специ[йяи]|перечн\w*|кориц\w*)\b",
    "минеральное": r"\bминеральн\w*\b",
}


def taste_evidence(description: str | None, taste: str) -> str | None:
    """Совпадение означает упоминание в источнике, а не новую характеристику вина."""
    if not description or taste not in TASTE_PATTERNS:
        return None
    for match in re.finditer(TASTE_PATTERNS[taste], description, re.IGNORECASE):
        before = description[max(0, match.start() - 45) : match.start()]
        if re.search(r"\b(?:без|нет|не|отсутств\w*)\b(?:\s+\w+){0,3}\s*$", before, re.IGNORECASE):
            continue
        # Цитата берётся буквально: не приписываем всему вину упомянутую технологию или аромат.
        start = description.rfind(" ", max(0, match.start() - 35), match.start()) + 1
        end = description.find(" ", match.end() + 35)
        return description[start : end if end >= 0 else len(description)].strip()
    return None
