"""Независимый подсчёт по ID и полям SQLite, без вызова рабочего ранжировщика."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.catalog.models import Wine
from app.catalog.repository import WineRepository
from app.evaluation.schemas import Expected
from app.recommendation.schemas import RecommendationResponse


def ratio(passed: int, total: int) -> float | str:
    """Не заменять отсутствие наблюдений нулём или идеальным результатом."""
    return passed / total if total else "N/A"


def canonical(value: str | None) -> str:
    """Нормализация эталона определена отдельно от производственного фильтра."""
    return " ".join((value or "").casefold().replace("ё", "е").split())


def constraint_checks(wine: Wine | None, expected: Expected) -> dict[str, bool]:
    """Оценить только заданные условия; неизвестная цена не даёт наблюдения бюджета."""
    hard = expected.hard_constraints
    checks = {}
    for key in ("color", "sweetness", "producer"):
        if value := getattr(hard, key):
            checks[key] = wine is not None and canonical(getattr(wine, key)) == canonical(value)
    if hard.grapes:
        checks["grapes"] = wine is not None and {canonical(v) for v in hard.grapes} <= {
            canonical(v) for v in wine.grapes
        }
    for key, actual in (
        ("regions", wine.region if wine else None),
        ("source_categories", wine.source_category if wine else None),
    ):
        if values := getattr(hard, key):
            checks[key] = wine is not None and canonical(actual) in {canonical(v) for v in values}
    if expected.budget_max_rub is not None and wine is not None and wine.price_rub is not None:
        checks["budget"] = wine.price_rub <= expected.budget_max_rub
    return checks


def food_matches(wine: Wine, expected: Expected) -> bool:
    """Хотя бы одно ожидаемое реальное гастрономическое сочетание."""
    return bool(
        {canonical(v) for v in expected.food_pairings} & {canonical(v) for v in wine.food_pairings}
    )


def is_hit(wine: Wine, expected: Expected) -> bool:
    """Совместное соблюдение эталона; неизвестная цена не удовлетворяет бюджету."""
    return (
        all(constraint_checks(wine, expected).values())
        and (not expected.food_pairings or food_matches(wine, expected))
        and (not expected.acceptable_slugs or wine.slug in expected.acceptable_slugs)
        and (
            expected.budget_max_rub is None
            or (wine.price_rub is not None and wine.price_rub <= expected.budget_max_rub)
        )
    )


@dataclass
class Metrics:
    """Счётчики с открытыми знаменателями, включая случаи нулевой выдачи."""

    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    dimensions: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def observe(
        self, repository: WineRepository, response: RecommendationResponse, expected: Expected
    ) -> dict[str, Any]:
        """Проверить фактический ответ; ошибки ID учитываются, а не выкидываются из знаменателя."""
        self.counts["scenarios"] += 1
        status_ok = response.status == expected.status
        self.counts["status_correct"] += status_ok
        budget_ok = not expected.budget_unavailable or (
            response.budget_status == "budget_constraint_unavailable"
            and all(r.budget_satisfied is None for r in response.recommendations)
        )
        self.counts["budget_unavailable_checks"] += expected.budget_unavailable
        self.counts["budget_unavailable_passed"] += expected.budget_unavailable and budget_ok
        positive = expected.status == "ok" and not expected.budget_unavailable
        self.counts["hit_scenarios"] += positive
        has_hard = any(expected.hard_constraints.model_dump().values())
        hits = 0
        failures = []
        all_facts_correct = True
        for item in response.recommendations:
            self.counts["recommendations"] += 1
            wine = repository.get(item.wine_id)
            self.counts["valid"] += wine is not None
            self.counts["factual_checks"] += 1
            facts_correct = wine is not None and item.wine == wine
            self.counts["factual_correct"] += facts_correct
            all_facts_correct = all_facts_correct and facts_correct
            checks = constraint_checks(wine, expected)
            if expected.budget_max_rub is not None and (wine is None or wine.price_rub is None):
                self.counts["unknown_budget_excluded"] += 1
            if has_hard or "budget" in checks:
                self.counts["hard_checked"] += 1
                correct = wine is not None and all(checks.values())
                self.counts["hard_correct"] += correct
                if not correct:
                    failures.append(item.wine_id)
            for key, passed in checks.items():
                self.dimensions[key][0] += passed
                self.dimensions[key][1] += 1
            if expected.food_pairings:
                self.counts["food_checked"] += 1
                self.counts["food_correct"] += wine is not None and food_matches(wine, expected)
            hits += wine is not None and is_hit(wine, expected)
        self.counts["hit_passed"] += positive and hits > 0
        success = (
            status_ok
            and all_facts_correct
            and budget_ok
            and (not positive or hits > 0)
            and not failures
            and (bool(response.recommendations) == (expected.status == "ok"))
        )
        self.counts["scenario_passed"] += success
        return {
            "status_correct": status_ok,
            "success": success,
            "hit": bool(hits) if positive else "N/A",
            "hard_constraint_failures": failures,
        }

    def summary(self) -> dict[str, Any]:
        """Все доли — от нуля до единицы; N/A означает нулевой знаменатель."""
        c = self.counts
        return {
            "scenario_count": c["scenarios"],
            "recommendation_count": c["recommendations"],
            "catalog_validity": ratio(c["valid"], c["recommendations"]),
            "hallucination_rate": ratio(c["recommendations"] - c["valid"], c["recommendations"]),
            "hard_constraint_accuracy": ratio(c["hard_correct"], c["hard_checked"]),
            "hard_constraint_by_dimension": {
                key: {"accuracy": ratio(*self.dimensions[key]), "count": self.dimensions[key][1]}
                for key in (
                    "color",
                    "sweetness",
                    "grapes",
                    "regions",
                    "producer",
                    "source_categories",
                    "budget",
                )
            },
            "food_match_rate": ratio(c["food_correct"], c["food_checked"]),
            "hit_rate_at_k": ratio(c["hit_passed"], c["hit_scenarios"]),
            "scenario_success_rate": ratio(c["scenario_passed"], c["scenarios"]),
            "factual_field_accuracy": ratio(c["factual_correct"], c["factual_checks"]),
            "denominators": dict(c),
        }
