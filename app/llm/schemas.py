"""LLM разрешено выбирать только существующие идентификаторы причин."""

from pydantic import BaseModel, ConfigDict, Field


class GroundedReason(BaseModel):
    """Формулировка, подготовленная Python по данным каталога."""

    reason_id: str
    text: str


class ExplanationCandidate(BaseModel):
    """Внешней модели передаётся только краткое обоснование выбранной позиции."""

    wine_id: int
    reasons: list[GroundedReason]


class ExplanationSelection(BaseModel):
    """Копии названий, цен и любых других фактов из ответа модели игнорируются."""

    model_config = ConfigDict(extra="ignore")
    wine_id: int = Field(strict=True, gt=0)
    reason_ids: list[str] = Field(min_length=1, max_length=30)


class ExplanationResponse(BaseModel):
    """Порядок вин в ответе модели не меняет детерминированный рейтинг."""

    model_config = ConfigDict(extra="ignore")
    explanations: list[ExplanationSelection] = Field(max_length=5)
