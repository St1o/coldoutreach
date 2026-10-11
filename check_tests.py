"""Проверка тестов у владельца: правый клик на файле -> Run 'check_tests'. Печатает коротко и по-русски, какие тесты упали и почему.

Зачем отдельная программа: `python -m unittest` в PowerShell ломает кириллицу, а вывод самих тестов длинный. Здесь вывод тестов
спрятан, остаётся итог и список упавших с последней строкой ошибки. Пароли, ключи и адреса почты в вывод не попадают
(значения переменных окружения с PASSWORD, TOKEN, KEY и адреса вида a***@mail.ru скрыты). Сами файлы local.env и settings.json
не читаются: печатается только то, есть ли они, и какие переменные окружения заданы ещё до тестов (только имена).
Код выхода 0 - всё хорошо, 1 - есть упавшие тесты.
"""
import contextlib
import io
import os
import platform
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET_WORDS = ("PASSWORD", "TOKEN", "KEY")
WATCHED_PREFIXES = ("MAIL", "OUTREACH", "SURVEY", "LEADS", "CHECKO", "HOST", "PORT")
MAX_SHOWN = 15
MAX_LINE = 300


def secret_values(environ) -> list:
    """Значения переменных окружения, похожих на секреты: их из вывода убираем (длиннее сначала, чтобы не оставить хвост)."""
    values = {value for name, value in environ.items() if value and len(value) >= 4 and any(w in name.upper() for w in SECRET_WORDS)}
    return sorted(values, key=len, reverse=True)


def scrub(text: str, secrets=()) -> str:
    from mailing.config import redact
    for value in secrets:
        text = text.replace(value, "***")
    return redact(text)


def last_lines(traceback_text: str, count: int = 2) -> str:
    """Последние строки трассировки (сама ошибка), в одну строку и не длиннее MAX_LINE знаков."""
    lines = [line.strip() for line in traceback_text.strip().splitlines() if line.strip()]
    joined = " | ".join(lines[-count:])
    return joined if len(joined) <= MAX_LINE else joined[:MAX_LINE] + "..."


def run_suite(suite) -> tuple:
    """Запускает тесты с молчащим выводом. Возвращает (сколько запущено, [(вид, название, последние строки ошибки)])."""
    sink = io.StringIO()
    runner = unittest.TextTestRunner(stream=sink, verbosity=0)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = runner.run(suite)
    problems = [("FAIL", str(test), last_lines(tb)) for test, tb in result.failures]
    problems += [("ERROR", str(test), last_lines(tb)) for test, tb in result.errors]
    return result.testsRun, problems


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    os.chdir(HERE)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    secrets = secret_values(os.environ)
    print(f"Python {platform.python_version()}, {platform.system()} {platform.release()}; папка проекта: {HERE}")
    print("Файлы в корне: local.env - " + ("есть" if os.path.exists(os.path.join(HERE, "local.env")) else "нет")
          + ", settings.json - " + ("есть" if os.path.exists(os.path.join(HERE, "settings.json")) else "нет") + " (содержимое не читается).")
    watched = sorted(name for name in os.environ if name.upper().startswith(WATCHED_PREFIXES))
    print("Переменные окружения, заданные ещё до тестов (только имена): " + (", ".join(watched) or "нет"))
    print("Запускаю тесты, около минуты...")
    suite = unittest.defaultTestLoader.discover(HERE)
    ran, problems = run_suite(suite)
    print(f"\nЗапущено тестов: {ran}. Упало: {len(problems)}.")
    for kind, name, message in problems[:MAX_SHOWN]:
        print(f"\n{kind}: {scrub(name, secrets)}\n    {scrub(message, secrets)}")
    if len(problems) > MAX_SHOWN:
        print(f"\n... и ещё {len(problems) - MAX_SHOWN} (показаны первые {MAX_SHOWN}).")
    print("\nИТОГ: " + ("всё хорошо." if not problems else "есть упавшие тесты, пришлите этот вывод."))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
