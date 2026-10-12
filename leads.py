"""Новые организации за день -> таблица: название, ФИО директора, почта, основной ОКВЭД.

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
Организации форм, которым владелец решил не писать (ТСН, МБУ, ПК…; правила: mailing.forms), в Checko не запрашиваются:
они получают статус form_skipped и повторно не проверяются, пока правило не изменилось.

Запуск:
    python leads.py                          # сегодня (по Москве)
    python leads.py --date 2026-10-09 --limit 50
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from checko_api import (
    CheckoApiError, CheckoApiNotFound, CheckoApiQuotaExceeded, parse_company,
)
from checko_updates import CheckoBlocked, CheckoClient, CheckoError
from egrul_inn_search import EgrulCaptchaRequired, EgrulClient, EgrulError
from egrul_pdf import ExtractError, parse_extract, pdf_to_text
from mailing import journal

STAGE_COLUMNS = ["Первое письмо", "Дожим", "Закрывающее"]     # в таблице «да» или «нет»: ушло ли письмо этого этапа
YES, NO = "да", "нет"
FIELDS = ["Название", "ФИО директора", "Почта", "ОКВЭД (код)", "ОКВЭД (название)", "Источник почты"] + STAGE_COLUMNS

SOURCE_REGISTER = "ЕГРЮЛ"             # почта из выписки ЕГРЮЛ: на такие адреса рассылка разрешена
SOURCE_CHECKO = "контакты Checko"      # почта из контактов Checko («открытые источники»): для рассылки не годится

OK = "ok"                # есть почта - идёт в таблицу
NO_EMAIL = "no_email"    # выписка получена, почты нет - в таблицу не идёт и повторно не проверяется
RETRY = "retry"          # ошибка или ещё нет в ЕГРЮЛ - проверим при следующем запуске
FORM_SKIPPED = "form_skipped"   # форма организации в правилах «не писать»: запрос не делали, повторно не проверяется
FORM_MISMATCH = "форма не соответствует"


def today_moscow() -> date:
    return datetime.now(timezone(timedelta(hours=3))).date()


def director_from_search(raw: str) -> str:
    """Руководитель из строки поиска egrul («Должность: ФИО») - запасной вариант."""
    return raw.split(":", 1)[-1].strip() if raw else ""


def process_company(client: EgrulClient, company) -> dict:
    """Одна организация -> запись состояния. Капча пробрасывается наверх."""
    entry = {"status": RETRY, "name": company.name, "director": "", "email": "", "okved": "",
             "okved_name": "", "note": ""}
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
    entry["email_source"] = SOURCE_REGISTER
    entry["okved"], entry["okved_name"] = data.okved, data.okved_name
    entry["director"] = data.director or director_from_search(record.director)
    entry["status"] = OK if data.email else NO_EMAIL
    return entry


def form_skipped_entry(company, form: str) -> dict:
    """Запись состояния для организации, которой владелец решил не писать по форме: запрос к API не делается."""
    return {"status": FORM_SKIPPED, "name": company.name, "form": form,
            "note": f"{FORM_MISMATCH}: {form} (решение: не писать)"}


def process_company_api(api, company, form_filter=None) -> dict:
    """Одна организация через API Checko -> запись состояния. Исчерпанный лимит пробрасывается.
    form_filter (mailing.forms.FormFilter): форма организации в правилах «не писать» - запрос к API не делается,
    возвращается запись form_skipped."""
    if form_filter is not None:
        form = form_filter.form_of(company.name)
        if form_filter.blocked(form):
            return form_skipped_entry(company, form)
    entry = {"status": RETRY, "name": company.name, "director": "", "email": "", "okved": "",
             "okved_name": "", "note": ""}
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
    entry["email"] = info.email
    entry["email_source"] = SOURCE_REGISTER if info.email_from_register else SOURCE_CHECKO
    entry["okved"], entry["okved_name"] = info.okved, info.okved_name
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


def _folder(out: str) -> str:
    return os.path.dirname(os.path.abspath(out))


def _csv_text(state: dict, sent: dict) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(FIELDS)
    writer.writerows(table_rows(state, sent))
    return buffer.getvalue()


def _write_csv(out: str, text: str) -> None:
    def write(tmp):
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:    # BOM: Excel читает кириллицу
            fh.write(text)

    _write_atomic(out, write)


def save_results(out: str, state: dict, sent: dict = None) -> None:
    """Служебный файл и таблица (только организации с почтой) перезаписываются целиком. Колонки «Первое письмо»,
    «Дожим», «Закрывающее» берутся из журналов отправки в той же папке (sent - готовый journal.sent_stages)."""
    if sent is None:
        sent = journal.sent_stages(_folder(out))

    def write_state(tmp):
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)

    _write_atomic(state_path(out), write_state)
    _write_csv(out, _csv_text(state, sent))


def refresh_table(out: str) -> bool:
    """Обновляет в CSV колонки «Первое письмо», «Дожим», «Закрывающее» по журналам отправки: письма уходят уже
    после выгрузки, и таблица на диске должна это отражать. True - файл изменился. Выгрузки за эту дату ещё не было
    (или служебный файл пуст или испорчен) - ничего не трогает, чтобы не стереть готовую таблицу."""
    state = load_state(state_path(out))
    if not state:
        return False
    text = _csv_text(state, journal.sent_stages(_folder(out)))
    try:
        with open(out, encoding="utf-8-sig", newline="") as fh:
            if fh.read() == text:
                return False
    except FileNotFoundError:
        pass
    _write_csv(out, text)
    return True


def needs_check(entry, form_filter=None) -> bool:
    """Проверять ли организацию (снова): ошибка или нет в ЕГРЮЛ; а также запись из старого запуска,
    сделанного до появления ОКВЭД и источника почты (ключа «okved» в ней нет) - её дозаполняем один раз.
    Пропущенная по форме (form_skipped) проверяется снова, только если правила форм сейчас читаются и форма в них
    больше не «не писать» (владелец изменил или убрал строку): пока правило то же или правил нет, запрос не делаем."""
    if entry is None:
        return True
    if entry["status"] == FORM_SKIPPED:
        return form_filter is not None and form_filter.active and not form_filter.blocked(entry.get("form"))
    return entry["status"] == RETRY or (entry["status"] == OK and "okved" not in entry)


def count(state: dict, status: str) -> int:
    return sum(1 for entry in state.values() if entry["status"] == status)


def form_summary(state: dict) -> str:
    """Пропущенные по форме -> «ТСН 2, МБУ 1, ПК 1» (больше организаций - раньше); никого нет - пустая строка."""
    forms = Counter(entry.get("form", "?") for entry in state.values() if entry["status"] == FORM_SKIPPED)
    return ", ".join(f"{form} {number}" for form, number in sorted(forms.items(), key=lambda item: (-item[1], item[0])))


WEB_HEADER = FIELDS


def _table_entries(state: dict) -> list:
    return [entry for entry in state.values() if entry["status"] == OK]


def table_notes(state: dict, form_filter=None) -> list:
    """Пометки к строкам таблицы (по одной на строку table_rows, в том же порядке): «форма не соответствует: ТСН» у
    организаций, проверенных раньше, чья форма сейчас в правилах «не писать»; у остальных пустая строка."""
    if form_filter is None:
        return [""] * len(_table_entries(state))
    notes = []
    for entry in _table_entries(state):
        form = form_filter.form_of(entry["name"])
        notes.append(f"{FORM_MISMATCH}: {form}" if form_filter.blocked(form) else "")
    return notes


def table_rows(state: dict, sent: dict = None) -> list:
    """Строки таблицы для сайта и Excel-файла (те же колонки, что в CSV): организации с почтой, в порядке проверки.
    sent - журнал отправки (journal.sent_stages): в колонках этапов «да», если письмо этого этапа принято сервером."""
    sent = sent or {}
    rows = []
    for entry in _table_entries(state):
        stages = sent.get(entry["email"].lower(), {})
        rows.append([entry["name"], entry["director"], entry["email"], entry.get("okved", ""),
                     entry.get("okved_name", ""), entry.get("email_source", "")]
                    + [YES if stage in stages else NO for stage in (1, 2, 3)])
    return rows


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
        process=None, form_filter=None) -> int:
    """Возвращает код выхода: 0 - готово, 2 - ФНС потребовала капчу, 3 - Checko недоступен,
    4 - остановлено пользователем, 5 - слишком много ошибок подряд (ФНС отвечает не как обычно:
    дальше стучаться бессмысленно), 6 - исчерпан лимит API Checko, 7 - во вставленном тексте нет
    организаций за выбранную дату (есть за другие).

    process(company) -> запись состояния: по умолчанию выписки ФНС (egrul), можно подставить API.

    form_filter (mailing.forms.FormFilter): организации форм, которым владелец решил не писать, не проверяются вовсе:
    им пишется запись form_skipped без запроса, и они не расходуют ни limit, ни паузу. Организации без названия и
    формы без решения проверяются как обычно. Нет фильтра (None) - всё как раньше.

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
    other_dates = checko.dates() if not companies and hasattr(checko, "dates") else {}
    if other_dates:
        listed = ", ".join(f"{day.isoformat()} ({number})" for day, number in sorted(other_dates.items(), reverse=True))
        log(f"В вставленном тексте нет организаций за {target.isoformat()}. Есть за: {listed}. "
            "Укажите одну из этих дат в поле «Дата регистрации».")
        return 7

    state = load_state(state_path(out))
    if form_filter is not None:
        log(form_filter.problem or form_filter.describe())
    todo, skipped = [], []
    for company in companies:
        if not needs_check(state.get(company.inn), form_filter):
            continue
        form = form_filter.form_of(company.name) if form_filter is not None else None
        if form_filter is not None and form_filter.blocked(form):
            skipped.append((company, form))
        else:
            todo.append(company)
    if state:
        log(f"Уже проверено раньше: {len(companies) - len(todo) - len(skipped)}; осталось: {len(todo)}")
    for number, (company, form) in enumerate(skipped, 1):       # без запроса, без паузы, до подсчёта limit
        state[company.inn] = form_skipped_entry(company, form)
        log(f"[{number}/{len(skipped)}] {company.inn}: {FORM_SKIPPED} ({FORM_MISMATCH}: {form})")
    if limit:
        todo = todo[:limit]

    code = 0
    errors_in_a_row = 0
    processed = failed = 0
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
        failure = entry["status"] == RETRY and entry["note"].startswith("ошибка")
        processed, failed = processed + 1, failed + failure
        errors_in_a_row = errors_in_a_row + 1 if entry["note"].startswith("ошибка") else 0
        if errors_in_a_row >= max_errors_in_a_row:
            log(f"{errors_in_a_row} ошибок подряд - останавливаемся: сайт ФНС отвечает не как обычно. "
                "Причина в строках выше; повторите позже.")
            code = 5
            break
    if code == 4:
        log("Остановлено по запросу. Результат сохранён.")
    elif code == 0 and processed and failed == processed:
        log("Ни одну организацию проверить не удалось: все запросы закончились ошибкой. Причина в строках выше.")
        code = 5

    save_results(out, state)                # файл создаётся и когда обработать нечего
    by_form = form_summary(state)
    log(f"Готово. В таблице {out}: {count(state, OK)} организаций с почтой. "
        f"Без почты (не включены): {count(state, NO_EMAIL)}. "
        f"Проверить повторно: {count(state, RETRY)}."
        + (f" Пропущено по форме организации (запрос к API не делали): {count(state, FORM_SKIPPED)} ({by_form})."
           if by_form else ""))
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Новые организации за день: название, ФИО директора, почта, основной ОКВЭД")
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
