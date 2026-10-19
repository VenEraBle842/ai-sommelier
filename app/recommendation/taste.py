"""Небольшой словарь явно выраженных пожеланий к вину вне описания блюда."""

import re

from app.recommendation.food import extract_dish_phrase
from app.recommendation.schemas import NegativeTaste, Taste, UserIntent
from app.recommendation.vocabulary import (
    NEGATIVE_TASTE_REQUEST_PATTERN,
    TASTE_MENTION_PATTERNS,
    TASTE_REQUEST_PATTERNS,
    normalize,
)


def wine_preference_text(message: str, dish: str | None = None) -> str:
    """Убрать фразу о блюде; не приписывать вину свойства соуса или гарнира."""
    result = normalize(message)
    for phrase in (extract_dish_phrase(message), dish):
        if phrase:
            result = result.replace(normalize(phrase), " ")
    # Поддерживается только локальная конструкция «прилагательное + еда», а не общий NLP.
    return re.sub(r"\b(?:\w+\s+){0,2}(?:соус\w*|желе|пюре|рагу|блюд\w*|гарнир\w*)\b", " ", result)


def extract_taste_signals(
    message: str, dish: str | None = None
) -> tuple[list[Taste], list[NegativeTaste]]:
    """Распознать только известные формы прилагательных и одну группу избегания."""
    text = wine_preference_text(message, dish)
    negatives: list[NegativeTaste] = (
        ["грубая терпкость"] if re.search(NEGATIVE_TASTE_REQUEST_PATTERN, text) else []
    )
    positives = [
        taste
        for taste, pattern in TASTE_REQUEST_PATTERNS.items()
        if _has_positive_mention(text, pattern)
    ]
    return positives, negatives


def _has_positive_mention(text: str, pattern: str) -> bool:
    """Не принимать явно отрицаемое свойство за желание пользователя."""
    # Законченное избегание грубой терпкости не отрицает следующее пожелание к вину.
    text = re.sub(NEGATIVE_TASTE_REQUEST_PATTERN, " ", text)
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 35) : match.start()]
        if not re.search(r"\b(?:без|не|избегаю)\b(?:\s+\w+){0,3}\s*$", prefix):
            return True
    return False


def reconcile_taste_signals(message: str, intent: UserIntent) -> UserIntent:
    """Сохранить независимые вкусовые сигналы и убрать явное заимствование из блюда."""
    scope = wine_preference_text(message, intent.dish)
    complete = normalize(message)
    positive, negative = extract_taste_signals(message, intent.dish)
    preserved = []
    for taste in intent.taste_preferences:
        pattern = TASTE_MENTION_PATTERNS[taste]
        if re.search(pattern, complete) and not _has_positive_mention(scope, pattern):
            continue
        preserved.append(taste)
    values = intent.model_dump()
    values["taste_preferences"] = list(dict.fromkeys([*preserved, *positive]))
    preserved_negative = intent.negative_taste_preferences
    if re.search(NEGATIVE_TASTE_REQUEST_PATTERN, complete) and not re.search(
        NEGATIVE_TASTE_REQUEST_PATTERN, scope
    ):
        preserved_negative = []
    values["negative_taste_preferences"] = list(dict.fromkeys([*preserved_negative, *negative]))
    return UserIntent.model_validate(values)
