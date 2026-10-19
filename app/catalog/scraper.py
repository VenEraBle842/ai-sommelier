"""Последовательная загрузка каталога с ограничениями и вежливыми паузами."""

import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.catalog.errors import AccessRestricted, FetchError, ParseError
from app.catalog.models import WineData
from app.catalog.parser import parse_catalog, parse_wine
from app.catalog.prices import NoPriceProvider, PriceProvider
from app.catalog.repository import WineRepository
from app.catalog.robots import RobotsPolicy
from app.catalog.urls import BASE_URL

logger = logging.getLogger(__name__)
USER_AGENT = "SvoeVinoCatalogMVP/0.1 (+local catalog synchronization; Python/httpx)"


class CatalogClient:
    """Ограниченные повторы; запреты сайта останавливают загрузку."""

    def __init__(
        self,
        *,
        timeout: float = 20,
        delay: float = 1,
        retries: int = 2,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(delay)
            or delay < 0
            or not 0 <= retries <= 5
        ):
            raise ValueError("Некорректные ограничения сетевой загрузки.")
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.owns_client = client is None
        self.timeout, self.delay, self.retries = timeout, delay, retries
        self.sleep, self.monotonic = sleep, monotonic
        self.last_finished: float | None = None
        self.policy: RobotsPolicy | None = None

    def close(self) -> None:
        """Закрыть собственное соединение; внедрённым клиентом управляет вызывающий код."""
        if self.owns_client:
            self.client.close()

    def _request(self, url: str) -> httpx.Response:
        """Не переходить по перенаправлениям и не повторять запреты доступа."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "vino-svoe.ru":
            raise AccessRestricted("Загрузка с постороннего адреса запрещена.")
        for attempt in range(self.retries + 1):
            if self.last_finished is not None:
                self.sleep(max(0, self.delay - (self.monotonic() - self.last_finished)))
            response = None
            try:
                response = self.client.get(
                    url,
                    timeout=self.timeout,
                    follow_redirects=False,
                    headers={"User-Agent": USER_AGENT},
                )
            except httpx.RequestError:
                if attempt == self.retries:
                    raise FetchError(
                        f"Не удалось получить страницу после {attempt + 1} попыток: {url}"
                    ) from None
            finally:
                self.last_finished = self.monotonic()
            if response is not None:
                if response.status_code in {401, 403, 429}:
                    raise AccessRestricted(
                        f"Сайт ограничил доступ (HTTP {response.status_code}); загрузка остановлена."
                    )
                if 300 <= response.status_code < 400:
                    raise AccessRestricted(
                        "Сайт вернул перенаправление; автоматический переход отключён."
                    )
                if response.status_code not in {408, 500, 502, 503, 504}:
                    return response
                # Retry-After не сокращается: следующий запуск выполняет оператор.
                if response.headers.get("Retry-After"):
                    raise AccessRestricted(
                        "Сайт запросил отложить повтор через Retry-After; загрузка остановлена."
                    )
                if attempt == self.retries:
                    raise FetchError(
                        f"Страница недоступна после {attempt + 1} попыток (HTTP {response.status_code}): {url}"
                    )
            logger.info("Временный сбой загрузки; повтор %s из %s.", attempt + 1, self.retries)
            self.sleep(2**attempt)
        raise FetchError("Исчерпаны попытки загрузки страницы.")

    def _load_policy(self) -> None:
        """Получить robots.txt до обращения к каталогу; при сбое не продолжать."""
        response = self._request(f"{BASE_URL}/robots.txt")
        if response.status_code == 404:
            self.policy = RobotsPolicy("", USER_AGENT)
        elif response.status_code == 200:
            if re.search(r"<(?:!doctype\s+html|html|form|title)\b", response.text, re.IGNORECASE):
                raise AccessRestricted("Вместо robots.txt сайт вернул HTML; загрузка остановлена.")
            self.policy = RobotsPolicy(response.text, USER_AGENT)
        else:
            raise AccessRestricted("Не удалось проверить robots.txt; загрузка остановлена.")
        self.delay = max(self.delay, self.policy.delay)

    def get_html(self, url: str) -> str:
        """Проверить правила сайта, HTTP-статус и признаки блокировки."""
        if self.policy is None:
            self._load_policy()
        assert self.policy is not None
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not self.policy.allows(path):
            raise AccessRestricted(f"robots.txt запрещает загрузку: {url}")
        response = self._request(url)
        if response.status_code != 200:
            raise FetchError(f"Не удалось загрузить страницу (HTTP {response.status_code}): {url}")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise FetchError("Вместо HTML сайт вернул неподдерживаемый тип содержимого.")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text().casefold() if soup.title else ""
        if any(
            value in title
            for value in ("captcha", "капча", "access denied", "just a moment", "доступ ограничен")
        ) or soup.select_one("#captcha, .g-recaptcha, #challenge-form, #cf-challenge-running"):
            raise AccessRestricted("Обнаружена проверка доступа или CAPTCHA; загрузка остановлена.")
        return response.text


@dataclass
class SyncReport:
    """Счётчики одного запуска, включая неудачные карточки."""

    pages: int = 0
    attempted: int = 0
    saved: int = 0
    failed: int = 0


class CatalogSynchronizer:
    """Связать загрузчик, парсер, поставщика цен и локальное хранилище."""

    def __init__(
        self,
        client: CatalogClient,
        repository: WineRepository,
        price_provider: PriceProvider | None = None,
    ):
        self.client = client
        self.repository = repository
        self.price_provider = price_provider if price_provider is not None else NoPriceProvider()

    def run(self, *, pages: int = 1000, limit: int | None = None) -> SyncReport:
        """Обойти подтверждённую пагинацию; лимит ограничивает число попыток карточек."""
        if not 1 <= pages <= 1000 or (limit is not None and limit < 1):
            raise ValueError(
                "Число страниц должно быть от 1 до 1000, лимит карточек — положительным."
            )
        report = SyncReport()
        url: str | None = f"{BASE_URL}/wines"
        seen_urls: set[str] = set()
        page_fingerprints: set[frozenset[str]] = set()
        while url and report.pages < pages:
            page = parse_catalog(self.client.get_html(url), url)
            report.pages += 1
            if not page.wine_urls:
                break
            fingerprint = frozenset(page.wine_urls)
            if fingerprint in page_fingerprints:
                raise ParseError(
                    "Сайт повторил страницу каталога; обход остановлен во избежание цикла."
                )
            page_fingerprints.add(fingerprint)
            for source_url in page.wine_urls:
                if source_url in seen_urls:
                    continue
                seen_urls.add(source_url)
                report.attempted += 1
                try:
                    wine = parse_wine(self.client.get_html(source_url), source_url)
                    values = wine.model_dump()
                    values["price_rub"] = self.price_provider.get_price(wine)
                    self.repository.upsert(WineData.model_validate(values))
                except AccessRestricted:
                    raise
                except (FetchError, ParseError) as exc:
                    report.failed += 1
                    logger.warning("Карточка пропущена: %s Причина: %s", source_url, exc)
                except ValidationError:
                    report.failed += 1
                    logger.warning(
                        "Карточка пропущена: %s Данные не прошли проверку модели.", source_url
                    )
                else:
                    report.saved += 1
                    logger.info("Карточка сохранена: %s", source_url)
                if limit is not None and report.attempted >= limit:
                    return report
            url = page.next_url
        return report
