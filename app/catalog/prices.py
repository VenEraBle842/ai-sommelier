"""Отдельная граница для будущего проверенного источника цен."""

from decimal import Decimal
from typing import Protocol

from app.catalog.models import WineData


class PriceProvider(Protocol):
    """Источник подтверждённой цены в рублях; отсутствие цены означает None."""

    def get_price(self, wine: WineData) -> Decimal | None:
        """Получить подтверждённую цену именно этой позиции без догадок."""
        ...


class NoPriceProvider:
    """Без доверенного источника цена всегда отсутствует."""

    def get_price(self, wine: WineData) -> None:
        """Не извлекать цену из описания, рейтинга или постороннего текста."""
        return None
