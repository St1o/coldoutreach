"""Простой сайт: ввести ИНН -> «Найти» -> «Получить выписку» (PDF).

Запуск:
    python app.py                 # http://127.0.0.1:8000
    python app.py --port 8080

Страница (index.html) обращается к этому серверу, а сервер - к egrul.nalog.ru
через egrul_inn_search. Напрямую из браузера обратиться к сайту ФНС нельзя
(запрет CORS), поэтому статичный хостинг вроде GitHub Pages не подходит.
"""
import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from egrul_inn_search import (
    EgrulCaptchaRequired, EgrulClient, EgrulError, EgrulTimeout, validate_inn,
)

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


class NotFound(Exception):
    """По ИНН ничего не найдено."""


class Service:
    """Обращения к реестру: по одному за раз, с паузой, чтобы не нагружать сайт ФНС."""

    def __init__(self, client_factory=EgrulClient, min_interval=2.0, cache_ttl=300.0):
        self.client_factory = client_factory
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._found = {}          # ИНН -> (время, записи): чтобы «Выписка» не искала заново

    def _throttle(self):
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _search(self, inn):
        self._throttle()
        records = self.client_factory().search_by_inn(inn)
        records = sorted(records, key=lambda r: not r.is_active)   # действующие первыми
        self._found[inn] = (time.monotonic(), records)
        return records

    def find(self, inn):
        if not validate_inn(inn):
            raise ValueError("Некорректный ИНН: проверьте цифры (10 или 12).")
        with self._lock:
            return self._search(inn)

    def extract(self, inn):
        """PDF-выписка по первой (действующей) записи."""
        if not validate_inn(inn):
            raise ValueError("Некорректный ИНН: проверьте цифры (10 или 12).")
        with self._lock:
            cached = self._found.get(inn)
            fresh = cached and time.monotonic() - cached[0] < self.cache_ttl
            records = cached[1] if fresh else self._search(inn)
            if not records:
                raise NotFound()
            try:
                self._throttle()
                return self.client_factory().download_extract(records[0])
            except EgrulError:
                self._found.pop(inn, None)    # токен мог устареть - в следующий раз ищем заново
                raise


class Handler(BaseHTTPRequestHandler):
    server_version = "EgrulWeb"

    def _send(self, status, body, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status, message, **extra):
        self._json(status, {"error": message, **extra})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(PAGE_PATH, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif url.path in ("/api/search", "/api/extract"):
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
        except EgrulCaptchaRequired:
            self._error(429, "Сайт ЕГРЮЛ запросил капчу. Подождите несколько минут и повторите.",
                        captcha=True)
        except EgrulTimeout:
            self._error(504, "Сайт ЕГРЮЛ слишком долго не отвечает. Повторите позже.")
        except EgrulError as exc:
            sys.stderr.write(f"EGRUL error for {inn}: {exc}\n")
            self._error(502, "Не удалось получить данные с сайта ЕГРЮЛ "
                             "(недоступен или изменился). Подробности - в консоли сервера.")


def make_server(host="127.0.0.1", port=8000, service=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service or Service()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Сайт: выписка из ЕГРЮЛ по ИНН")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="адрес (по умолчанию только этот компьютер)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port)
    print(f"Откройте в браузере: http://{args.host}:{server.server_port}  (Ctrl+C - остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
