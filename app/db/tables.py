"""Таблицы SQLite; схема не зависит от HTML-парсера."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Integer, Numeric, String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Вернуть часовой пояс UTC, который SQLite не сохраняет в DateTime."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Преобразовать время в UTC перед сохранением."""
        return value.astimezone(UTC).replace(tzinfo=None) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Восстановить UTC при чтении."""
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    """Общая метаинформация таблиц."""


class WineRecord(Base):
    """Одна запись на канонический адрес реальной карточки."""

    __tablename__ = "wines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    producer: Mapped[str | None] = mapped_column(String)
    region: Mapped[str | None] = mapped_column(String)
    grapes: Mapped[list[str]] = mapped_column(JSON)
    color: Mapped[str | None] = mapped_column(String)
    sweetness: Mapped[str | None] = mapped_column(String)
    source_category: Mapped[str | None] = mapped_column(String)
    source_color: Mapped[str | None] = mapped_column(String)
    serving_temperature: Mapped[str | None] = mapped_column(String)
    alcohol: Mapped[str | None] = mapped_column(String)
    food_pairings: Mapped[list[str]] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    source_url: Mapped[str] = mapped_column(String, unique=True)
    image_url: Mapped[str | None] = mapped_column(Text)
    price_rub: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    scraped_at: Mapped[datetime] = mapped_column(UTCDateTime())
