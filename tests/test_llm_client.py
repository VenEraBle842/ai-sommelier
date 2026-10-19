"""Внешний протокол проверяется только через MockTransport, без реального API."""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.llm.base import LLMConfigurationError, LLMResponseError, LLMUnavailable
from app.llm.client import CompatibleTextClient, LLMSettings
from app.llm.schemas import ExplanationCandidate, GroundedReason

TEST_SECRET = "test-secret-never-log"


def settings():
    return LLMSettings(
        api_key=SecretStr(TEST_SECRET),
        model="учебная-модель",
        base_url="https://llm.example.invalid/v1",
        timeout_seconds=7,
    )


def completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_generic_endpoint_headers_timeout_and_locally_validated_intent():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://llm.example.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + TEST_SECRET
        assert request.extensions["timeout"]["read"] == 7
        payload = json.loads(request.content)
        assert payload["model"] == "учебная-модель"
        assert "response_format" not in payload
        assert set(payload) == {"model", "messages"}
        user_data = json.loads(payload["messages"][1]["content"])
        assert user_data == {
            "message": "Красное сухое к стейку",
            "food_categories": ["Мясо и стейки"],
        }
        return completion(
            '{"hard_constraints":{"color":"красное","sweetness":"сухое"},"normalized_food_categories":["Мясо и стейки"]}'
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CompatibleTextClient(settings(), client=http)
        intent = client.extract_preferences("Красное сухое к стейку", ["Мясо и стейки"])
        assert intent.hard_constraints.color == "красное"
        assert intent.normalized_food_categories == ["Мясо и стейки"]
        assert len(requests) == 1


def test_invalid_json_one_retry_then_valid():
    count = [0]

    def handler(request):
        count[0] += 1
        return (
            completion("не JSON")
            if count[0] == 1
            else completion('{"hard_constraints":{"color":"белое"}}')
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert (
            CompatibleTextClient(settings(), client=http)
            .extract_preferences("Белое", [])
            .hard_constraints.color
            == "белое"
        )
    assert count[0] == 2


@pytest.mark.parametrize(
    "response",
    [
        completion("{неверный JSON"),
        completion('{"hard_constraints":{"color":"фиолетовое"}}'),
        completion('{"wine":{"name":"Выдуманная позиция"}}'),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"unsupported": "неподдерживаемый ответ"}),
        httpx.Response(200, text="<html>Ошибка сервиса</html>"),
    ],
)
def test_malformed_responses_bounded_and_safe(response, caplog):
    attempts = []

    def handler(request):
        attempts.append(request)
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMResponseError) as error:
            CompatibleTextClient(settings(), client=http).extract_preferences("Красное", [])
    assert len(attempts) == 2
    assert TEST_SECRET not in str(error.value)
    assert TEST_SECRET not in caplog.text
    assert "Выдуманная позиция" not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503, 307])
def test_http_errors_no_unbounded_retries_no_secret_leak(status, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text=TEST_SECRET, headers={"Location": "https://evil.invalid"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMUnavailable) as error:
            CompatibleTextClient(settings(), client=http).extract_preferences("Красное", [])
    assert len(calls) == 1
    assert TEST_SECRET not in str(error.value)
    assert TEST_SECRET not in caplog.text


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_failures_are_safe(exception, caplog):
    def handler(request):
        raise exception(TEST_SECRET, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMUnavailable) as error:
            CompatibleTextClient(settings(), client=http).extract_preferences("Красное", [])
    assert TEST_SECRET not in str(error.value)
    assert TEST_SECRET not in caplog.text


def test_explanation_request_contains_only_selected_ids_and_reasons():
    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["candidates"] == [
            {
                "wine_id": 42,
                "reasons": [
                    {"reason_id": "food:0", "text": "В каталоге указано сочетание с рыбой."}
                ],
            }
        ]
        return completion('{"explanations":[{"wine_id":42,"reason_ids":["food:0"],"price_rub":1}]}')

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = CompatibleTextClient(settings(), client=http).explain_recommendations(
            "К рыбе",
            [
                ExplanationCandidate(
                    wine_id=42,
                    reasons=[
                        GroundedReason(
                            reason_id="food:0", text="В каталоге указано сочетание с рыбой."
                        )
                    ],
                )
            ],
        )
    assert result.explanations[0].wine_id == 42
    assert "price_rub" not in result.explanations[0].model_dump()


def test_unknown_id_is_not_coerced_from_boolean():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: completion('{"explanations":[{"wine_id":true,"reason_ids":["rating"]}]}')
        )
    ) as http:
        with pytest.raises(LLMResponseError):
            CompatibleTextClient(settings(), client=http).explain_recommendations("Красное", [])


def test_environment_configuration_without_provider_name(monkeypatch):
    assert LLMSettings.from_env() is None
    monkeypatch.setenv("LLM_API_KEY", TEST_SECRET)
    monkeypatch.setenv("LLM_MODEL", "учебная-модель")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.invalid/v1/")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "9")
    configured = LLMSettings.from_env()
    assert configured.base_url == "https://llm.example.invalid/v1"
    assert configured.timeout_seconds == 9
    assert TEST_SECRET not in repr(configured)


@pytest.mark.parametrize(
    "overrides",
    [
        {"LLM_MODEL": ""},
        {"LLM_BASE_URL": "http://llm.example.invalid"},
        {"LLM_BASE_URL": "https://user:password@llm.example.invalid"},
        {"LLM_BASE_URL": "https://llm.example.invalid?key=secret"},
        {"LLM_BASE_URL": "https://llm.example.invalid:ошибка/v1"},
        {"LLM_TIMEOUT_SECONDS": "nan"},
        {"LLM_TIMEOUT_SECONDS": "0"},
        {"LLM_TIMEOUT_SECONDS": "не число"},
    ],
)
def test_invalid_config_is_safe(monkeypatch, overrides):
    monkeypatch.setenv("LLM_API_KEY", TEST_SECRET)
    monkeypatch.setenv("LLM_MODEL", "учебная-модель")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.invalid/v1")
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(LLMConfigurationError) as error:
        LLMSettings.from_env()
    assert TEST_SECRET not in str(error.value)


def test_client_releases_owned_connections():
    client = CompatibleTextClient(settings())
    client.close()
    assert client.client.is_closed


def test_injected_http_client_remains_owned_by_caller():
    with httpx.Client(transport=httpx.MockTransport(lambda _: completion("{}"))) as http:
        CompatibleTextClient(settings(), client=http).close()
        assert not http.is_closed


def test_protocol_validates_positive_and_negative_tastes_independently_of_food():
    def handler(request):
        payload = json.loads(request.content)
        instructions = payload["messages"][0]["content"]
        assert "negative_taste_preferences" in instructions
        assert "свойства соуса" in instructions
        return completion(
            json.dumps(
                {
                    "dish": "утка с ягодным соусом",
                    "normalized_food_categories": [],
                    "taste_preferences": ["насыщенное"],
                    "negative_taste_preferences": ["грубая терпкость"],
                },
                ensure_ascii=False,
            )
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        intent = CompatibleTextClient(settings(), client=http).extract_preferences(
            "К утке хочу насыщенного вина без грубой терпкости", []
        )
    assert intent.dish == "утка с ягодным соусом"
    assert intent.normalized_food_categories == []
    assert intent.taste_preferences == ["насыщенное"]
    assert intent.negative_taste_preferences == ["грубая терпкость"]
