"""Небольшой профиль по последним оценкам: без обучения, предположений и скрытых весов."""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from app.catalog.models import Wine
from app.recommendation.schemas import ScoreComponent
from app.recommendation.vocabulary import TASTE_PATTERNS, normalize, taste_evidence
from app.users.schemas import HistoryEvent

# Влияние истории ограничено; совпадение блюда весит 10, вкуса — 3.
HISTORY_WEIGHTS = {
    "wine": Decimal("4"),
    "color": Decimal("0.25"),
    "sweetness": Decimal("0.25"),
    "grape": Decimal("1"),
    "region": Decimal("0.25"),
    "producer": Decimal("0.25"),
    "food": Decimal("0.5"),
    "taste": Decimal("0.5"),
}
MAX_HISTORY_SCORE = Decimal("6")
LABELS = {
    "color": "цвет",
    "sweetness": "сладость",
    "grape": "сорт",
    "region": "регион",
    "producer": "производитель",
    "food": "сочетание с едой",
    "taste": "признак из описания",
}


def wine_features(wine: Wine) -> dict[tuple[str, str], str]:
    """Брать признаки только из полей и подтверждённых фрагментов описания."""
    features = {}
    for dimension, values in (
        ("color", [wine.color]),
        ("sweetness", [wine.sweetness]),
        ("grape", wine.grapes),
        ("region", [wine.region]),
        ("producer", [wine.producer]),
        ("food", wine.food_pairings),
        ("taste", [taste for taste in TASTE_PATTERNS if taste_evidence(wine.description, taste)]),
    ):
        for value in values:
            if value:
                features[(dimension, normalize(value))] = value
    return features


@dataclass
class PreferenceProfile:
    """Показ и открытие не влияют на балл; последняя оценка заменяет предыдущую."""

    wine_affinity: dict[int, int] = field(default_factory=dict)
    feature_affinity: dict[tuple[str, str], int] = field(default_factory=dict)

    @classmethod
    def from_feedback(cls, events: list[HistoryEvent]) -> PreferenceProfile:
        """Собрать профиль из последних оценок каждого вина конкретного пользователя."""
        profile = cls()
        votes: dict[tuple[str, str], int] = defaultdict(int)
        latest = {}
        for event in sorted(events, key=lambda event: event.id):
            if event.event_type in ("like", "dislike"):
                latest[event.wine_id] = event
        for event in latest.values():
            sign = 1 if event.event_type == "like" else -1
            profile.wine_affinity[event.wine_id] = sign
            for feature in wine_features(event.wine):
                votes[feature] += sign
        profile.feature_affinity = {key: max(-1, min(1, value)) for key, value in votes.items()}
        return profile

    def components(self, wine: Wine) -> list[ScoreComponent]:
        """Вернуть ограниченный балл и объяснение связи с реальными оценками."""
        total = Decimal(0)
        reasons = []
        if sign := self.wine_affinity.get(wine.id):
            total += HISTORY_WEIGHTS["wine"] * sign
            reasons.append(
                "вам понравилось это вино"
                if sign > 0
                else "вы отметили, что это вино вам не подошло"
            )
        for key, label in sorted(wine_features(wine).items()):
            if sign := self.feature_affinity.get(key, 0):
                total += HISTORY_WEIGHTS[key[0]] * sign
                attitude = "понравившихся" if sign > 0 else "не понравившихся"
                reasons.append(f"{LABELS[key[0]]} «{label}» встречается в {attitude} вам винах")
        if not reasons:
            return []
        total = max(-MAX_HISTORY_SCORE, min(MAX_HISTORY_SCORE, total))
        return [
            ScoreComponent(
                key="personalization",
                score=total,
                reason="История ваших оценок: "
                + "; ".join(reasons)
                + f". Влияние на балл: {total:+g}.",
            )
        ]
