"""Проверка адресов источника и единый ключ карточки."""

import re
from urllib.parse import urljoin, urlsplit

from app.catalog.errors import ParseError

BASE_URL = "https://vino-svoe.ru"


def wine_url(value: str, base: str = BASE_URL) -> str:
    """Вернуть канонический адрес карточки только с разрешённого сайта."""
    url = urlsplit(urljoin(base, value))
    if (
        url.scheme != "https"
        or url.netloc != "vino-svoe.ru"
        or not re.fullmatch(r"/wines/[a-z0-9][a-z0-9_-]*/?", url.path)
    ):
        raise ParseError("Адрес не является карточкой вина на vino-svoe.ru.")
    return BASE_URL + url.path.rstrip("/")
