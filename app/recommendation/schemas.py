"""Проверяемые предпочтения, диалоговый контекст и результат подбора."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalog.models import Wine

Color = Literal["белое", "красное", "розовое", "оранжевое"]
Sweetness = Literal["сухое", "полусухое", "полусладкое", "сладкое", "брют", "экстра брют"]
Taste = Literal[
    "ягодное",
    "фруктовое",
    "цветочное",
    "цитрусовое",
    "ванильное",
    "дубовое",
    "пряное",
    "минеральное",
]
ShortText = Annotated[str, Field(min_length=1, max_length=160)]
Terms = Annotated[list[ShortText], Field(max_length=10)]


class Filters(BaseModel):
    """Значения внутри списка регионов/категорий — альтернативы, сорта — все обязательны."""

    model_config = ConfigDict(extra="forbid")
    color: Color | None = None
    sweetness: Sweetness | None = None
    grapes: Terms = Field(default_factory=list)
    regions: Terms = Field(default_factory=list)
    producer: ShortText | None = None
    source_categories: Terms = Field(default_factory=list)


class UserIntent(BaseModel):
    """Явные требования отделены от пожеланий, которые влияют только на баллы."""

    model_config = ConfigDict(extra="forbid")
    dish: ShortText | None = None
    normalized_food_categories: Terms = Field(default_factory=list)
    hard_constraints: Filters = Field(default_factory=Filters)
    soft_preferences: Filters = Field(default_factory=Filters)
    taste_preferences: Annotated[list[Taste], Field(max_length=8)] = Field(default_factory=list)
    budget_max_rub: Annotated[Decimal, Field(gt=0, le=100000000, allow_inf_nan=False)] | None = None
    missing_information: list[Literal["color", "sweetness", "food", "unsupported_request"]] = Field(
        default_factory=list, max_length=4
    )
    needs_clarification: bool = False
    allow_defaults: bool = False


class RecommendationRequest(BaseModel):
    """Клиент может передать предпочтения прошлого шага без серверной сессии."""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    previous_intent: UserIntent | None = None
    limit: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def nonempty_message(self) -> RecommendationRequest:
        """Не принимать сообщение из одних пробелов."""
        if not self.message.strip():
            raise ValueError("Введите пожелания к вину.")
        return self


class ScoreComponent(BaseModel):
    """Баллы сопровождаются проверяемой причиной, включая цитату при вкусовом совпадении."""

    key: str
    score: Decimal
    reason: str


class Recommendation(BaseModel):
    """Фактическая карточка всегда повторно читается из репозитория."""

    wine_id: int
    wine: Wine
    score: Decimal
    breakdown: list[ScoreComponent]
    reasons: list[str]
    explanation: str
    budget_satisfied: bool | None = None

    @model_validator(mode="after")
    def consistent_id(self) -> Recommendation:
        """Исключить расхождение идентификатора и фактической карточки."""
        if self.wine_id != self.wine.id:
            raise ValueError("Идентификатор рекомендации не совпадает с карточкой каталога.")
        return self


class RecommendationResponse(BaseModel):
    """Ответ явно различает неизвестный бюджет, уточнение и отсутствие подходящих вин."""

    status: Literal["ok", "clarification", "no_candidates", "uninterpretable", "empty_catalog"]
    message: str
    intent: UserIntent
    recommendations: list[Recommendation] = Field(default_factory=list)
    budget_status: Literal["not_requested", "applied", "budget_constraint_unavailable"] = (
        "not_requested"
    )
    warnings: list[str] = Field(default_factory=list)
    extraction_source: Literal["llm", "fallback"] = "fallback"
