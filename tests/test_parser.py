"""Проверки данных источника и отказа от выдумывания отсутствующих полей."""

from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from conftest import FIXTURES, MERLO_URL

from app.catalog.errors import ParseError
from app.catalog.parser import parse_catalog, parse_wine


def test_real_merlo_fields(merlo):
    assert merlo.name == "Мерло. Дрозд"
    assert merlo.slug == "merlo-drozd"
    assert merlo.producer == "ДРОЗД. Вино&Виноградник"
    assert merlo.region == "Кубань"
    assert merlo.grapes == ["Мерло"]
    assert merlo.color == "красное"
    assert merlo.sweetness == "сухое"
    assert merlo.source_category == "Красное сухое"
    assert merlo.source_color == "Рубиновый"
    assert merlo.serving_temperature == "14-16°C"
    assert merlo.alcohol == "13-14%"
    assert merlo.food_pairings == ["Мясное ассорти", "Мясо и стейки", "Сыры"]
    assert merlo.description.startswith("Вино рубинового цвета с фиолетовым отливом")
    assert "выдержка в новых бочках из французского дуба" in merlo.description
    assert merlo.rating == Decimal("5")
    assert merlo.source_url == MERLO_URL
    assert merlo.image_url.endswith(
        "photo_2025_12_15_16_20_30_no_bg_preview_carve_photos_468caad242.webp"
    )
    assert merlo.price_rub is None
    assert merlo.scraped_at.utcoffset().total_seconds() == 0


def test_real_shenen_without_rating():
    wine = parse_wine(
        (FIXTURES / "shenen.html").read_text(), "https://vino-svoe.ru/wines/shenen-blan-beton-drozd"
    )
    assert wine.name == "Шенен Блан. Бетон. Дрозд"
    assert wine.grapes == ["Шенен Блан"]
    assert wine.color == "белое"
    assert wine.rating is None
    assert wine.serving_temperature == "10-12°C"
    assert wine.alcohol == "12%"
    assert wine.food_pairings == ["Легкие закуски", "Сыры"]
    assert wine.description
    assert wine.price_rub is None


def test_real_blend_grapes():
    wine = parse_wine(
        (FIXTURES / "blend.html").read_text(),
        "https://vino-svoe.ru/wines/suhoe-beloe-traminer-pino-blan-sh-ardone",
    )
    assert wine.grapes == ["Пино Блан", "Траминер", "Шардоне"]
    assert wine.producer == "Усадьба Маскага"
    assert wine.alcohol == "11-13%"


def test_incomplete_card_leaves_all_optional_values_empty():
    wine = parse_wine((FIXTURES / "incomplete.html").read_text(), MERLO_URL)
    assert wine.name == "Учебная неполная карточка"
    assert wine.grapes == wine.food_pairings == []
    for field in (
        "producer",
        "region",
        "color",
        "sweetness",
        "source_category",
        "source_color",
        "serving_temperature",
        "alcohol",
        "description",
        "rating",
        "image_url",
        "price_rub",
    ):
        assert getattr(wine, field) is None


def test_missing_name_is_rejected(merlo_html):
    soup = BeautifulSoup(merlo_html, "html.parser")
    soup.h1.decompose()
    with pytest.raises(ParseError, match="название"):
        parse_wine(str(soup), MERLO_URL)


@pytest.mark.parametrize(
    "html", ["", "<h1>Ошибка сервера</h1>", (FIXTURES / "malformed.html").read_text()]
)
def test_malformed_page_is_not_a_wine(html):
    with pytest.raises(ParseError):
        parse_wine(html, MERLO_URL)


def test_recoverable_broken_html():
    wine = parse_wine(
        '<div class="wine-page"><h1 class="wine-main-title-block__title">Учебная карточка</h1>',
        MERLO_URL,
    )
    assert wine.name == "Учебная карточка"
    assert wine.price_rub is None


def test_related_wine_cannot_supply_missing_fields():
    html = (
        (FIXTURES / "incomplete.html").read_text()
        + '<aside><a class="wine-main-title-block__manufacturer">Чужой производитель</a><div class="wine-page__description">Чужое описание</div></aside>'
    )
    wine = parse_wine(html, MERLO_URL)
    assert wine.producer is None
    assert wine.description is None


def test_price_is_not_extracted_even_from_explicit_price_text(merlo_html):
    html = merlo_html.replace(
        'class="wine-page__description"', 'data-price="999" class="wine-page__description"'
    )
    html += '<div itemprop="price">999 ₽</div>'
    assert parse_wine(html, MERLO_URL).price_rub is None


def test_real_catalog_pagination_and_urls():
    page = parse_catalog((FIXTURES / "catalog.html").read_text(), "https://vino-svoe.ru/wines")
    assert len(page.wine_urls) == 16
    assert page.wine_urls[0] == MERLO_URL
    assert page.next_url == "https://vino-svoe.ru/wines?page=2"


def test_catalog_excludes_foreign_urls_duplicates_and_outside_links():
    html = '<div class="wines-list__grid"><a class="wine-item" href="/wines/merlo-drozd?utm=1"></a><a class="wine-item" href="/wines/merlo-drozd/#x"></a><a class="wine-item" href="https://evil.test/wines/other"></a></div><a class="wine-item" href="/wines/other"></a>'
    page = parse_catalog(html, "https://vino-svoe.ru/wines")
    assert page.wine_urls == [MERLO_URL]
    assert page.next_url is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/wines/merlo-drozd",
        "http://vino-svoe.ru/wines/merlo-drozd",
        "https://vino-svoe.ru/wineries/drozd",
        "https://vino-svoe.ru/wines/../private",
    ],
)
def test_invalid_source_url_is_rejected(merlo_html, url):
    with pytest.raises(ParseError):
        parse_wine(merlo_html, url)


def test_source_url_is_canonical(merlo_html):
    assert parse_wine(merlo_html, MERLO_URL + "/?utm=1#x").source_url == MERLO_URL


def test_unknown_category_is_preserved_without_guessing(merlo_html):
    wine = parse_wine(merlo_html.replace("Красное сухое", "Неизвестная категория"), MERLO_URL)
    assert wine.source_category == "Неизвестная категория"
    assert wine.color is wine.sweetness is None


@pytest.mark.parametrize(
    "rating", ["Народный рейтинг 9", "Оценка отсутствует", "Народный рейтинг nan"]
)
def test_unreliable_rating_is_empty(merlo_html, rating):
    assert parse_wine(merlo_html.replace("Народный рейтинг 5", rating), MERLO_URL).rating is None


def test_missing_catalog_grid_is_a_failure():
    with pytest.raises(ParseError, match="сетка"):
        parse_catalog("<html>Ошибка сайта</html>", "https://vino-svoe.ru/wines")
