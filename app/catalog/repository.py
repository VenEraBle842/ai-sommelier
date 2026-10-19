"""Операции каталога работают только с локальной SQLite."""

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.catalog.models import Wine, WineData
from app.catalog.urls import wine_url
from app.db.tables import WineRecord


class WineRepository:
    """Чтение каталога и атомарное обновление по адресу источника."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def upsert(self, wine: WineData) -> Wine:
        """Сохранить текущий снимок полей, не меняя существующий id."""
        values = wine.model_dump(exclude={"id"})
        statement = insert(WineRecord).values(**values)
        upsert = statement.on_conflict_do_update(
            index_elements=[WineRecord.source_url],
            set_={key: getattr(statement.excluded, key) for key in values},
        ).returning(WineRecord)
        with Session(self.engine) as session, session.begin():
            record = session.scalars(upsert).one()
            return Wine.model_validate(record)

    def get(self, wine_id: int) -> Wine | None:
        """Прочитать одну запись по локальному id."""
        with Session(self.engine) as session:
            record = session.get(WineRecord, wine_id)
            return Wine.model_validate(record) if record else None

    def get_by_source_url(self, source_url: str) -> Wine | None:
        """Найти карточку по каноническому адресу сайта."""
        with Session(self.engine) as session:
            record = session.scalar(
                select(WineRecord).where(WineRecord.source_url == wine_url(source_url))
            )
            return Wine.model_validate(record) if record else None

    def list_wines(self, *, limit: int = 50, offset: int = 0) -> list[Wine]:
        """Прочитать страницу каталога в устойчивом порядке без сетевых запросов."""
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("Размер страницы должен быть от 1 до 500, смещение — неотрицательным.")
        with Session(self.engine) as session:
            records = session.scalars(
                select(WineRecord).order_by(WineRecord.id).limit(limit).offset(offset)
            )
            return [Wine.model_validate(record) for record in records]

    def count(self) -> int:
        """Вернуть число сохранённых позиций."""
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(WineRecord)) or 0
