"""Новые организации за день -> таблица: название, ФИО директора, почта.

Алгоритм:
    1. Со страницы https://checko.ru/company/updates собрать ИНН организаций,
       зарегистрированных в нужный день (checko_updates).
    2. Для каждого ИНН найти организацию на egrul.nalog.ru и скачать выписку (egrul_inn_search).
    3. Открыть выписку (PDF), взять ФИО руководителя и почту (egrul_pdf).
    4. В таблицу попадают только организации с почтой.

Результат - CSV (открывается в Excel) и служебный файл *.state.json рядом: в нём статус
каждой организации. Он сохраняется после каждой организации, поэтому при остановке
(капча, обрыв) готовое не пропадает, а повторный запуск продолжает с того же места.
Организации без почты второй раз не проверяются; ошибки и «ещё нет в ЕГРЮЛ» - проверяются.

Запуск:
    python leads.py                          # сегодня (по Москве)
    python leads.py --date 2026-10-09 --limit 50
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from checko_api import (
    CheckoApiError, CheckoApiNotFound, CheckoApiQuotaExceeded, parse_company,
)
from checko_updates import CheckoBlocked, CheckoClient, CheckoError
from egrul_inn_search import EgrulCaptchaRequired, EgrulClient, EgrulError
from egrul_pdf import ExtractError, parse_extract, pdf_to_text

FIELDS = ["Название", "ФИО директора", "Почта"]
OKVED_FIELD = "Основной ОКВЭД"      # колонка есть, только если данные из API Checko (выписки ФНС ОКВЭД не разбирают)

OK = "ok"                # есть почта - идёт в таблицу
NO_EMAIL = "no_email"    # выписка получена, почты нет - в таблицу не идёт и повторно не проверяется
RETRY = "retry"          # ошибка или ещё нет в ЕГРЮЛ - проверим при следующем запуске


def today_moscow() -> date:
    return datetime.now(timezone(timedelta(hours=3))).date()


def director_from_search(raw: str) -> str:
    """Руководитель из строки поиска egrul («Должность: ФИО») - запасной вариант."""
    return raw.split(":", 1)[-1].strip() if raw else ""


def process_company(client: EgrulClient, company) -> dict:
    """Одна организация -> запись состояния. Капча пробрасывается наверх."""
    entry = {"status": RETRY, "name": company.name, "director": "", "email": "", "note": ""}
    try:
        records = client.search_by_inn(company.inn)
        if not records:
            entry["note"] = "нет в ЕГРЮЛ (возможно, ещё не обновился)"
            return entry
        record = max(records, key=lambda r: r.is_active)
        entry["name"] = record.short_name or record.name or company.name
        pdf = client.download_extract(record)
    except EgrulCaptchaRequired:
        raise
    except EgrulError as exc:
        entry["note"] = f"ошибка: {exc}"[:200]
        return entry

    try:
        data = parse_extract(pdf_to_text(pdf))
    except ExtractError as exc:
        entry["note"] = f"ошибка разбора выписки: {exc}"[:200]
        return entry

    entry["email"] = data.email
    entry["director"] = data.director or director_from_search(record.director)
    entry["status"] = OK if data.email else NO_EMAIL
    return entry


def process_company_api(api, company) -> dict:
    """Одна организация через API Checko -> запись состояния. Исчерпанный лимит пробрасывается."""
    entry = {"status": RETRY, "name": company.name, "director": "", "okved": "", "email": "", "note": ""}
    try:
        info = parse_company(api.company(company.inn))
    except CheckoApiQuotaExceeded:
        raise
    except CheckoApiNotFound:
        entry["note"] = "нет в Checko (возможно, ещё не появилась)"
        return entry
    except CheckoApiError as exc:
        entry["note"] = f"ошибка: {exc}"[:200]
        return entry

    entry["name"] = company.name or info.name      # название из списка Checko; из API - только если в списке его нет
    entry["director"] = info.director
    entry["okved"] = info.okved
    entry["email"] = info.email
    entry["status"] = OK if info.email else NO_EMAIL
    notes = []
    if info.email and not info.email_from_register:
        notes.append("почта из контактов Checko, не из ЕГРЮЛ")
    if not info.okved:
        notes.append("ОКВЭД не найден в ответе API")
    entry["note"] = "; ".join(notes)
    return entry


def state_path(out: str) -> str:
    return os.path.splitext(out)[0] + ".state.json"


def _write_atomic(path: str, write) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".part"
    write(tmp)
    os.replace(tmp, path)


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def save_results(out: str, state: dict) -> None:
    """Служебный файл и таблица (только организации с почтой) перезаписываются целиком."""
    def write_state(tmp):
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)

    with_okved = any("okved" in entry for entry in state.values())

    def write_csv(tmp):
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:    # BOM: Excel читает кириллицу
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(FIELDS + ([OKVED_FIELD] if with_okved else []))
            for entry in state.values():
                if entry["status"] == OK:
                    row = [entry["name"], entry["director"], entry["email"]]
                    writer.writerow(row + ([entry.get("okved", "")] if with_okved else []))

    _write_atomic(state_path(out), write_state)
    _write_atomic(out, write_csv)


def count(state: dict, status: str) -> int:
    return sum(1 for entry in state.values() if entry["status"] == status)


WEB_HEADER = FIELDS + [OKVED_FIELD]


def table_rows(state: dict) -> list:
    """Строки таблицы для сайта и Excel-файла: организации с почтой, в порядке проверки."""
    return [[entry["name"], entry["director"], entry["email"], entry.get("okved", "")]
            for entry in state.values() if entry["status"] == OK]


def _wait(seconds: float, should_stop, sleep) -> bool:
    """Ждёт, проверяя каждую секунду, не попросили ли остановиться. True - остановили."""
    remaining = seconds
    while remaining > 0:
        step = min(1.0, remaining)
        sleep(step)
        remaining -= step
        if should_stop():
            return True
    return should_stop()


def run(target: date, out: str, limit: int = 0, pause: float = 2.0, max_pages: int = 25,
        checko=None, egrul=None, log=print, captcha_wait: float = 0.0, captcha_retries: int = 6,
        should_stop=lambda: False, sleep=time.sleep, max_errors_in_a_row: int = 5,
        process=None) -> int:
    """Возвращает код выхода: 0 - готово, 2 - ФНС потребовала капчу, 3 - Checko недоступен,
    4 - остановлено пользователем, 5 - слишком много ошибок подряд (ФНС отвечает не как обычно:
    дальше стучаться бессмысленно), 6 - исчерпан лимит API Checko.

    process(company) -> запись состояния: по умолчанию выписки ФНС (egrul), можно подставить API.

    captcha_wait > 0: при капче ждать столько секунд и пробовать ту же организацию снова
    (не больше captcha_retries раз подряд); пауза - это ожидание, а не обход ограничения."""
    checko = checko or CheckoClient()
    egrul = egrul or EgrulClient()
    process = process or (lambda company: process_company(egrul, company))

    log(f"Ищем на Checko организации, зарегистрированные {target.isoformat()}...")
    try:
        companies = checko.new_companies(target, max_pages=max_pages)
    except CheckoBlocked as exc:
        log(f"Checko недоступен: {exc}")
        return 3
    except CheckoError as exc:
        log(f"Ошибка при чтении Checko: {exc}")
        return 3
    log(f"Найдено организаций: {len(companies)}; названий в списке: {sum(1 for c in companies if c.name)}")

    state = load_state(state_path(out))
    todo = [c for c in companies if state.get(c.inn, {}).get("status", RETRY) == RETRY]
    if state:
        log(f"Уже проверено раньше: {len(companies) - len(todo)}; осталось: {len(todo)}")
    if limit:
        todo = todo[:limit]

    code = 0
    errors_in_a_row = 0
    for number, company in enumerate(todo, 1):
        if should_stop() or (number > 1 and _wait(pause, should_stop, sleep)):   # пауза: не нагружаем ФНС
            code = 4
            break
        entry, attempts = None, 0
        while entry is None:
            try:
                entry = process(company)
            except CheckoApiQuotaExceeded as exc:
                log(f"Лимит API Checko исчерпан ({exc}). Результат сохранён: продолжите завтра или пополните тариф.")
                code = 6
                break
            except EgrulCaptchaRequired:
                attempts += 1
                if not captcha_wait or attempts > captcha_retries:
                    log("ФНС просит капчу - останавливаемся. Результат сохранён, повторите позже.")
                    code = 2
                    break
                log(f"ФНС просит капчу. Ждём {captcha_wait / 60:g} мин и пробуем снова "
                    f"({attempts}/{captcha_retries}).")
                if _wait(captcha_wait, should_stop, sleep):
                    code = 4
                    break
        if entry is None:
            break
        state[company.inn] = entry
        save_results(out, state)
        # в журнал - только статус, без ФИО и почты (в публичных репозиториях журнал открыт всем)
        log(f"[{number}/{len(todo)}] {company.inn}: {entry['status']}"
            + (f" ({entry['note']})" if entry["note"] else ""))
        errors_in_a_row = errors_in_a_row + 1 if entry["note"].startswith("ошибка") else 0
        if errors_in_a_row >= max_errors_in_a_row:
            log(f"{errors_in_a_row} ошибок подряд - останавливаемся: сайт ФНС отвечает не как обычно. "
                "Причина в строках выше; повторите позже.")
            code = 5
            break
    if code == 4:
        log("Остановлено по запросу. Результат сохранён.")

    save_results(out, state)                # файл создаётся и когда обработать нечего
    log(f"Готово. В таблице {out}: {count(state, OK)} организаций с почтой. "
        f"Без почты (не включены): {count(state, NO_EMAIL)}. "
        f"Проверить повторно: {count(state, RETRY)}.")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Новые организации за день: название, ФИО директора, почта")
    parser.add_argument("--date", type=date.fromisoformat, default=None,
                        help="дата регистрации ГГГГ-ММ-ДД (по умолчанию сегодня по Москве)")
    parser.add_argument("--out", help="CSV-файл (по умолчанию leads_<дата>.csv)")
    parser.add_argument("--limit", type=int, default=0, help="сколько организаций проверить за запуск (0 - все)")
    parser.add_argument("--pause", type=float, default=2.0, help="пауза между организациями, с")
    parser.add_argument("--max-pages", type=int, default=25, help="сколько страниц Checko просматривать")
    parser.add_argument("--captcha-wait", type=float, default=0,
                        help="при капче ФНС ждать столько минут и продолжать (0 - остановиться)")
    args = parser.parse_args(argv)

    target = args.date or today_moscow()
    out = args.out or f"leads_{target.isoformat()}.csv"
    return run(target, out, args.limit, args.pause, args.max_pages,
               captcha_wait=args.captcha_wait * 60)


if __name__ == "__main__":
    sys.exit(main())
