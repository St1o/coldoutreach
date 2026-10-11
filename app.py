"""Простой сайт: ввести ИНН -> «Найти» -> «Получить выписку» (PDF).

Запуск у себя:
    python app.py                 # http://127.0.0.1:8000
    python app.py --port 8080

Публичный сайт (на хостинге с Python или через Dockerfile) настраивается
переменными окружения: HOST=0.0.0.0, PORT, TRUSTED_PROXIES, RATE_LIMIT, MAX_QUEUE.

Страница (index.html) обращается к этому серверу, а сервер - к egrul.nalog.ru
через egrul_inn_search. Напрямую из браузера обратиться к сайту ФНС нельзя
(запрет CORS), поэтому статичный хостинг вроде GitHub Pages не подходит.

Защита сайта ФНС от перегрузки: к реестру идёт один запрос за раз с паузой,
в очереди не больше MAX_QUEUE запросов, повторы по одному ИНН отдаются из кэша,
а с одного адреса принимается не больше RATE_LIMIT запросов в минуту.
"""
import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from egrul_inn_search import (
    EgrulCaptchaRequired, EgrulClient, EgrulError, EgrulTimeout, validate_inn,
)
from leads_job import JobRunning, LeadsJob
from send_service import SendError, SendService
from letter_template import render_letter
from mailing import config as mailcfg, mailboxes as mailboxes_mod
from mail_service import MailError, MailService

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(HERE, "index.html")
LEADS_PAGE_PATH = os.path.join(HERE, "leads.html")
MAIL_PAGE_PATH = os.path.join(HERE, "mail.html")
MAIL_MAX_BODY = 100_000       # запрос страницы /mail: тема и текст одного письма
MAIL_FIELDS = ("company", "director", "okved", "signature", "offer")
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SEND_MAX_BODY = 2_000       # запрос кнопки «Отправить»: число писем и две отметки
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")      # имена, под которыми страницу открывают на этом же компьютере
MAX_BODY = 2_000_000          # текст страниц Checko, вставленный на странице /leads

# Страница использует только свои встроенные скрипт и стили и обращается только к себе.
CSP = ("default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


class NotFound(Exception):
    """По ИНН ничего не найдено."""


class Busy(Exception):
    """Очередь к реестру заполнена."""


class RateLimiter:
    """Не больше `limit` запросов за `window` секунд с одного адреса."""

    def __init__(self, limit=10, window=60.0):
        self.limit, self.window = limit, window
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, key):
        """0 - запрос принят; иначе сколько секунд ждать."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 10000:      # не копим память на разовых посетителях
                self._hits = {k: h for k, h in self._hits.items() if now - h[-1] < self.window}
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return self.window - (now - hits[0])
            hits.append(now)
            return 0.0


class Service:
    """Обращения к реестру: по одному за раз, с паузой, очередью и кэшем."""

    def __init__(self, client_factory=EgrulClient, min_interval=2.0, cache_ttl=300.0,
                 pdf_ttl=600.0, max_pdfs=50, max_queue=8):
        self.client_factory = client_factory
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.pdf_ttl = pdf_ttl
        self.max_pdfs = max_pdfs
        self.max_queue = max_queue        # запросов внутри сервиса: идущий + ожидающие
        self._lock = threading.Lock()     # к реестру - один обмен за раз
        self._state = threading.Lock()
        self._inside = 0
        self._last_call = 0.0
        self._found = {}                  # ИНН -> (время, записи)
        self._pdfs = {}                   # ИНН -> (время, PDF)

    @staticmethod
    def _fresh(store, inn, ttl):
        entry = store.get(inn)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        return None

    @staticmethod
    def _put(store, inn, value, limit):
        if inn not in store and len(store) >= limit:
            del store[min(store, key=lambda k: store[k][0])]    # выбрасываем самое старое
        store[inn] = (time.monotonic(), value)

    @contextmanager
    def _slot(self):
        """Место в очереди к реестру; если очередь полна - Busy."""
        with self._state:
            if self._inside >= self.max_queue:
                raise Busy()
            self._inside += 1
        try:
            with self._lock:
                yield
        finally:
            with self._state:
                self._inside -= 1

    def _throttle(self):
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _search(self, inn):
        self._throttle()
        records = self.client_factory().search_by_inn(inn)
        records = sorted(records, key=lambda r: not r.is_active)   # действующие первыми
        self._put(self._found, inn, records, 1000)
        return records

    @staticmethod
    def _check(inn):
        if not validate_inn(inn):
            raise ValueError("Некорректный ИНН: проверьте цифры (10 или 12).")

    def find(self, inn):
        self._check(inn)
        cached = self._fresh(self._found, inn, self.cache_ttl)
        if cached is not None:
            return cached
        with self._slot():
            cached = self._fresh(self._found, inn, self.cache_ttl)
            return cached if cached is not None else self._search(inn)

    def extract(self, inn):
        """PDF-выписка по первой (действующей) записи."""
        self._check(inn)
        pdf = self._fresh(self._pdfs, inn, self.pdf_ttl)
        if pdf is not None:
            return pdf
        with self._slot():
            pdf = self._fresh(self._pdfs, inn, self.pdf_ttl)
            if pdf is not None:
                return pdf
            records = self._fresh(self._found, inn, self.cache_ttl)
            if records is None:
                records = self._search(inn)
            if not records:
                raise NotFound()
            try:
                self._throttle()
                pdf = self.client_factory().download_extract(records[0])
            except EgrulError:
                self._found.pop(inn, None)    # токен мог устареть - в следующий раз ищем заново
                raise
            self._put(self._pdfs, inn, pdf, self.max_pdfs)
            return pdf


class Handler(BaseHTTPRequestHandler):
    server_version = "EgrulWeb"
    timeout = 30      # секунд на чтение/запись сокета: медленные клиенты не держат поток вечно

    def client_ip(self):
        """Адрес посетителя. За прокси хостинга берём запись X-Forwarded-For, добавленную
        нашим прокси (считая справа): левее могут стоять значения, подставленные клиентом."""
        hops = self.server.trusted_proxies
        if hops:
            parts = [p.strip() for p in self.headers.get("X-Forwarded-For", "").split(",")
                     if p.strip()]
            if len(parts) >= hops:
                return parts[-hops]
        return self.client_address[0]

    def log_request(self, code="-", size="-"):
        # без строки запроса: ИНН в журнал не пишем
        sys.stderr.write(f"{self.client_ip()} {self.command} {urlparse(self.path).path} {code}\n")

    def _send(self, status, body, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload, headers=()):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status, message, headers=(), **extra):
        self._json(status, {"error": message, **extra}, headers)

    def do_HEAD(self):
        # мониторы и хостинги проверяют живость через HEAD; к реестру он не обращается
        if urlparse(self.path).path in ("/", "/index.html", "/healthz"):
            self.do_GET()
        else:
            self._error(405, "Метод не поддерживается.", [("Allow", "GET")])

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(PAGE_PATH, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif url.path == "/healthz":                  # для проверки живости на хостинге
            self._send(200, b"ok", "text/plain; charset=utf-8")
        elif url.path == "/leads":
            self._leads_page()
        elif url.path == "/mail":
            self._mail_page()
        elif url.path == "/api/mail/defaults":
            self._mail_defaults()
        elif url.path == "/api/mail/settings":
            self._mail_settings()
        elif url.path == "/api/mail/mailboxes":
            self._mail_mailboxes()
        elif url.path == "/api/leads/session":
            self._leads_session()
        elif url.path in ("/api/leads/status", "/api/leads/download", "/api/send/status"):
            self._leads_get(url.path, url.query)
        elif url.path in ("/api/search", "/api/extract"):
            wait = self.server.limiter.check(self.client_ip())
            if wait:
                seconds = math.ceil(wait)
                self._error(429, f"Слишком много запросов. Повторите через {seconds} с.",
                            [("Retry-After", str(seconds))])
                return
            inn = (parse_qs(url.query).get("inn") or [""])[0].strip()
            self._api(url.path, inn)
        else:
            self._error(404, "Страница не найдена.")

    # ---- закрытая выгрузка «новые компании» (страница /leads) ----

    def _leads_enabled(self):
        job = self.server.leads_job
        if job is None or not job.enabled:
            self._error(404, "Страница не найдена.")
            return None
        return job

    def _leads_authorized(self, job):
        """Пароль в заголовке X-Leads-Token. Подбор ограничен: 5 неудач в минуту с адреса."""
        if job.check_token(self.headers.get("X-Leads-Token", "")):
            return True
        wait = self.server.auth_limiter.check(self.client_ip())
        if wait:
            seconds = math.ceil(wait)
            self._error(429, f"Слишком много неверных попыток. Повторите через {seconds} с.",
                        [("Retry-After", str(seconds))])
        else:
            self._error(401, "Неверный пароль.")
        return False

    def _local_request(self):
        """Запрос идёт с этого же компьютера, со страницы этого сайта, а не с чужого сайта в браузере:
        адрес собеседника - сам компьютер (заголовок прокси не в счёт), Host - 127.0.0.1 или localhost
        (при подмене DNS в нём будет чужое имя), Sec-Fetch-Site - свой сайт."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.partition(":")[0]
        if name not in LOCAL_HOSTS:
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _leads_session(self):
        """Пароль страницы для самой страницы, только когда сайт запущен у вас на компьютере (run_local.py):
        вводить его не нужно. На хостинге и для чужих запросов страницы нет."""
        job = self.server.leads_job
        if not self.server.auto_login or job is None or not job.enabled or not self._local_request():
            self._error(404, "Страница не найдена.")
            return
        self._json(200, {"token": job.token})

    def _leads_page(self):
        if self._leads_enabled() is None:
            return
        with open(LEADS_PAGE_PATH, "rb") as fh:
            self._send(200, fh.read(), "text/html; charset=utf-8")

    def _leads_get(self, path, query=""):
        job = self._leads_enabled()
        if job is None or not self._leads_authorized(job):
            return
        if path == "/api/send/status":
            service = self.server.send_service
            if service is None:
                self._error(404, "Отправка с этой страницы выключена.")
            else:
                self._json(200, service.status())
            return
        raw_day = (parse_qs(query).get("date") or [""])[0].strip()
        try:
            day = date.fromisoformat(raw_day) if raw_day else None      # таблица показывается только за выбранную дату
        except ValueError:
            self._error(400, "Дата должна быть в виде ГГГГ-ММ-ДД.")
            return
        if path == "/api/leads/status":
            payload = job.status(day)
            payload["send_enabled"] = self.server.send_service is not None
            self._json(200, payload)
            return
        table = job.table(day)
        if table is None:
            self._error(404, "Таблицы пока нет.")
        else:
            name, content = table
            self._send(200, content, XLSX_TYPE,
                       [("Content-Disposition", f'attachment; filename="{name}"')])

    # ---- пробное письмо через mail.ru (страница /mail, тот же пароль, что у /leads) ----

    def _mail_enabled(self):
        """Служба отправки, если задан пароль страницы (LEADS_TOKEN); иначе страница скрыта (404)."""
        job, service = self.server.leads_job, self.server.mail_service
        if job is None or not job.enabled or service is None:
            self._error(404, "Страница не найдена.")
            return None
        return service

    def _mail_page(self):
        if self._mail_enabled() is None:
            return
        with open(MAIL_PAGE_PATH, "rb") as fh:
            self._send(200, fh.read(), "text/html; charset=utf-8")

    def _mail_defaults(self):
        service = self._mail_enabled()
        if service is not None and self._leads_authorized(self.server.leads_job):
            self._json(200, service.defaults())

    def _limit_payload(self):
        value, source = mailcfg.resolve_daily_limit(settings_path=self.server.settings_path)
        return {"daily_limit": value, "source": source, "min": mailcfg.DAILY_LIMIT_MIN, "max": mailcfg.DAILY_LIMIT_MAX}

    def _mail_settings(self):
        """Лимит писем в сутки на один ящик, который действует для mailer.py (страница /mail)."""
        if self._mail_enabled() is not None and self._leads_authorized(self.server.leads_job):
            try:
                self._json(200, self._limit_payload())
            except ValueError:
                self._error(500, "В MAIL_DAILY_LIMIT (файл local.env) не число: исправьте или задайте лимит здесь.")

    def _mailboxes_payload(self):
        return {"mailboxes": self.server.send_service.mailbox_infos()}

    def _mail_mailboxes(self):
        """Подключённые ящики, день прогрева и квота каждого на сегодня (страница /mail). Только при папке данных."""
        if self._mail_enabled() is None or not self._leads_authorized(self.server.leads_job):
            return
        if self.server.send_service is None:
            self._error(404, "Ящики и прогрев видны только на сайте, запущенном у вас на компьютере (run_local.py).")
            return
        self._json(200, self._mailboxes_payload())

    def _mail_post(self, path):
        service = self._mail_enabled()
        if service is None or not self._leads_authorized(self.server.leads_job):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAIL_MAX_BODY:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or not all(isinstance(v, str) for v in body.values()):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._error(400, "Некорректный запрос: нужен JSON с текстовыми полями до 100 КБ.")
            return
        if path == "/api/mail/preview":
            subject, text = render_letter(**{k: body.get(k, "") for k in MAIL_FIELDS})
            self._json(200, {"subject": subject, "body": text})
            return
        if path == "/api/mail/settings":
            try:
                limit = mailcfg.parse_daily_limit(body.get("daily_limit", ""))
            except ValueError as exc:
                self._error(400, str(exc))
                return
            settings = mailcfg.load_settings(self.server.settings_path)
            settings[mailcfg.DAILY_LIMIT_KEY] = limit
            try:
                mailcfg.save_settings(settings, self.server.settings_path)
            except OSError as exc:
                self._error(500, f"Не удалось сохранить настройку: {exc.strerror or exc}")
                return
            self._json(200, self._limit_payload())
            return
        if path == "/api/mail/mailboxes":
            if self.server.send_service is None:
                self._error(404, "Ящики и прогрев видны только на сайте, запущенном у вас на компьютере (run_local.py).")
                return
            known = {info["id"] for info in self.server.send_service.mailbox_infos()}
            if body.get("id", "") not in known:
                self._error(400, "Нет такого подключённого ящика.")
                return
            try:
                mailboxes_mod.set_first_message(self.server.settings_path, body["id"], body.get("first_message", ""))
            except ValueError as exc:
                self._error(400, str(exc))
                return
            except OSError as exc:
                self._error(500, f"Не удалось сохранить дату: {exc.strerror or exc}")
                return
            self._json(200, self._mailboxes_payload())
            return
        try:
            self._json(200, service.send(body.get("to", ""), body.get("subject", ""), body.get("body", ""),
                                         body.get("mailbox", "")))
        except MailError as exc:
            self._error(exc.status, exc.message)

    def _send_post(self, path):
        """Кнопка «Отправить» на странице /leads: проверка (что уйдёт) и запуск отправки в фоне."""
        job = self._leads_enabled()
        if job is None or not self._leads_authorized(job):
            return
        service = self.server.send_service
        if service is None:
            self._error(404, "Отправка с этой страницы выключена.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= SEND_MAX_BODY:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
            include_unwritten = bool(body.get("include_unwritten"))
            expected = body.get("expected")
            if expected is not None and not isinstance(expected, int):
                raise ValueError
            mailbox_count = body.get("mailbox_count")             # со скольки почт отправлять (пусто - со всех)
            if mailbox_count is not None and (not isinstance(mailbox_count, int) or isinstance(mailbox_count, bool)):
                raise ValueError
            only_mailbox = body.get("only_mailbox") or ""          # или только с одной почты: её номер
            if not isinstance(only_mailbox, str):
                raise ValueError
            raw_day = body.get("date") or ""
            if not isinstance(raw_day, str):
                raise ValueError
            day = date.fromisoformat(raw_day.strip()) if raw_day.strip() else None     # письма из таблицы этой даты
        except (ValueError, UnicodeDecodeError):
            self._error(400, "Некорректный запрос: нужен JSON с числом писем, числом или номером почты и датой ГГГГ-ММ-ДД.")
            return
        leads_path = job.output_path(day)
        if not leads_path:
            self._error(400, "Таблицы пока нет: сначала выгрузите компании.")
            return
        try:
            if path == "/api/send/prepare":
                self._json(200, service.prepare(leads_path, body.get("count"), include_unwritten, mailbox_count, only_mailbox))
            else:
                self._json(202, service.start(leads_path, body.get("count"), include_unwritten, expected,
                                              mailbox_count, only_mailbox))
        except SendError as exc:
            self._error(exc.status, exc.message)

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api/mail/preview", "/api/mail/send", "/api/mail/settings", "/api/mail/mailboxes"):
            self._mail_post(path)
            return
        if path in ("/api/send/prepare", "/api/send/start"):
            self._send_post(path)
            return
        if path not in ("/api/leads/start", "/api/leads/stop"):
            self._error(404, "Страница не найдена.")
            return
        job = self._leads_enabled()
        if job is None or not self._leads_authorized(job):
            return
        if path == "/api/leads/stop":
            self._json(200, {"was_running": job.stop()})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            raw_date = str(body.get("date") or "").strip()
            if not raw_date:
                self._error(400, "Укажите дату регистрации (ГГГГ-ММ-ДД): она должна совпадать с датой в тексте с Checko.")
                return
            target = date.fromisoformat(raw_date)
            limit = int(body.get("limit") or 0)
            if not 0 <= limit <= 2000:
                raise ValueError
            text = body.get("text") or ""
            if not isinstance(text, str):
                raise ValueError
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            self._error(400, "Некорректные данные: дата ГГГГ-ММ-ДД, количество 0-2000, текст до 2 МБ.")
            return
        if not job.api_available:
            self._error(400, "Ключ Checko API не задан: добавьте переменную CHECKO_API_KEY в настройках сервера.")
            return
        try:
            job.start(target, limit, text)
        except JobRunning:
            self._error(409, "Выгрузка уже идёт.")
            return
        self._json(202, {"started": True, "date": target.isoformat(), "limit": limit})

    def _api(self, path, inn):
        service = self.server.service
        try:
            if path == "/api/search":
                records = service.find(inn)
                payload = [{k: v for k, v in asdict(r).items() if k != "raw"} for r in records]
                self._json(200, {"inn": inn, "records": payload})
            else:
                pdf = service.extract(inn)
                self._send(200, pdf, "application/pdf",
                           [("Content-Disposition", f'attachment; filename="{inn}.pdf"')])
        except ValueError as exc:
            self._error(400, str(exc))
        except NotFound:
            self._error(404, "По этому ИНН ничего не найдено.")
        except Busy:
            self._error(503, "Сейчас много запросов. Повторите через минуту.",
                        [("Retry-After", "30")])
        except EgrulCaptchaRequired:
            self._error(429, "Сайт ЕГРЮЛ запросил капчу. Подождите несколько минут и повторите.",
                        captcha=True)
        except EgrulTimeout:
            self._error(504, "Сайт ЕГРЮЛ слишком долго не отвечает. Повторите позже.")
        except EgrulError as exc:
            sys.stderr.write(f"EGRUL error: {exc}\n")
            self._error(502, "Не удалось получить данные с сайта ЕГРЮЛ "
                             "(недоступен или изменился). Повторите позже.")


def make_server(host="127.0.0.1", port=8000, service=None, limiter=None, trusted_proxies=0,
                leads_job=None, mail_service=None, settings_path=None, send_service=None, auto_login=False):
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service or Service()
    server.limiter = limiter or RateLimiter()
    server.auth_limiter = RateLimiter(5, 60)
    server.trusted_proxies = trusted_proxies
    server.leads_job = leads_job
    server.mail_service = mail_service
    server.send_service = send_service
    server.auto_login = auto_login
    server.settings_path = settings_path or mailcfg.SETTINGS_FILE
    if send_service is not None and not getattr(send_service, "settings_path", "x"):
        send_service.settings_path = server.settings_path           # лимит и даты ящиков из одного файла
    return server


def main(argv=None):
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Сайт: выписка из ЕГРЮЛ по ИНН")
    parser.add_argument("--host", default=env("HOST", "127.0.0.1"),
                        help="адрес (по умолчанию только этот компьютер; на хостинге 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(env("PORT", 8000)))
    parser.add_argument("--rate-limit", type=int, default=int(env("RATE_LIMIT", 10)),
                        help="запросов в минуту с одного адреса")
    parser.add_argument("--max-queue", type=int, default=int(env("MAX_QUEUE", 8)),
                        help="сколько запросов может одновременно ждать реестр")
    parser.add_argument("--trusted-proxies", type=int, default=int(env("TRUSTED_PROXIES", 0)),
                        help="сколько прокси хостинга стоит перед сайтом (0 - нет)")
    args = parser.parse_args(argv)

    data_dir = env("OUTREACH_DATA", "")              # папка данных рассылки: задана только при запуске у вас на компьютере
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    leads_job = LeadsJob(env("LEADS_TOKEN", ""), out_dir=data_dir or None, api_key=env("CHECKO_API_KEY", ""))
    try:
        smtp_port = mailcfg.resolve_port()                 # MAILRU_PORT из local.env: 465 (по умолчанию) или 587
    except ValueError as exc:
        print(f"{exc} Использую 465.")
        smtp_port = 465
    mail_service = MailService(
        env("MAILRU_USER", ""), env("MAILRU_APP_PASSWORD", ""), env("MAIL_FROM_NAME", ""), port=smtp_port,
        example={"to": env("MAIL_EXAMPLE_TO", ""), "director": env("MAIL_EXAMPLE_DIRECTOR", "")},
        mailboxes=mailboxes_mod.load_mailboxes())           # первый ящик и дополнительные MAILRU_USER_2, ...
    send_service = SendService(data_dir) if data_dir else None
    server = make_server(args.host, args.port, Service(max_queue=args.max_queue),
                         RateLimiter(args.rate_limit), args.trusted_proxies, leads_job, mail_service,
                         send_service=send_service, auto_login=env("LEADS_AUTO_LOGIN") == "1")
    print(f"Сайт запущен: http://{args.host}:{server.server_port}  (Ctrl+C - остановить)")
    if leads_job.enabled:
        print("Страница /leads включена (пароль из LEADS_TOKEN)."
              + (" Источник API Checko доступен." if leads_job.api_available else ""))
    elif env("LEADS_TOKEN"):
        print("LEADS_TOKEN короче 12 знаков: страницы /leads и /mail отключены.")
    if leads_job.enabled and send_service is not None:
        print(f"Кнопка «Отправить» на /leads включена. Папка данных: {data_dir}")
    if leads_job.enabled:
        print("Страница /mail включена (тот же пароль)."
              + ("" if mail_service.configured else " Отправка не настроена: нужны MAILRU_USER и MAILRU_APP_PASSWORD.")
              + (f" Подключено ящиков: {len(mail_service.ready_mailboxes())}." if mail_service.configured else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
