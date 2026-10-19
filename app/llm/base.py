"""Небольшой контракт без привязки к коммерческому поставщику."""

from typing import Protocol

from app.llm.schemas import ExplanationCandidate, ExplanationResponse
from app.recommendation.schemas import UserIntent


class LLMError(Exception):
    """Безопасная ошибка модели без ключей, тела ответа и технической трассировки."""


class LLMUnavailable(LLMError):
    """Внешняя модель недоступна или отклонила запрос."""


class LLMResponseError(LLMError):
    """Ответ модели не прошёл локальную проверку."""


class LLMConfigurationError(LLMError):
    """Настройки неполны или не соответствуют поддерживаемому HTTP-интерфейсу."""


class TextLLM(Protocol):
    """Модель извлекает предпочтения и выбирает подтверждённые причины; Wine не создаёт."""

    def extract_preferences(self, message: str, food_categories: list[str]) -> UserIntent:
        """Вернуть локально проверенное намерение из русского текста."""
        ...

    def explain_recommendations(
        self, message: str, candidates: list[ExplanationCandidate]
    ) -> ExplanationResponse:
        """Выбрать только переданные причины для уже отобранных Python позиций."""
        ...

    def close(self) -> None:
        """Освободить собственные сетевые ресурсы."""
        ...
