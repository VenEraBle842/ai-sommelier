"""Изолированный прогон реального снимка и золотых сценариев без настроек LLM."""

import hashlib
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter

from app.catalog.parser import parse_wine
from app.catalog.repository import WineRepository
from app.db.database import init_database
from app.evaluation.metrics import Metrics, ratio
from app.evaluation.schemas import Scenario
from app.recommendation.schemas import RecommendationRequest, RecommendationResponse
from app.recommendation.service import RecommendationService
from app.users.preferences import PreferenceProfile
from app.users.repository import UserRepository

DATA = Path(__file__).with_name("data")


class SnapshotCard(BaseModel):
    """Зафиксированное происхождение каждой реальной HTML-карточки."""

    model_config = ConfigDict(extra="forbid")
    file: str
    source_url: str
    sha256: str
    scraped_at: datetime


def load_snapshot(repository: WineRepository) -> list[dict[str, Any]]:
    """Проверить контрольные суммы и разобрать HTML штатным парсером во временную базу."""
    cards = TypeAdapter(list[SnapshotCard]).validate_json((DATA / "snapshot.json").read_bytes())
    manifest = []
    for card in cards:
        path = (DATA / card.file).resolve()
        if path.parent != DATA.resolve():
            raise ValueError("Файл снимка должен находиться в папке данных оценки.")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != card.sha256:
            raise ValueError("Контрольная сумма карточки снимка не совпадает.")
        parsed = parse_wine(content.decode("utf-8"), card.source_url)
        parsed.scraped_at = card.scraped_at
        wine = repository.upsert(parsed)
        manifest.append({"wine_id": wine.id, "slug": wine.slug, **card.model_dump(mode="json")})
    return manifest


def load_scenarios() -> list[Scenario]:
    """Не допускать пустой набор, повторные ID или непроверенные поля эталона."""
    scenarios = TypeAdapter(list[Scenario]).validate_json((DATA / "scenarios.json").read_bytes())
    if not scenarios or len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("Набор сценариев пуст или содержит повторные идентификаторы.")
    return scenarios


def run_evaluation(*, k: int = 3) -> dict[str, Any]:
    """Воспроизвести прогон; рабочая база, переменные LLM и сеть не используются."""
    if not 1 <= k <= 5:
        raise ValueError("K должно быть от 1 до 5.")
    with TemporaryDirectory(prefix="svoevino-evaluation-") as directory:
        engine = init_database(Path(directory) / "catalog.sqlite3")
        try:
            repository = WineRepository(engine)
            manifest = load_snapshot(repository)
            scenarios = load_scenarios()
            known = {wine.slug: wine for wine in repository.list_wines()}
            foods = {food for wine in known.values() for food in wine.food_pairings}
            service = RecommendationService(repository, llm=None)
            users = UserRepository(engine)
            metrics = Metrics()
            details = []
            personalization: list[dict[str, Any]] = []
            for number, scenario in enumerate(scenarios, start=1):
                referenced = set(scenario.expected.acceptable_slugs) | {
                    event.slug for event in scenario.history
                }
                if not referenced <= known.keys():
                    raise ValueError("Эталон ссылается на отсутствующее в снимке вино.")
                if not set(scenario.expected.food_pairings) <= foods:
                    raise ValueError("Эталон содержит отсутствующее в снимке сочетание с едой.")

                def recommend(
                    profile: PreferenceProfile | None = None,
                    scenario: Scenario = scenario,
                    limit: int = k,
                ) -> RecommendationResponse:
                    """Один и тот же конвейер для базового и персонального ответа."""
                    if scenario.intent is not None:
                        return service.recommend_intent(
                            scenario.intent, limit=limit, message=scenario.message, profile=profile
                        )
                    return service.recommend(
                        RecommendationRequest(message=scenario.message, limit=limit),
                        profile=profile,
                    )

                baseline = recommend(limit=5) if scenario.personalization_check else None
                user = UUID(int=number)
                for event in scenario.history:
                    users.record(user, [known[event.slug].id], event.event_type)
                profile = PreferenceProfile.from_feedback(users.latest_feedback(user))
                response = recommend(profile)
                result = metrics.observe(repository, response, scenario.expected)
                details.append(
                    {
                        "id": scenario.id,
                        "mode": "structured" if scenario.intent is not None else "fallback_text",
                        "message": scenario.message,
                        "status": response.status,
                        "wine_ids": [r.wine_id for r in response.recommendations],
                        **result,
                    }
                )
                if baseline is not None:
                    target = known[scenario.history[0].slug].id
                    before = {r.wine_id: r for r in baseline.recommendations}
                    after = {r.wine_id: r for r in response.recommendations}
                    before_order, after_order = list(before), list(after)
                    old = before.get(target)
                    new = after.get(target)
                    history_score = (
                        sum((p.score for p in new.breakdown if p.key == "personalization"), start=0)
                        if new
                        else 0
                    )
                    passed = (
                        old is not None
                        and new is not None
                        and new.score > old.score
                        and history_score > 0
                        and after_order.index(target) <= before_order.index(target)
                    )
                    personalization.append(
                        {
                            "scenario_id": scenario.id,
                            "wine_id": target,
                            "passed": passed,
                            "baseline_wine_ids": before_order,
                            "personalized_wine_ids": after_order,
                            "baseline_score": str(old.score) if old else None,
                            "personalized_score": str(new.score) if new else None,
                            "personalization_score": str(history_score),
                        }
                    )
            return {
                **metrics.summary(),
                "k": k,
                "dataset_version": 1,
                "dataset_sha256": hashlib.sha256(
                    (DATA / "scenarios.json").read_bytes()
                ).hexdigest(),
                "catalog_size": repository.count(),
                "known_price_count": sum(w.price_rub is not None for w in known.values()),
                "scenario_modes": {
                    mode: sum(d["mode"] == mode for d in details)
                    for mode in ("fallback_text", "structured")
                },
                "personalization_checks": {
                    "count": len(personalization),
                    "passed": sum(p["passed"] for p in personalization),
                    "rate": ratio(sum(p["passed"] for p in personalization), len(personalization)),
                    "details": personalization,
                },
                "snapshot": manifest,
                "scenarios": details,
                "notes": [
                    "Фиксированный сокращённый снимок реальных карточек; не весь каталог сайта.",
                    "Внешняя модель не вызывается. Структурированные сценарии не измеряют понимание текста.",
                    "Основные метрики учитывают итоговые ответы; дополнительные базовые ответы персонализации в них не входят.",
                    "N/A означает отсутствие наблюдений. Бюджет неизвестных цен исключён из точности ограничений.",
                    "Бюджетные сценарии без цен проверяют честное предупреждение и не входят в HitRate@K.",
                    "HitRate@K — хотя бы одно совпадение с эталоном в первых K; это не абсолютная оценка вкуса.",
                ],
            }
        finally:
            engine.dispose()
