"""Профиль оценивает только историю конкретного пользователя и подтверждённые признаки."""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.users.preferences import MAX_HISTORY_SCORE, PreferenceProfile, wine_features
from app.users.repository import UserRepository


def test_profile_only_derives_source_supported_taste(merlo, repository):
    wine = repository.upsert(merlo)
    features = wine_features(wine)
    assert ("taste", "насыщенное") not in features
    assert any(dimension == "taste" for dimension, value in features)


def test_feedback_is_isolated_and_duplicate_clicks_do_not_amplify(repository, merlo):
    wine = repository.upsert(merlo)
    users = UserRepository(repository.engine)
    first, second = uuid4(), uuid4()
    users.record(first, [wine.id], "like")
    initial = PreferenceProfile.from_feedback(users.latest_feedback(first))
    users.record(first, [wine.id], "like")
    repeated = PreferenceProfile.from_feedback(users.latest_feedback(first))
    assert initial == repeated
    assert PreferenceProfile.from_feedback(users.latest_feedback(second)).components(wine) == []
    users.record(first, [wine.id], "dislike")
    assert len(users.latest_feedback(first)) == 1
    disliked = PreferenceProfile.from_feedback(users.latest_feedback(first))
    assert disliked.wine_affinity[wine.id] == -1
    assert disliked.components(wine)[0].score < 0


def test_profile_clamps_history_and_handles_cancelled_feature_votes(repository, merlo):
    first = repository.upsert(merlo)
    values = merlo.model_dump()
    values.update(slug="uchebnyj-dubl", source_url="https://vino-svoe.ru/wines/uchebnyj-dubl")
    second = repository.upsert(type(merlo).model_validate(values))
    users = UserRepository(repository.engine)
    user = uuid4()
    users.record(user, [first.id], "like")
    liked = PreferenceProfile.from_feedback(users.latest_feedback(user))
    assert Decimal(0) < liked.components(first)[0].score <= MAX_HISTORY_SCORE
    users.record(user, [second.id], "dislike")
    mixed = PreferenceProfile.from_feedback(users.latest_feedback(user))
    assert all(value == 0 for value in mixed.feature_affinity.values())
    assert mixed.components(first)[0].score == Decimal(4)
    assert mixed.components(second)[0].score == Decimal(-4)


def test_unknown_wine_rolls_back_whole_event_batch(repository, merlo):
    wine = repository.upsert(merlo)
    users = UserRepository(repository.engine)
    user = uuid4()
    with pytest.raises(ValueError, match="не найдено"):
        users.record(user, [wine.id, 999999], "like")
    assert users.history(user) == []


def test_history_order_and_limits_are_predictable(repository, merlo):
    wine = repository.upsert(merlo)
    users = UserRepository(repository.engine)
    user = uuid4()
    users.record(user, [wine.id], "shown")
    users.record(user, [wine.id], "opened")
    assert [event.event_type for event in users.history(user)] == ["opened", "shown"]
    assert users.history(user, limit=1)[0].event_type == "opened"
    assert users.latest_feedback(user) == []
    with pytest.raises(ValueError, match="Размер истории"):
        users.history(user, limit=201)
