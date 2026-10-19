"""Команда автономной оценки: JSON в stdout, сообщения об ошибках в stderr."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from app.evaluation.runner import run_evaluation


def main() -> int:
    """Запустить оценку без сервера, сетевых запросов и изменения рабочей базы."""
    parser = argparse.ArgumentParser(
        description="Офлайн-оценка подбора на реальном снимке каталога."
    )
    parser.add_argument(
        "--k", type=int, choices=range(1, 6), default=3, help="Максимум рекомендаций."
    )
    parser.add_argument("--output", type=Path, help="Дополнительно сохранить итоговый JSON в файл.")
    args = parser.parse_args()
    try:
        report = run_evaluation(k=args.k)
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        print(output, end="")
    except OSError, ValueError, ValidationError:
        print("Не удалось выполнить оценку: проверьте набор данных и путь отчёта.", file=sys.stderr)
        return 1
    checks = report["personalization_checks"]
    return int(report["scenario_success_rate"] != 1 or checks["passed"] != checks["count"])


if __name__ == "__main__":
    raise SystemExit(main())
