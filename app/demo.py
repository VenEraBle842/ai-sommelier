"""Явный запуск демонстрации на небольшом проверенном реальном снимке."""

import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from app.api.main import create_app
from app.catalog.errors import CatalogError
from app.catalog.repository import WineRepository
from app.catalog.sync import RussianArgumentParser
from app.db.database import init_database
from app.evaluation.runner import load_snapshot

LLM_ENVIRONMENT = ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_TIMEOUT_SECONDS")


def prepare_demo(path: Path) -> int:
    """Наполнить только пустую базу реальными карточками; существующие данные сохранить."""
    engine = init_database(path)
    try:
        repository = WineRepository(engine)
        if repository.count() == 0:
            load_snapshot(repository)
        return repository.count()
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Создать отдельную SQLite и запустить тот же API на локальном интерфейсе."""
    parser = RussianArgumentParser(
        description="Запустить винный гид на трёх реальных сохранённых карточках.",
        usage="%(prog)s [--db ПУТЬ] [--port ПОРТ] [--offline]",
        add_help=False,
    )
    parser._positionals.title = "Позиционные параметры"
    parser._optionals.title = "Параметры"
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/demo.sqlite3"),
        metavar="ПУТЬ",
        help="Отдельная база демонстрации; по умолчанию data/demo.sqlite3.",
    )
    parser.add_argument(
        "--port", type=int, default=8000, metavar="ПОРТ", help="Порт, по умолчанию 8000."
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Отключить внешний LLM независимо от окружения; использовать ограниченный разбор.",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Укажите допустимый порт.")
    previous = {key: os.environ.get(key) for key in LLM_ENVIRONMENT}
    try:
        count = prepare_demo(args.db)
        if args.offline:
            for key in LLM_ENVIRONMENT:
                os.environ.pop(key, None)
        print(f"Вин в демонстрационном каталоге: {count}. База: {args.db}.", flush=True)
        print(f"Откройте http://127.0.0.1:{args.port}. Для остановки нажмите Ctrl+C.", flush=True)
        if args.offline:
            print(
                "Внешняя модель отключена; доступен ограниченный разбор русских запросов.",
                flush=True,
            )
        uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port, log_level="warning")
        return 0
    except OSError, ValueError, SQLAlchemyError, CatalogError:
        print("Не удалось запустить демонстрацию. Проверьте снимок, путь базы и свободный порт.")
        return 1
    except KeyboardInterrupt:
        print("Демонстрация остановлена.")
        return 130
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
