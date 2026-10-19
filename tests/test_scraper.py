"""Синхронизация и сетевые ошибки проверяются через локальный HTTP-транспорт."""

from decimal import Decimal

import httpx
import pytest
from conftest import FIXTURES, MERLO_URL

from app.catalog.errors import AccessRestricted, FetchError, ParseError
from app.catalog.scraper import USER_AGENT, CatalogClient, CatalogSynchronizer

BASE = "https://vino-svoe.ru"
SHENEN_URL = BASE + "/wines/shenen-blan-beton-drozd"


def html_response(html):
    return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})


def listing(urls, next_page=None):
    """Учебная сетка для проверки обхода; не используется в рабочем каталоге."""
    cards = "".join(f'<a class="wine-item" href="{url}"></a>' for url in urls)
    next_link = (
        f'<div class="core-pagination"><a href="/wines?page={next_page}">Далее</a></div>'
        if next_page
        else ""
    )
    return f'<div class="wines-list__grid">{cards}</div>{next_link}'


@pytest.fixture
def make_client():
    clients = []

    def make(handler, **kwargs):
        def router(request):
            assert request.headers["User-Agent"] == USER_AGENT
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    200, text="User-agent: *\nDisallow: /api/\nDisallow: *?*\nAllow: *page="
                )
            return handler(request)

        http = httpx.Client(transport=httpx.MockTransport(router))
        clients.append(http)
        return CatalogClient(client=http, delay=0, sleep=lambda _: None, **kwargs)

    yield make
    for client in clients:
        client.close()


def test_repeated_real_catalog_sync_no_duplicates(make_client, repository, merlo_html):
    def handler(request):
        if request.url.path == "/wines":
            return html_response((FIXTURES / "catalog.html").read_text())
        if str(request.url) == MERLO_URL:
            return html_response(merlo_html)
        if str(request.url) == SHENEN_URL:
            return html_response((FIXTURES / "shenen.html").read_text())
        raise AssertionError("Лимит должен исключать загрузку других карточек.")

    sync = CatalogSynchronizer(make_client(handler), repository)
    first = sync.run(pages=1, limit=2)
    ids = [wine.id for wine in repository.list_wines()]
    second = sync.run(pages=1, limit=2)
    assert first.saved == second.saved == 2
    assert first.failed == second.failed == 0
    assert repository.count() == 2
    assert [wine.id for wine in repository.list_wines()] == ids
    assert {wine.source_url for wine in repository.list_wines()} == {MERLO_URL, SHENEN_URL}
    assert all(wine.price_rub is None for wine in repository.list_wines())


def test_pagination_and_deduplication(make_client, repository, merlo_html):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if request.url.path == "/wines":
            if request.url.params.get("page") == "2":
                return html_response(listing([MERLO_URL, SHENEN_URL]))
            return html_response(listing([MERLO_URL, MERLO_URL], next_page=2))
        return html_response(
            merlo_html if str(request.url) == MERLO_URL else (FIXTURES / "shenen.html").read_text()
        )

    report = CatalogSynchronizer(make_client(handler), repository).run(pages=3)
    assert report.pages == 2
    assert report.attempted == report.saved == 2
    assert visited.count(MERLO_URL) == 1


def test_page_limit(make_client, repository, merlo_html):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return html_response(
            listing([MERLO_URL], 2) if request.url.path == "/wines" else merlo_html
        )

    report = CatalogSynchronizer(make_client(handler), repository).run(pages=1)
    assert report.pages == report.saved == 1
    assert BASE + "/wines?page=2" not in visited


def test_repeated_page_stops_loop(make_client, repository, merlo_html):
    def handler(request):
        return html_response(
            listing([MERLO_URL], int(request.url.params.get("page", "1")) + 1)
            if request.url.path == "/wines"
            else merlo_html
        )

    with pytest.raises(ParseError, match="повторил"):
        CatalogSynchronizer(make_client(handler), repository).run()
    assert repository.count() == 1


def test_bad_card_skipped_and_attempt_limit_counts_failures(make_client, repository):
    def handler(request):
        if request.url.path == "/wines":
            return html_response(listing([MERLO_URL, SHENEN_URL]))
        return html_response("<h1>Ошибка страницы</h1>")

    report = CatalogSynchronizer(make_client(handler), repository).run(limit=1)
    assert report.failed == report.attempted == 1
    assert report.saved == repository.count() == 0


def test_failed_card_does_not_block_valid_card(make_client, repository, merlo_html):
    def handler(request):
        if request.url.path == "/wines":
            return html_response(listing([SHENEN_URL, MERLO_URL]))
        return html_response(merlo_html) if str(request.url) == MERLO_URL else httpx.Response(404)

    report = CatalogSynchronizer(make_client(handler), repository).run()
    assert report.failed == report.saved == 1
    assert repository.list_wines()[0].source_url == MERLO_URL


def test_price_provider_is_optional_and_separate(make_client, repository, merlo_html):
    class TrustedTestProvider:
        """Искусственная цена разрешена только в этом изолированном тесте."""

        def get_price(self, wine):
            assert wine.source_url == MERLO_URL
            return Decimal("1500.25")

    def handler(request):
        return html_response(listing([MERLO_URL]) if request.url.path == "/wines" else merlo_html)

    client = make_client(handler)
    CatalogSynchronizer(client, repository, TrustedTestProvider()).run()
    assert repository.list_wines()[0].price_rub == Decimal("1500.25")
    CatalogSynchronizer(client, repository).run()
    assert repository.list_wines()[0].price_rub is None


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_transient_http_failure_retried(make_client, merlo_html, status):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status) if len(attempts) == 1 else html_response(merlo_html)

    assert make_client(handler).get_html(MERLO_URL) == merlo_html
    assert len(attempts) == 2


def test_network_error_retried_and_bounded(make_client):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("Учебный сетевой сбой", request=request)

    with pytest.raises(FetchError, match="3 попыток"):
        make_client(handler, retries=2).get_html(MERLO_URL)
    assert len(attempts) == 3


def test_timeout_recovery(make_client, merlo_html):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("Учебный тайм-аут", request=request)
        assert request.extensions["timeout"]["read"] == 7
        return html_response(merlo_html)

    make_client(handler, timeout=7).get_html(MERLO_URL)
    assert len(attempts) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 302])
def test_access_restrictions_never_retried(make_client, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/"})

    with pytest.raises(AccessRestricted):
        make_client(handler).get_html(MERLO_URL)
    assert len(calls) == 1


def test_retry_after_stops_entire_sync(make_client, repository):
    def handler(request):
        if request.url.path == "/wines":
            return html_response(listing([MERLO_URL, SHENEN_URL]))
        assert str(request.url) == MERLO_URL
        return httpx.Response(503, headers={"Retry-After": "300"})

    with pytest.raises(AccessRestricted, match="Retry-After"):
        CatalogSynchronizer(make_client(handler), repository).run()


@pytest.mark.parametrize(
    "html",
    [
        "<title>CAPTCHA</title>",
        '<form id="challenge-form"></form>',
        "<title>Доступ ограничен</title>",
    ],
)
def test_challenge_stops_sync(make_client, html):
    with pytest.raises(AccessRestricted):
        make_client(lambda _: html_response(html)).get_html(MERLO_URL)


def test_non_html_rejected(make_client):
    with pytest.raises(FetchError, match="тип содержимого"):
        make_client(lambda _: httpx.Response(200, json={"ошибка": "нет страницы"})).get_html(
            MERLO_URL
        )


def test_rate_delay_and_robots_crawl_delay(merlo_html):
    now = [0.0]
    times = []

    def sleep(seconds):
        now[0] += seconds

    def handler(request):
        times.append(now[0])
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 3")
        return html_response(merlo_html)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CatalogClient(client=http, delay=1, sleep=sleep, monotonic=lambda: now[0])
        client.get_html(MERLO_URL)
        client.get_html(MERLO_URL)
    assert times == [0, 3, 6]


def test_robots_disallow_blocks_catalog_request():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /wines")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AccessRestricted, match="robots.txt"):
            CatalogClient(client=http, delay=0).get_html(MERLO_URL)
    assert paths == ["/robots.txt"]


def test_robots_failure_does_not_allow_scraping():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as http:
        with pytest.raises(FetchError):
            CatalogClient(client=http, delay=0, retries=0).get_html(MERLO_URL)


def test_html_instead_of_robots_stops_scraping():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return html_response("<html><title>Проверка доступа</title></html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AccessRestricted, match="Вместо robots.txt"):
            CatalogClient(client=http, delay=0).get_html(MERLO_URL)
    assert paths == ["/robots.txt"]


def test_empty_catalog_terminates(make_client, repository):
    report = CatalogSynchronizer(
        make_client(lambda _: html_response(listing([]))), repository
    ).run()
    assert report.pages == 1
    assert report.attempted == repository.count() == 0
