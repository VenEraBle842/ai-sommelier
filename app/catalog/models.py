"""Нормализованные данные: отсутствующие сведения остаются пустыми."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.catalog.urls import wine_url


class WineData(BaseModel):
    """Данные карточки до назначения локального идентификатора."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)
    producer: str | None = None
    region: str | None = None
    grapes: list[str] = Field(default_factory=list)
    color: str | None = None
    sweetness: str | None = None
    source_category: str | None = None
    source_color: str | None = None
    serving_temperature: str | None = None
    alcohol: str | None = None
    food_pairings: list[str] = Field(default_factory=list)
    description: str | None = None
    rating: Annotated[Decimal, Field(ge=0, le=5)] | None = None
    source_url: str
    image_url: str | None = None
    price_rub: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("source_url")
    @classmethod
    def canonical_source(cls, value: str) -> str:
        """Использовать один адрес для всех вариантов ссылки на карточку."""
        return wine_url(value)

    @field_validator("scraped_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Хранить момент загрузки в UTC без неоднозначного местного времени."""
        if value.tzinfo is None:
            raise ValueError("Дата загрузки должна содержать часовой пояс.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def matching_slug(self) -> WineData:
        """Исключить несогласованные адрес и идентификатор источника."""
        if self.slug != self.source_url.rsplit("/", 1)[-1]:
            raise ValueError("Slug не совпадает с адресом карточки.")
        return self


class Wine(WineData):
    """Сохранённая карточка с постоянным локальным идентификатором."""

    id: int
