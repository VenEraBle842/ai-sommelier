"""Эталон, независимые знаменатели метрик и инварианты без реальной сети."""

import json
from decimal import Decimal
from uuid import UUID

import pytest

from app.catalog.models import WineData
from app.evaluation.__main__ import main
from app.evaluation.metrics import Metrics, constraint_checks, is_hit
from app.evaluation.runner import DATA, load_scenarios, load_snapshot, run_evaluation
from app.evaluation.schemas import Expected
from app.llm.base import LLMUnavailable
from app.llm.fake import FakeLLM
from app.recommendation.schemas import Filters, RecommendationRequest, UserIntent
from app.recommendation.service import RecommendationService
from app.users.preferences import PreferenceProfile
from app.users.repository import UserRepository


@pytest.fixture
def snapshot(repository):
    load_snapshot(repository)
    return repository


def red_response(repository):
    return RecommendationService(repository).recommend(
        RecommendationRequest(message="Красное сухое")
    )


def test_evaluation_is_reproducible_and_ignores_runtime_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "учебный-секрет-не-используется")
    monkeypatch.setenv("LLM_BASE_URL", "https://unused.invalid")
    monkeypatch.setenv("WINE_DB_PATH", str(tmp_path / "рабочая.sqlite3"))
    first = run_evaluation()
    assert first == run_evaluation()
    assert not (tmp_path / "рабочая.sqlite3").exists()
    assert first["scenario_count"] == len(load_scenarios()) == 24
    assert first["catalog_size"] == 3
    assert first["scenario_modes"] == {"fallback_text": 20, "structured": 4}
    assert first["recommendation_count"] > 0
    assert first["hallucination_rate"] == 0
    assert first["catalog_validity"] == 1
    assert first["hard_constraint_accuracy"] == 1
    assert first["hit_rate_at_k"] == 1
    assert first["food_match_rate"] == pytest.approx(4 / 6)
    assert first["hard_constraint_by_dimension"]["budget"] == {"accuracy": "N/A", "count": 0}
    assert first["denominators"]["unknown_budget_excluded"] == 1
    for scenario in first["scenarios"]:
        assert scenario["success"]
        assert set(scenario["wine_ids"]) <= {w["wine_id"] for w in first["snapshot"]}
    personal = first["personalization_checks"]["details"][0]
    assert personal["passed"]
    assert personal["baseline_wine_ids"] == [3, 2]
    assert personal["personalized_wine_ids"] == [2, 3]
    assert Decimal(personal["personalization_score"]) > 0


def test_hallucination_and_factual_metrics_are_measured_not_constants(snapshot):
    response = red_response(snapshot)
    valid = response.recommendations[0]
    invented = valid.model_copy(
        update={"wine_id": 999999, "wine": valid.wine.model_copy(update={"id": 999999})}
    )
    response.recommendations.append(invented)
    metrics = Metrics()
    observed = metrics.observe(
        snapshot, response, Expected(hard_constraints=Filters(color="красное"))
    )
    report = metrics.summary()
    assert report["catalog_validity"] == 0.5
    assert report["hallucination_rate"] == 0.5
    assert report["hard_constraint_accuracy"] == 0.5
    assert report["factual_field_accuracy"] == 0.5
    assert report["hard_constraint_by_dimension"]["color"]["accuracy"] == 0.5
    assert not observed["success"]
    assert observed["hard_constraint_failures"] == [999999]


def test_metric_detects_copied_factual_fields_and_wrong_hard_constraints(snapshot):
    response = red_response(snapshot)
    response.recommendations[0].wine.name = "Выдуманное название"
    metrics = Metrics()
    metrics.observe(snapshot, response, Expected(hard_constraints=Filters(color="белое")))
    assert metrics.summary()["hard_constraint_accuracy"] == 0
    assert metrics.summary()["factual_field_accuracy"] == 0
    assert metrics.summary()["hit_rate_at_k"] == 0
    assert metrics.summary()["catalog_validity"] == 1


def test_empty_response_does_not_manufacture_accuracy_or_hits(snapshot):
    response = red_response(snapshot)
    response.recommendations = []
    metrics = Metrics()
    metrics.observe(snapshot, response, Expected(hard_constraints=Filters(color="красное")))
    summary = metrics.summary()
    for key in (
        "catalog_validity",
        "hallucination_rate",
        "hard_constraint_accuracy",
        "food_match_rate",
    ):
        assert summary[key] == "N/A"
    assert summary["hit_rate_at_k"] == 0
    assert summary["scenario_success_rate"] == 0
    assert Metrics().summary()["hit_rate_at_k"] == "N/A"


@pytest.mark.parametrize(
    "price,expected", [(None, None), ("1000", True), ("2000", True), ("2001", False)]
)
def test_budget_oracle_checks_known_prices_only(snapshot, price, expected):
    wine = snapshot.get(1).model_copy(update={"price_rub": Decimal(price) if price else None})
    golden = Expected(budget_max_rub=Decimal("2000"))
    checks = constraint_checks(wine, golden)
    if expected is None:
        assert "budget" not in checks
        assert not is_hit(wine, golden)
    else:
        assert checks["budget"] is expected
        assert is_hit(wine, golden) is expected


def test_snapshot_upsert_and_provenance(snapshot):
    before = snapshot.list_wines()
    load_snapshot(snapshot)
    assert snapshot.list_wines() == before
    assert all(w.price_rub is None for w in before)
    assert all(w.scraped_at.isoformat() == "2026-10-19T00:00:00+00:00" for w in before)


def test_tampered_snapshot_is_rejected(repository, monkeypatch, tmp_path):
    manifest = json.loads((DATA / "snapshot.json").read_text())
    (tmp_path / "snapshot.json").write_text(json.dumps(manifest))
    (tmp_path / "merlo.html").write_text("Изменённый источник")
    monkeypatch.setattr("app.evaluation.runner.DATA", tmp_path)
    with pytest.raises(ValueError, match="Контрольная сумма"):
        load_snapshot(repository)
    assert repository.count() == 0


def test_cli_saves_the_same_json_as_stdout(monkeypatch, tmp_path, capsys):
    output = tmp_path / "оценка.json"
    monkeypatch.setattr("sys.argv", ["оценка", "--output", str(output)])
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == output.read_text()
    assert not captured.err
    assert json.loads(captured.out)["scenario_count"] == 24


def test_cli_reports_invalid_data_without_traceback(monkeypatch, capsys):
    def fail(**kwargs):
        raise ValueError("Внутренняя техническая ошибка")

    monkeypatch.setattr("sys.argv", ["оценка"])
    monkeypatch.setattr("app.evaluation.__main__.run_evaluation", fail)
    assert main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Не удалось выполнить оценку" in captured.err
    assert "Внутренняя" not in captured.err


@pytest.mark.parametrize("phase", ["extract", "explain"])
def test_external_failure_keeps_catalog_and_feedback_intact(snapshot, phase):
    users = UserRepository(snapshot.engine)
    user = UUID(int=1)
    users.record(user, [2], "like")
    catalog_before = snapshot.list_wines()
    history_before = users.history(user)
    intent = UserIntent(hard_constraints=Filters(color="красное", sweetness="сухое"))
    llm = FakeLLM(
        LLMUnavailable("Учебный сбой") if phase == "extract" else intent,
        explanations=LLMUnavailable("Учебный сбой объяснения"),
    )
    response = RecommendationService(snapshot, llm).recommend(
        RecommendationRequest(message="Красное сухое")
    )
    assert response.status == "ok"
    assert snapshot.list_wines() == catalog_before
    assert users.history(user) == history_before
    assert response.warnings
    assert all(r.wine == snapshot.get(r.wine_id) for r in response.recommendations)


def test_personalization_never_overrides_constraints_or_unknown_price(snapshot):
    users = UserRepository(snapshot.engine)
    user = UUID(int=1)
    users.record(user, [2], "like")
    profile = PreferenceProfile.from_feedback(users.latest_feedback(user))
    response = RecommendationService(snapshot).recommend(
        RecommendationRequest(message="Красное сухое"), profile=profile
    )
    assert [r.wine_id for r in response.recommendations] == [1]
    wine = snapshot.get(1)
    # Искусственная цена проверяет строгий режим только во временной тестовой базе.
    snapshot.upsert(
        WineData.model_validate(wine.model_dump(exclude={"id"}) | {"price_rub": Decimal("1500")})
    )
    response = RecommendationService(snapshot).recommend(
        RecommendationRequest(message="Белое сухое до 2000 рублей"), profile=profile
    )
    assert response.budget_status == "applied"
    assert response.status == "no_candidates"
    assert not response.recommendations


def test_fake_cannot_insert_unknown_wine_or_replace_database_facts(snapshot):
    fake = FakeLLM(
        UserIntent(hard_constraints=Filters(color="красное", sweetness="сухое")),
        explanations=json.dumps(
            {
                "explanations": [
                    {"wine_id": 999999, "reason_ids": ["constraints"], "name": "Выдуманное вино"},
                    {
                        "wine_id": 1,
                        "reason_ids": ["constraints"],
                        "name": "Подмена",
                        "price_rub": "1",
                    },
                ]
            }
        ),
    )
    response = RecommendationService(snapshot, fake).recommend(
        RecommendationRequest(message="Красное сухое")
    )
    assert [r.wine_id for r in response.recommendations] == [1]
    assert all(r.wine == snapshot.get(r.wine_id) for r in response.recommendations)
    assert response.recommendations[0].wine.price_rub is None
    assert len(fake.explanation_calls[0]) <= 5
    assert "Выдуманное" not in response.model_dump_json()
