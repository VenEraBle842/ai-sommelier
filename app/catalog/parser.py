"""Детерминированный разбор HTML vino-svoe.ru без выполнения JavaScript."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from app.catalog.errors import ParseError
from app.catalog.models import WineData
from app.catalog.urls import BASE_URL, wine_url


def clean(value: str) -> str:
    """Убрать лишние пробелы, в том числе неразрывные."""
    return " ".join(value.split())


def text(node: Tag | None) -> str | None:
    """Получить текст элемента, не подменяя отсутствие пустой строкой."""
    return clean(node.get_text(" ", strip=True)) or None if node else None


def unique(values: list[str]) -> list[str]:
    """Сохранить порядок и убрать повторы из мобильной и обычной разметки."""
    return list(dict.fromkeys(values))


@dataclass(frozen=True)
class CatalogPage:
    """Ссылки текущей страницы и подтверждённая ссылка на следующую."""

    wine_urls: list[str]
    next_url: str | None


def parse_catalog(html: str, source_url: str) -> CatalogPage:
    """Читать только сетку каталога, исключая меню и похожие вина."""
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.select_one(".wines-list__grid")
    if grid is None:
        raise ParseError("Не найдена сетка каталога: возможно, разметка сайта изменилась.")
    links: list[str] = []
    for link in grid.select("a.wine-item[href]"):
        try:
            links.append(wine_url(str(link["href"]), source_url))
        except ParseError:
            continue
    if not links and (grid.find("a") or grid.get_text(strip=True)):
        raise ParseError("В непустой сетке каталога не найдены допустимые ссылки на карточки.")
    current_page = int(parse_qs(urlsplit(source_url).query).get("page", ["1"])[0])
    next_url = None
    for link in soup.select(".core-pagination a[href]"):
        url = urlsplit(urljoin(source_url, str(link["href"])))
        if url.scheme != "https" or url.netloc != "vino-svoe.ru" or url.path != "/wines":
            continue
        if parse_qs(url.query) == {"page": [str(current_page + 1)]}:
            next_url = f"{BASE_URL}/wines?page={current_page + 1}"
            break
    return CatalogPage(unique(links), next_url)


def parse_wine(html: str, source_url: str) -> WineData:
    """Разобрать только текущую карточку; отсутствующие поля не восстанавливать."""
    source_url = wine_url(source_url)
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".wine-page")
    name = text(root.select_one("h1.wine-main-title-block__title")) if root else None
    if root is None or not name:
        raise ParseError("Не найдены карточка и название вина: запись не сохранена.")

    details: dict[str, list[str]] = {}
    for block in root.select(".wine-detail-info__detail-content"):
        label = text(block.select_one(".wine-detail-info__detail-label"))
        values = [
            v for node in block.select(".wine-detail-info__detail-value") if (v := text(node))
        ]
        if label and values:
            details.setdefault(label.casefold(), values)
    category_values = details.get("категория и цвет", [])
    category = category_values[0] if category_values else None
    normalized = category.casefold().replace("ё", "е") if category else ""
    color = next(
        (
            v
            for v in ("белое", "красное", "розовое", "оранжевое")
            if re.search(rf"\b{v}\b", normalized)
        ),
        None,
    )
    sweetness = next(
        (
            v
            for v in ("экстра брют", "брют", "полусладкое", "полусухое", "сладкое", "сухое")
            if re.search(rf"\b{v}\b", normalized)
        ),
        None,
    )

    cards: dict[str, str] = {}
    for block in root.select(
        ".wine-hero-block__card-content, .wine-mobile-hero-block__card-content"
    ):
        label = text(
            block.select_one(".wine-hero-block__card-label, .wine-mobile-hero-block__card-label")
        )
        value = text(
            block.select_one(".wine-hero-block__card-value, .wine-mobile-hero-block__card-value")
        )
        if label and value:
            cards.setdefault(label.casefold(), value)

    grapes = unique(
        [
            clean(v)
            for value in details.get("сорт винограда", details.get("сорта винограда", []))
            for v in re.split(r"[,;\n]", value)
            if clean(v)
        ]
    )
    rating_text = text(root.select_one(".wine-main-title-block__rating-text"))
    rating = None
    if rating_text and (
        match := re.fullmatch(r"Народный рейтинг\s+(\d+(?:[.,]\d+)?)", rating_text)
    ):
        try:
            number = Decimal(match[1].replace(",", "."))
            if 0 <= number <= 5:
                rating = number
        except InvalidOperation:
            pass

    image = root.select_one("img.wine-hero-block__bottle, img.wine-mobile-hero-block__bottle")
    image_url = None
    if image and image.get("src"):
        candidate = urljoin(source_url, str(image["src"]))
        if urlsplit(candidate).scheme in {"http", "https"}:
            image_url = candidate

    return WineData(
        slug=source_url.rsplit("/", 1)[-1],
        name=name,
        producer=text(root.select_one(".wine-main-title-block__manufacturer")),
        region=next(iter(details.get("регион", [])), None),
        grapes=grapes,
        color=color,
        sweetness=sweetness,
        source_category=category,
        source_color=category_values[1] if len(category_values) > 1 else None,
        serving_temperature=cards.get("температура подачи"),
        alcohol=cards.get("крепость вина"),
        food_pairings=unique(
            [v for node in root.select(".wine-dish-item__name") if (v := text(node))]
        ),
        description=text(root.select_one(".wine-page__description")),
        rating=rating,
        source_url=source_url,
        image_url=image_url,
    )
