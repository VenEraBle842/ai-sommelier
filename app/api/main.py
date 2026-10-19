"""API чтения и подбора по локальному снимку; загрузка каталога выполняется отдельно."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.catalog.models import Wine
from app.catalog.repository import WineRepository
from app.db.database import init_database
from app.llm.base import LLMConfigurationError, TextLLM
from app.llm.client import CompatibleTextClient, LLMSettings
from app.recommendation.schemas import RecommendationRequest, RecommendationResponse
from app.recommendation.service import RecommendationService


def create_app(db_path: str | Path | None = None, *, llm: TextLLM | None = None) -> FastAPI:
    """Создать API с отдельным жизненным циклом SQLite."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        engine = init_database(db_path)
        application.state.repository = WineRepository(engine)
        configured_llm = llm
        if configured_llm is None:
            try:
                settings = LLMSettings.from_env()
                if settings is not None:
                    configured_llm = CompatibleTextClient(settings)
            except LLMConfigurationError:
                logging.getLogger(__name__).warning(
                    "Настройки внешней модели некорректны; включён ограниченный локальный разбор."
                )
        application.state.recommendations = RecommendationService(
            application.state.repository, configured_llm
        )
        try:
            yield
        finally:
            if configured_llm is not None and configured_llm is not llm:
                configured_llm.close()
            engine.dispose()

    application = FastAPI(
        title="Винный помощник",
        description="Подбор вина и чтение сохранённых карточек vino-svoe.ru. Цены могут отсутствовать.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Не передавать пользователю встроенные сообщения валидации на английском."""
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Некорректные параметры запроса. Проверьте сообщение, предпочтения, идентификатор, размер страницы и смещение."
            },
        )

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        """Перевести стандартные ошибки маршрутизации."""
        messages = {
            404: "Запись или страница не найдена.",
            405: "Этот HTTP-метод не поддерживается.",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": messages.get(exc.status_code, "Не удалось выполнить запрос.")},
        )

    @application.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        """Вернуть понятное сообщение при сбое хранилища."""
        return JSONResponse(
            status_code=500, content={"detail": "Не удалось прочитать локальный каталог."}
        )

    @application.get("/wines", response_model=list[Wine], summary="Прочитать каталог")
    def list_wines(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500, description="Размер страницы")] = 50,
        offset: Annotated[int, Query(ge=0, description="Смещение")] = 0,
    ) -> list[Wine]:
        """Вернуть только записи локального каталога."""
        return request.app.state.repository.list_wines(limit=limit, offset=offset)

    @application.get("/wines/{wine_id}", response_model=Wine, summary="Прочитать карточку")
    def get_wine(wine_id: int, request: Request) -> Wine:
        """Найти сохранённое вино по локальному идентификатору."""
        wine = request.app.state.repository.get(wine_id)
        if wine is None:
            raise HTTPException(status_code=404, detail="Вино не найдено в локальном каталоге.")
        return wine

    @application.post(
        "/recommendations",
        response_model=RecommendationResponse,
        summary="Подобрать вино по пожеланиям",
    )
    def recommend(payload: RecommendationRequest, request: Request) -> RecommendationResponse:
        """Рекомендовать только сохранённые вина; синхронизация каталога здесь не запускается."""
        return request.app.state.recommendations.recommend(payload)

    return application


app = create_app()
