"""Проверяемые события локального пользователя и контракт чата."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.catalog.models import Wine
from app.recommendation.schemas import RecommendationRequest

EventType = Literal["shown", "like", "dislike", "opened"]


class ChatRequest(RecommendationRequest):
    """Контекст диалога передаётся явно, история оценок определяется локальным UUID."""

    user_id: UUID


class FeedbackRequest(BaseModel):
    """Клиент не может самостоятельно записывать показы или изменять каталог."""

    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    wine_id: Annotated[int, Field(gt=0)]
    event_type: Literal["like", "dislike", "opened"]


class HistoryEvent(BaseModel):
    """Фактическая карточка истории читается из текущего каталога."""

    id: int
    user_id: UUID
    wine_id: int
    event_type: EventType
    timestamp: datetime
    wine: Wine


class FeedbackResponse(BaseModel):
    """Краткое подтверждение сохранённого действия."""

    message: str
    event: HistoryEvent
