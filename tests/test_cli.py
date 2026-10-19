"""Параметры команды, сообщения и коды завершения без настоящей сети."""

import pytest

from app.catalog import sync
from app.catalog.errors import AccessRestricted
from app.catalog.scraper import SyncReport


def test_help_in_russian(capsys):
    with pytest.raises(SystemExit) as error:
        sync.main(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "Обновить локальный каталог" in output
    assert "Параметры" in output
    assert "options:" not in output
    assert "usage:" not in output


@pytest.mark.parametrize(
    "args",
    [
        ["--pages", "0"],
        ["--pages", "1001"],
        ["--limit", "-2"],
        ["--delay", "0"],
        ["--delay", "nan"],
        ["--timeout", "inf"],
        ["--retries", "6"],
        ["--unknown"],
    ],
)
def test_invalid_arguments_are_russian(args, capsys):
    with pytest.raises(SystemExit) as error:
        sync.main(args)
    assert error.value.code == 2
    assert "Ошибка: проверьте параметры команды" in capsys.readouterr().err


@pytest.mark.parametrize("failures, expected", [(0, 0), (1, 1)])
def test_cli_report_and_exit_code(monkeypatch, tmp_path, capsys, failures, expected):
    monkeypatch.setattr(
        sync.CatalogSynchronizer,
        "run",
        lambda self, **kwargs: SyncReport(
            pages=1, attempted=1, saved=1 - failures, failed=failures
        ),
    )
    assert (
        sync.main(["--pages", "1", "--limit", "1", "--db", str(tmp_path / "catalog.sqlite3")])
        == expected
    )
    assert "Синхронизация завершена" in capsys.readouterr().out


def test_access_error_is_nonzero(monkeypatch, tmp_path, caplog):
    def denied(self, **kwargs):
        raise AccessRestricted("Доступ запрещён.")

    monkeypatch.setattr(sync.CatalogSynchronizer, "run", denied)
    assert sync.main(["--db", str(tmp_path / "catalog.sqlite3")]) == 1
    assert "Синхронизация остановлена" in caplog.text
