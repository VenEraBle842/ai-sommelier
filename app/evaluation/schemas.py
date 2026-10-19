"""Контракт независимых ожиданий и входных данных оценки."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.recommendation.schemas import Filters, UserIntent


class Expected(BaseModel):
    """Проверяемые свойства; пустая выдача не считается успешным попаданием."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "clarification", "no_candidates", "uninterpretable"] = "ok"
    hard_constraints: Filters = Field(default_factory=Filters)
    budget_max_rub: Decimal | None = Field(default=None, gt=0)
    food_pairings: list[str] = Field(default_factory=list)
    acceptable_slugs: list[str] = Field(default_factory=list)
    budget_unavailable: bool = False


class Feedback(BaseModel):
    """Ссылка на реальную карточку, независимая от ID рабочей базы."""

    model_config = ConfigDict(extra="forbid")
    slug: str
    event_type: Literal["like", "dislike"] = "like"


class Scenario(BaseModel):
    """Текстовый либо явно структурированный сценарий с независимым эталоном."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    intent: UserIntent | None = None
    history: list[Feedback] = Field(default_factory=list)
    personalization_check: bool = False
    expected: Expected

    @model_validator(mode="after")
    def require_like(self) -> Scenario:
        """Изменение персонализации измеряется только после одного лайка."""
        if self.personalization_check and (
            len(self.history) != 1 or self.history[0].event_type != "like"
        ):
            raise ValueError("Проверка персонализации требует ровно одного лайка.")
        return self
