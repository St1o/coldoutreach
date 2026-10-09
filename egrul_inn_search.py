"""Поиск по ИНН на сайте https://egrul.nalog.ru и скачивание выписки (PDF)

Алгоритм (повторяет действия пользователя на сайте):
    1. Проверить ИНН (длина 10 или 12 цифр + контрольные цифры).
    2. POST https://egrul.nalog.ru/  с полем query=<ИНН>   (кнопка «Найти»)
       -> JSON {"t": "<токен поиска>"}  (или {"captchaRequired": true}).
    3. GET  /search-result/<токен поиска>
       пока ответ {"status": "wait"} - повторять с паузой;
       готовый ответ - JSON {"rows": [ {...}, ... ]}; в строке есть свой токен "t".
    4. Разобрать rows в EgrulRecord и оставить записи с точно совпавшим ИНН.
    5. GET  /vyp-request/<токен строки>                    (кнопка «Получить выписку»)
    6. GET  /vyp-status/<токен>  пока выписка не станет {"status": "ready"}.
    7. GET  /vyp-download/<токен>  -> PDF, сохранить в <папка>/<ИНН>.pdf.

Шаги 5-7 проверены на живом сайте (запуск в GitHub Actions 09.10.2026). Сайт может
изменить адреса и формат ответа; вся привязка к ним - в request_extract /
_wait_extract / _download_pdf, а к полям строки результата - в parse_row.

Использование:
    python egrul_inn_search.py 7707083893                      # только данные
    python egrul_inn_search.py 7707083893 500100732259 --json
    python egrul_inn_search.py 7707083893 500100732259 --pdf-dir выписки
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Iterable, Iterator, Optional

import requests

BASE_URL = "https://egrul.nalog.ru"

_INN10_COEFFS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_COEFFS_N11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_COEFFS_N12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)

# Поле "k" в строке результата: ul - юридическое лицо, fl - индивидуальный предприниматель
_KINDS = {"ul": "ЮЛ", "fl": "ИП"}


class EgrulError(Exception):
    """Базовая ошибка при обращении к egrul.nalog.ru."""


class EgrulCaptchaRequired(EgrulError):
    """Сайт потребовал капчу. Решать её автоматически не предусмотрено."""


class EgrulTimeout(EgrulError):
    """Результат поиска / выписка не появились за отведённое время."""


def _check_digit(digits, coeffs):
    return sum(int(d) * c for d, c in zip(digits, coeffs)) % 11 % 10


def validate_inn(inn: str) -> bool:
    """Проверяет формат и контрольные цифры ИНН (10 цифр - ЮЛ, 12 - ИП/физлицо)."""
    if not isinstance(inn, str) or not inn.isdigit():
        return False
    if len(inn) == 10:
        return _check_digit(inn, _INN10_COEFFS) == int(inn[9])
    if len(inn) == 12:
        return (_check_digit(inn, _INN12_COEFFS_N11) == int(inn[10])
                and _check_digit(inn, _INN12_COEFFS_N12) == int(inn[11]))
    return False


@dataclass
class EgrulRecord:
    inn: str
    ogrn: str = ""
    kpp: str = ""
    kind: str = ""          # "ЮЛ" / "ИП" (либо исходное значение поля k)
    name: str = ""          # полное наименование / ФИО ИП
    short_name: str = ""    # краткое наименование
    director: str = ""
    reg_date: str = ""      # дата регистрации (присвоения ОГРН)
    end_date: str = ""      # дата прекращения деятельности, пусто - действует
    token: str = ""         # токен строки (нужен для запроса выписки)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_active(self) -> bool:
        return not self.end_date


@dataclass
class ExtractResult:
    """Итог по одному ИНН при скачивании выписки."""
    inn: str
    path: Optional[str] = None      # куда сохранён PDF
    record: Optional[EgrulRecord] = None
    error: Optional[str] = None
    skipped: bool = False           # файл уже был, повторно не скачивали
    captcha: bool = False           # сайт потребовал капчу

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_row(row: dict) -> EgrulRecord:
    """Единственное место, где сопоставляются сокращённые поля ответа сайта."""
    kind = row.get("k", "")
    return EgrulRecord(
        inn=row.get("i", ""),
        ogrn=row.get("o", ""),
        kpp=row.get("p", ""),
        kind=_KINDS.get(kind, kind),
        name=row.get("n", ""),
        short_name=row.get("c", ""),
        director=row.get("g", ""),
        reg_date=row.get("r", ""),
        end_date=row.get("e", ""),
        token=row.get("t", ""),
        raw=row,
    )


class EgrulClient:
    def __init__(self, session: Optional[requests.Session] = None,
                 request_timeout: float = 20.0, poll_interval: float = 1.0,
                 max_wait: float = 30.0):
        self.session = session or requests.Session()
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self._session_ready = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL + "/index.html",
        })

    # ---- HTTP ----------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs):
        """Выполняет запрос; сетевые и HTTP-ошибки превращаются в EgrulError."""
        try:
            response = getattr(self.session, method)(
                url, timeout=self.request_timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            self._raise_for_failed(method, url, exc)

    @staticmethod
    def _raise_for_failed(method, url, exc):
        """Ошибка запроса. Если сайт ответил, показываем начало его ответа (по нему видно
        причину), а ответ с признаком капчи превращаем в EgrulCaptchaRequired."""
        response = getattr(exc, "response", None)
        body = ""
        if response is not None:
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("captchaRequired"):
                    raise EgrulCaptchaRequired("Сайт требует капчу.") from exc
            except ValueError:
                pass
            body = " ".join((getattr(response, "text", "") or "").split())[:200]
        detail = f" | ответ сайта: {body}" if body else ""
        raise EgrulError(f"Ошибка запроса {method.upper()} {url}: {exc}{detail}") from exc

    def _request_json(self, method: str, url: str, **kwargs) -> dict:
        response = self._request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise EgrulError(f"Ответ {url} не является JSON: {exc}") from exc

    def _ensure_session(self) -> None:
        """Открывает главную страницу, чтобы получить cookie, как это делает браузер."""
        if not self._session_ready:
            self._request("get", BASE_URL + "/index.html")
            self._session_ready = True

    def _poll(self, url: str, is_ready, what: str) -> dict:
        """Опрашивает url, пока is_ready(payload) не вернёт True; ограничено max_wait."""
        deadline = time.monotonic() + self.max_wait
        while True:
            now_ms = int(time.time() * 1000)
            payload = self._request_json("get", url, params={"r": now_ms, "_": now_ms})
            if payload.get("captchaRequired"):
                raise EgrulCaptchaRequired(f"Сайт требует капчу ({what}).")
            if is_ready(payload):
                return payload
            if time.monotonic() >= deadline:
                raise EgrulTimeout(f"{what}: не готово за {self.max_wait} с: {payload}")
            time.sleep(self.poll_interval)

    # ---- поиск ---------------------------------------------------------

    def _start_search(self, inn: str) -> str:
        """Шаг 2: отправить ИНН, получить токен поиска."""
        self._ensure_session()
        data = {
            "vyp3CaptchaToken": "",
            "page": "",
            "query": inn,
            "region": "",
            "PreventChromeAutocomplete": "",
        }
        payload = self._request_json("post", BASE_URL + "/", data=data)
        if payload.get("captchaRequired"):
            raise EgrulCaptchaRequired(
                "Сайт требует капчу. Подождите и повторите позже, "
                "либо используйте открытые данные ФНС / API-ФНС.")
        token = payload.get("t")
        if not token:
            raise EgrulError(f"В ответе нет токена поиска: {payload}")
        return token

    def search_by_inn(self, inn: str, exact: bool = True) -> list:
        """Возвращает список EgrulRecord по ИНН.

        exact=True оставляет только записи с точно таким же ИНН
        (поиск на сайте полнотекстовый); exact=False отдаёт всё, что вернул сайт.
        """
        inn = inn.strip()
        if not validate_inn(inn):
            raise ValueError(f"Некорректный ИНН: {inn!r}")
        token = self._start_search(inn)
        payload = self._poll(f"{BASE_URL}/search-result/{token}",
                             lambda p: "rows" in p, "результат поиска")
        records = [parse_row(row) for row in payload["rows"]]
        return [r for r in records if r.inn == inn] if exact else records

    # ---- выписка -------------------------------------------------------

    def request_extract(self, record: EgrulRecord) -> str:
        """Шаг 5 («Получить выписку»): запускает формирование, возвращает токен выписки."""
        if not record.token:
            raise EgrulError("У найденной записи нет токена для запроса выписки.")
        payload = self._request_json("get", f"{BASE_URL}/vyp-request/{record.token}")
        if payload.get("captchaRequired"):
            raise EgrulCaptchaRequired("Сайт требует капчу при запросе выписки.")
        return payload.get("t") or record.token

    def _wait_extract(self, token: str) -> None:
        """Шаг 6: ждать, пока выписка сформирована."""
        self._poll(f"{BASE_URL}/vyp-status/{token}",
                   lambda p: p.get("status") == "ready", "формирование выписки")

    def _download_pdf(self, token: str) -> bytes:
        """Шаг 7: скачать готовую выписку; убеждаемся, что это действительно PDF."""
        content = self._request("get", f"{BASE_URL}/vyp-download/{token}").content
        if not content.startswith(b"%PDF-"):
            raise EgrulError("Сайт вернул не PDF (возможно, капча или ошибка сервиса).")
        return content

    def download_extract(self, record: EgrulRecord) -> bytes:
        """Запрос -> ожидание -> скачивание. Возвращает содержимое PDF (подписанный ЭЦП файл)."""
        token = self.request_extract(record)
        self._wait_extract(token)
        return self._download_pdf(token)

    def fetch_extract(self, inn: str, out_dir: str, overwrite: bool = False) -> ExtractResult:
        """Полный цикл для одного ИНН: поиск -> выписка -> файл <out_dir>/<ИНН>.pdf.

        Бросает EgrulError / ValueError; для пакетной обработки есть fetch_extracts.
        """
        inn = inn.strip()
        if not validate_inn(inn):   # после проверки в имени файла только цифры
            raise ValueError(f"Некорректный ИНН: {inn!r}")
        path = os.path.join(out_dir, f"{inn}.pdf")
        if os.path.exists(path) and not overwrite:
            return ExtractResult(inn, path=path, skipped=True)

        records = self.search_by_inn(inn)
        if not records:
            raise EgrulError("По этому ИНН ничего не найдено.")
        record = max(records, key=lambda r: r.is_active)   # действующая запись, иначе первая
        pdf = self.download_extract(record)

        os.makedirs(out_dir, exist_ok=True)
        with open(path + ".part", "wb") as fh:
            fh.write(pdf)
        os.replace(path + ".part", path)     # файл появляется только целиком
        return ExtractResult(inn, path=path, record=record)

    def fetch_extracts(self, inns: Iterable[str], out_dir: str, pause: float = 2.0,
                       overwrite: bool = False) -> Iterator[ExtractResult]:
        """Пакетная обработка: результат по каждому ИНН отдаётся сразу, ошибка одного
        ИНН не прерывает остальные. После капчи обработка останавливается."""
        first = True
        for inn in inns:
            if not first:
                time.sleep(pause)    # не нагружаем сайт
            first = False
            try:
                result = self.fetch_extract(inn, out_dir, overwrite)
            except EgrulCaptchaRequired as exc:
                yield ExtractResult(inn, error=str(exc), captcha=True)
                return
            except (EgrulError, ValueError) as exc:
                result = ExtractResult(inn, error=str(exc))
            yield result


def search_by_inn(inn: str) -> list:
    """Короткая обёртка: одна функция - один поиск."""
    return EgrulClient().search_by_inn(inn)


def _print_record(rec: EgrulRecord) -> None:
    status = "действует" if rec.is_active else f"прекращено {rec.end_date}"
    print(f"[{rec.kind}] {rec.short_name or rec.name}")
    print(f"  ИНН: {rec.inn}  ОГРН: {rec.ogrn}  КПП: {rec.kpp or '-'}")
    print(f"  Полное наименование: {rec.name}")
    print(f"  Руководитель: {rec.director or '-'}")
    print(f"  Дата регистрации: {rec.reg_date or '-'}  Статус: {status}")


def _run_extracts(client: EgrulClient, args) -> int:
    exit_code = 0
    for res in client.fetch_extracts(args.inn, args.pdf_dir, pause=args.pause,
                                     overwrite=args.overwrite):
        if res.error:
            print(f"{res.inn}: ошибка - {res.error}", file=sys.stderr)
            exit_code = 2 if res.captcha else (exit_code or 1)
        elif res.skipped:
            print(f"{res.inn}: пропущено, файл уже есть ({res.path})")
        else:
            print(f"{res.inn}: сохранено {res.path}")
    return exit_code


def _run_search(client: EgrulClient, args) -> int:
    results, exit_code = {}, 0
    for i, inn in enumerate(args.inn):
        if i:
            time.sleep(args.pause)
        try:
            results[inn] = client.search_by_inn(inn)
        except EgrulCaptchaRequired as exc:
            print(f"{inn}: {exc}", file=sys.stderr)
            return 2  # дальше продолжать бессмысленно
        except (EgrulError, ValueError) as exc:
            print(f"{inn}: ошибка - {exc}", file=sys.stderr)
            exit_code = 1

    if args.json:
        out = {inn: [{k: v for k, v in asdict(r).items() if k != "raw"} for r in recs]
               for inn, recs in results.items()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for inn, recs in results.items():
            if not recs:
                print(f"{inn}: ничего не найдено")
            for rec in recs:
                _print_record(rec)
    return exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Поиск по ИНН на egrul.nalog.ru")
    parser.add_argument("inn", nargs="+", help="один или несколько ИНН")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    parser.add_argument("--pdf-dir", metavar="ПАПКА",
                        help="скачать выписки (PDF) в эту папку как <ИНН>.pdf")
    parser.add_argument("--overwrite", action="store_true",
                        help="перекачивать выписки, которые уже есть в папке")
    parser.add_argument("--pause", type=float, default=2.0,
                        help="пауза между ИНН, с (не нагружать сайт)")
    args = parser.parse_args(argv)

    client = EgrulClient()
    return _run_extracts(client, args) if args.pdf_dir else _run_search(client, args)


if __name__ == "__main__":
    sys.exit(main())
