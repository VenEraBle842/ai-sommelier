"""Самостоятельная команда обновления локального снимка каталога."""

import argparse
import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from sqlalchemy.exc import SQLAlchemyError

from app.catalog.errors import CatalogError
from app.catalog.repository import WineRepository
from app.catalog.scraper import CatalogClient, CatalogSynchronizer
from app.db.database import init_database


class RussianArgumentParser(argparse.ArgumentParser):
    """Справка и ошибки аргументов на русском языке."""

    def error(self, message: str) -> Never:
        """Не выводить непереведённые встроенные сообщения argparse."""
        self.print_usage()
        self.exit(2, "Ошибка: проверьте параметры команды. Справка: --help.\n")

    def format_usage(self) -> str:
        """Перевести стандартный префикс строки использования."""
        return super().format_usage().replace("usage: ", "Использование: ", 1)

    def format_help(self) -> str:
        """Перевести префикс также в развёрнутой справке."""
        return super().format_help().replace("usage: ", "Использование: ", 1)


def positive_int(value: str) -> int:
    """Проверить положительный целочисленный лимит."""
    number = int(value)
    if number <= 0:
        raise ValueError("Значение должно быть положительным.")
    return number


def positive_float(value: str) -> float:
    """Не разрешать нулевые, отрицательные и бесконечные интервалы."""
    number = float(value)
    if not math.isfinite(number) or number < 1:
        raise ValueError("Значение должно быть конечным числом не меньше 1.")
    return number


def make_parser() -> argparse.ArgumentParser:
    """Настроить небольшую русскоязычную CLI без дополнительных библиотек."""
    parser = RussianArgumentParser(
        description="Обновить локальный каталог вин с vino-svoe.ru без LLM.",
        usage="%(prog)s [--pages ЧИСЛО] [--limit ЧИСЛО] [--db ПУТЬ] [--delay СЕКУНДЫ] [--timeout СЕКУНДЫ] [--retries ЧИСЛО]",
        add_help=False,
    )
    parser._positionals.title = "Позиционные параметры"
    parser._optionals.title = "Параметры"
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument(
        "--pages",
        type=positive_int,
        default=1000,
        metavar="ЧИСЛО",
        help="Максимум страниц: от 1 до 1000, по умолчанию 1000.",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        metavar="ЧИСЛО",
        help="Максимум попыток загрузки уникальных карточек.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        metavar="ПУТЬ",
        help="Путь SQLite; иначе WINE_DB_PATH или data/catalog.sqlite3.",
    )
    parser.add_argument(
        "--delay",
        type=positive_float,
        default=1.0,
        metavar="СЕКУНДЫ",
        help="Пауза между запросами, минимум 1 секунда.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=20.0,
        metavar="СЕКУНДЫ",
        help="Тайм-аут сетевых операций, по умолчанию 20 секунд.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        choices=range(6),
        default=2,
        metavar="ЧИСЛО",
        help="Повторы при временном сбое: от 0 до 5, по умолчанию 2.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Вернуть ненулевой код при ошибке, включая частично неудачный запуск."""
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.pages > 1000:
        parser.error("Превышен предел страниц.")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = None
    client = CatalogClient(timeout=args.timeout, delay=args.delay, retries=args.retries)
    try:
        engine = init_database(args.db)
        report = CatalogSynchronizer(client, WineRepository(engine)).run(
            pages=args.pages, limit=args.limit
        )
        print(
            f"Синхронизация завершена. Страниц: {report.pages}; карточек проверено: {report.attempted}; сохранено: {report.saved}; ошибок: {report.failed}."
        )
        print("Цена отсутствует без доверенного поставщика; фильтрация по бюджету пока недоступна.")
        return 1 if report.failed else 0
    except CatalogError as exc:
        logging.error("Синхронизация остановлена: %s Уже сохранённые записи остаются в базе.", exc)
        return 1
    except OSError, SQLAlchemyError:
        logging.error("Не удалось создать или обновить SQLite. Проверьте путь и права доступа.")
        return 1
    except KeyboardInterrupt:
        logging.warning("Синхронизация прервана. Уже сохранённые записи остаются в базе.")
        return 130
    finally:
        client.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
