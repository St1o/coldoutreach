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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from egrul_inn_search import (
    EgrulCaptchaRequired, EgrulClient, EgrulError, EgrulTimeout, validate_inn,
)

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

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


def make_server(host="127.0.0.1", port=8000, service=None, limiter=None, trusted_proxies=0):
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service or Service()
    server.limiter = limiter or RateLimiter()
    server.trusted_proxies = trusted_proxies
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

    server = make_server(args.host, args.port, Service(max_queue=args.max_queue),
                         RateLimiter(args.rate_limit), args.trusted_proxies)
    print(f"Сайт запущен: http://{args.host}:{server.server_port}  (Ctrl+C - остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
