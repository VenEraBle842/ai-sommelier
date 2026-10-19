"""История действий в той же SQLite; показы не считаются симпатией."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.catalog.models import Wine
from app.db.tables import UserEventRecord, WineRecord
from app.users.schemas import EventType, HistoryEvent


class UserRepository:
    """Сохранение событий и чтение последней оценки каждого вина."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _event(record: UserEventRecord, wine: WineRecord) -> HistoryEvent:
        return HistoryEvent(
            id=record.id,
            user_id=UUID(record.user_id),
            wine_id=record.wine_id,
            event_type=cast(EventType, record.event_type),
            timestamp=record.timestamp,
            wine=Wine.model_validate(wine),
        )

    def record(
        self, user_id: UUID, wine_ids: list[int], event_type: EventType
    ) -> list[HistoryEvent]:
        """Записать действия атомарно; неизвестное вино не попадает в историю."""
        events = []
        with Session(self.engine) as session, session.begin():
            for wine_id in dict.fromkeys(wine_ids):
                wine = session.get(WineRecord, wine_id)
                if wine is None:
                    raise ValueError("Вино не найдено в локальном каталоге.")
                record = UserEventRecord(
                    user_id=str(user_id),
                    wine_id=wine_id,
                    event_type=event_type,
                    timestamp=datetime.now(UTC),
                )
                session.add(record)
                session.flush()
                events.append(self._event(record, wine))
        return events

    def history(self, user_id: UUID, *, limit: int = 50) -> list[HistoryEvent]:
        """Последние события пользователя в устойчивом порядке, новые первыми."""
        if not 1 <= limit <= 200:
            raise ValueError("Размер истории должен быть от 1 до 200.")
        with Session(self.engine) as session:
            rows = session.execute(
                select(UserEventRecord, WineRecord)
                .join(WineRecord, UserEventRecord.wine_id == WineRecord.id)
                .where(UserEventRecord.user_id == str(user_id))
                .order_by(UserEventRecord.id.desc())
                .limit(limit)
            )
            return [self._event(event, wine) for event, wine in rows]

    def latest_feedback(self, user_id: UUID) -> list[HistoryEvent]:
        """По одному последнему лайку/дизлайку: повторные нажатия не усиливают профиль."""
        with Session(self.engine) as session:
            latest = (
                select(UserEventRecord.wine_id, func.max(UserEventRecord.id).label("id"))
                .where(
                    UserEventRecord.user_id == str(user_id),
                    UserEventRecord.event_type.in_(["like", "dislike"]),
                )
                .group_by(UserEventRecord.wine_id)
                .subquery()
            )
            rows = session.execute(
                select(UserEventRecord, WineRecord)
                .join(latest, UserEventRecord.id == latest.c.id)
                .join(WineRecord, UserEventRecord.wine_id == WineRecord.id)
                .order_by(UserEventRecord.id)
            )
            return [self._event(event, wine) for event, wine in rows]
