import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from app import RateLimiter, Service, make_server
from egrul_inn_search import EgrulCaptchaRequired, EgrulError, EgrulRecord, EgrulTimeout

INN = "7707083893"
INN2 = "500100732259"
PDF = b"%PDF-1.7 fake"


class FakeClient:
    """Подставной клиент ЕГРЮЛ: считает обращения и может падать или «висеть»."""
    searches = 0
    downloads = 0
    records = []
    search_error = None
    download_error = None
    gate = None       # Event: пока не set(), поиск «висит»
    entered = None    # Event: set(), когда поиск начался

    def search_by_inn(self, inn):
        FakeClient.searches += 1
        if FakeClient.entered:
            FakeClient.entered.set()
        if FakeClient.gate:
            FakeClient.gate.wait(5)
        if FakeClient.search_error:
            raise FakeClient.search_error
        return list(FakeClient.records)

    def download_extract(self, record):
        FakeClient.downloads += 1
        if FakeClient.download_error:
            raise FakeClient.download_error
        return PDF


def record(**kw):
    base = dict(inn=INN, ogrn="1027700132195", kind="ЮЛ", name="ПАО СБЕРБАНК", token="t")
    base.update(kw)
    return EgrulRecord(**base)


class AppTest(unittest.TestCase):
    def setUp(self):
        FakeClient.searches = FakeClient.downloads = 0
        FakeClient.records = [record()]
        FakeClient.search_error = FakeClient.download_error = None
        FakeClient.gate = FakeClient.entered = None
        self.start()

    def start(self, service=None, limiter=None, trusted_proxies=0):
        server = make_server("127.0.0.1", 0, service or Service(FakeClient, min_interval=0),
                             limiter or RateLimiter(10000), trusted_proxies)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop, server)
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def stop(server):
        server.shutdown()
        server.server_close()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get_json(self, path, headers=None):
        status, _, body = self.get(path, headers)
        return status, json.loads(body)

    # ---- страница и общие свойства ----

    def test_index_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Получить выписку".encode(), body)

    def test_security_headers(self):
        _, headers, _ = self.get("/")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_healthz(self):
        self.assertEqual(self.get("/healthz")[::2], (200, b"ok"))

    def test_head_for_monitors_never_reaches_registry(self):
        def head(path):
            request = urllib.request.Request(self.base + path, method="HEAD")
            try:
                with urllib.request.urlopen(request) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()
        self.assertEqual(head("/healthz"), (200, b""))
        self.assertEqual(head("/"), (200, b""))
        self.assertEqual(head(f"/api/search?inn={INN}"), (405, b""))
        self.assertEqual(FakeClient.searches, 0)

    def test_unknown_path(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/app.py")[0], 404)     # исходники не раздаются

    # ---- поиск ----

    def test_search_returns_records_without_raw(self):
        status, data = self.get_json(f"/api/search?inn={INN}")
        self.assertEqual(status, 200)
        self.assertEqual(data["records"][0]["name"], "ПАО СБЕРБАНК")
        self.assertNotIn("raw", data["records"][0])

    def test_search_puts_active_record_first(self):
        FakeClient.records = [record(name="OLD", end_date="01.01.2020"), record(name="NEW")]
        _, data = self.get_json(f"/api/search?inn={INN}")
        self.assertEqual([r["name"] for r in data["records"]], ["NEW", "OLD"])

    def test_search_nothing_found(self):
        FakeClient.records = []
        status, data = self.get_json(f"/api/search?inn={INN}")
        self.assertEqual((status, data["records"]), (200, []))

    def test_repeated_search_is_served_from_cache(self):
        self.get_json(f"/api/search?inn={INN}")
        self.get_json(f"/api/search?inn={INN}")
        self.assertEqual(FakeClient.searches, 1)

    def test_invalid_inn_never_reaches_registry(self):
        for bad in ("", "123", "1234567890", "../../etc/passwd", "7707083893%0d%0aSet-Cookie:x"):
            status, data = self.get_json("/api/search?inn=" + bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", data)
            status, _, _ = self.get("/api/extract?inn=" + bad)
            self.assertEqual(status, 400, bad)
        self.assertEqual(FakeClient.searches, 0)

    def test_surrounding_whitespace_is_trimmed(self):
        status, headers, _ = self.get(f"/api/extract?inn=%20{INN}%0d%0a")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], f'attachment; filename="{INN}.pdf"')

    # ---- выписка ----

    def test_extract_returns_pdf_and_reuses_search(self):
        self.get_json(f"/api/search?inn={INN}")
        status, headers, body = self.get(f"/api/extract?inn={INN}")
        self.assertEqual(status, 200)
        self.assertEqual(body, PDF)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertIn(f'filename="{INN}.pdf"', headers["Content-Disposition"])
        self.assertEqual(FakeClient.searches, 1)     # повторного поиска не было

    def test_extract_without_prior_search_searches_itself(self):
        status, _, body = self.get(f"/api/extract?inn={INN}")
        self.assertEqual((status, body, FakeClient.searches), (200, PDF, 1))

    def test_repeated_extract_is_served_from_cache(self):
        self.get(f"/api/extract?inn={INN}")
        self.get(f"/api/extract?inn={INN}")
        self.assertEqual(FakeClient.downloads, 1)

    def test_pdf_cache_is_bounded(self):
        service = Service(FakeClient, min_interval=0, max_pdfs=2)
        self.start(service)
        for inn in (INN, INN2, "7707083004"):
            self.get(f"/api/extract?inn={inn}")
        self.assertEqual(len(service._pdfs), 2)
        self.assertNotIn(INN, service._pdfs)          # самое старое вытеснено

    def test_extract_not_found(self):
        FakeClient.records = []
        status, data = self.get_json(f"/api/extract?inn={INN}")
        self.assertEqual(status, 404)
        self.assertEqual(FakeClient.downloads, 0)

    def test_failed_download_drops_cache_so_next_try_searches_again(self):
        self.get_json(f"/api/search?inn={INN}")
        FakeClient.download_error = EgrulError("token expired")
        self.assertEqual(self.get(f"/api/extract?inn={INN}")[0], 502)
        FakeClient.download_error = None
        self.assertEqual(self.get(f"/api/extract?inn={INN}")[0], 200)
        self.assertEqual(FakeClient.searches, 2)

    # ---- ошибки реестра ----

    def test_error_mapping(self):
        cases = [(EgrulCaptchaRequired("c"), 429), (EgrulTimeout("t"), 504), (EgrulError("e"), 502)]
        for error, expected in cases:
            FakeClient.search_error = error
            status, data = self.get_json(f"/api/search?inn={INN}")
            self.assertEqual(status, expected, error)
            self.assertNotIn("Traceback", data["error"])
        FakeClient.search_error = EgrulCaptchaRequired("c")
        self.assertTrue(self.get_json(f"/api/search?inn={INN}")[1]["captcha"])

    # ---- защита от перегрузки ----

    def test_rate_limit_per_address(self):
        self.start(limiter=RateLimiter(3, 60))
        for _ in range(3):
            self.assertEqual(self.get(f"/api/search?inn={INN}")[0], 200)
        status, headers, body = self.get(f"/api/search?inn={INN}")
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.get("/")[0], 200)               # страница и проверка живости
        self.assertEqual(self.get("/healthz")[0], 200)        # под лимит не попадают

    def test_forwarded_header_is_ignored_without_trusted_proxy(self):
        self.start(limiter=RateLimiter(1, 60))
        self.assertEqual(self.get(f"/api/search?inn={INN}", {"X-Forwarded-For": "1.1.1.1"})[0], 200)
        self.assertEqual(self.get(f"/api/search?inn={INN}", {"X-Forwarded-For": "2.2.2.2"})[0], 429)

    def test_forwarded_header_with_trusted_proxy(self):
        self.start(limiter=RateLimiter(1, 60), trusted_proxies=1)
        path = f"/api/search?inn={INN}"
        self.assertEqual(self.get(path, {"X-Forwarded-For": "9.9.9.9, 5.5.5.5"})[0], 200)
        # подставленное клиентом левое значение не помогает обойти лимит
        self.assertEqual(self.get(path, {"X-Forwarded-For": "8.8.8.8, 5.5.5.5"})[0], 429)
        # другой посетитель (другой адрес от прокси) не страдает
        self.assertEqual(self.get(path, {"X-Forwarded-For": "5.5.5.5, 6.6.6.6"})[0], 200)

    def test_full_queue_returns_503(self):
        FakeClient.gate, FakeClient.entered = threading.Event(), threading.Event()
        self.start(Service(FakeClient, min_interval=0, max_queue=1))
        first = threading.Thread(target=self.get, args=(f"/api/search?inn={INN}",))
        first.start()
        self.assertTrue(FakeClient.entered.wait(5))           # первый запрос занял место
        status, headers, body = self.get(f"/api/search?inn={INN2}")
        self.assertEqual(status, 503)
        self.assertEqual(headers["Retry-After"], "30")
        FakeClient.gate.set()
        first.join(5)
        self.assertEqual(self.get(f"/api/search?inn={INN2}")[0], 200)   # после освобождения - снова можно

    def test_cached_answers_do_not_need_a_queue_slot(self):
        self.start(Service(FakeClient, min_interval=0, max_queue=1))
        self.assertEqual(self.get(f"/api/search?inn={INN}")[0], 200)     # прогрели кэш
        FakeClient.gate, FakeClient.entered = threading.Event(), threading.Event()
        blocker = threading.Thread(target=self.get, args=(f"/api/search?inn={INN2}",))
        blocker.start()
        self.assertTrue(FakeClient.entered.wait(5))                      # очередь занята
        self.assertEqual(self.get(f"/api/search?inn={INN}")[0], 200)     # из кэша - без очереди
        self.assertEqual(self.get("/api/search?inn=7707083004")[0], 503)  # новый ИНН - отказ
        FakeClient.gate.set()
        blocker.join(5)


class RateLimiterTest(unittest.TestCase):
    def test_window_expires(self):
        limiter = RateLimiter(2, 0.05)
        self.assertEqual((limiter.check("a"), limiter.check("a")), (0.0, 0.0))
        self.assertGreater(limiter.check("a"), 0)
        self.assertEqual(limiter.check("b"), 0.0)             # у другого адреса свой счётчик
        time.sleep(0.06)
        self.assertEqual(limiter.check("a"), 0.0)


# ---- закрытая выгрузка «новые компании» (/leads) ----

import datetime
import tempfile

import leads
from leads_job import LeadsJob

TOKEN = "correct-horse-battery"


class LeadsWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.code = 0
        self.gate = None            # Event: пока не set(), выгрузка «идёт»
        self.error = None

    def fake_run(self, target, out, limit=0, log=print, **kwargs):
        self.calls.append((target, limit))
        log("Найдено организаций: 1")
        if self.gate:
            self.gate.wait(5)
        if self.error:
            raise self.error
        entry = {"status": leads.OK, "name": "ФИРМА", "director": "Иванов Иван", "email": "a@b.ru", "note": ""}
        leads.save_results(out, {"7707083004": entry})
        log("[1/1] 7707083004: ok")
        return self.code

    def start(self, token=TOKEN):
        job = LeadsJob(token, run=self.fake_run, out_dir=self.tmp.name)
        server = make_server("127.0.0.1", 0, Service(FakeClient, min_interval=0), RateLimiter(10000),
                             leads_job=job)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(AppTest.stop, server)
        self.job, self.base = job, f"http://127.0.0.1:{server.server_port}"
        return job

    def request(self, path, method="GET", body=None, token=TOKEN):
        headers = {"X-Leads-Token": token} if token is not None else {}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    # ---- доступ ----

    def test_disabled_without_a_sufficient_token(self):
        for token in ("", "short"):
            self.start(token)
            self.assertEqual(self.request("/leads")[0], 404)
            self.assertEqual(self.request("/api/leads/status", token="short")[0], 404)
            self.assertEqual(self.request("/api/leads/start", "POST", {"limit": 1}, token="short")[0], 404)
        self.assertEqual(self.calls, [])

    def test_wrong_or_missing_password(self):
        self.start()
        for token in ("wrong", None):
            self.assertEqual(self.request("/api/leads/status", token=token)[0], 401)
            self.assertEqual(self.request("/api/leads/start", "POST", {"limit": 1}, token=token)[0], 401)
        self.assertEqual(self.calls, [])

    def test_password_guessing_is_limited(self):
        self.start()
        codes = [self.request("/api/leads/status", token=f"guess-{i}")[0] for i in range(7)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_page_is_served_when_enabled(self):
        self.start()
        status, headers, body = self.request("/leads", token=None)      # сама страница без пароля - только форма
        self.assertEqual(status, 200)
        self.assertIn("Запустить".encode(), body)
        self.assertIn("Content-Security-Policy", headers)

    # ---- работа ----

    def test_full_run_and_download(self):
        job = self.start()
        status, _, body = self.request("/api/leads/start", "POST", {"date": "2026-10-09", "limit": 5})
        self.assertEqual((status, json.loads(body)["date"]), (202, "2026-10-09"))
        job.join(5)
        data = json.loads(self.request("/api/leads/status")[2])
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["counts"], {"ok": 1, "no_email": 0, "retry": 0})
        self.assertTrue(data["has_table"])
        self.assertIn("[1/1] 7707083004: ok", data["log"])
        self.assertEqual(self.calls, [(datetime.date(2026, 10, 9), 5)])

        status, headers, body = self.request("/api/leads/download")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn('filename="leads_2026-10-09.csv"', headers["Content-Disposition"])
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Название;ФИО директора;Почта".encode(), body)
        self.assertIn("ФИРМА;Иванов Иван;a@b.ru".encode(), body)

    def test_empty_date_means_today_in_moscow(self):
        job = self.start()
        self.request("/api/leads/start", "POST", {"date": "", "limit": 0})
        job.join(5)
        self.assertEqual(self.calls, [(leads.today_moscow(), 0)])

    def test_second_start_while_running_is_rejected(self):
        self.gate = threading.Event()
        job = self.start()
        self.assertEqual(self.request("/api/leads/start", "POST", {"limit": 1})[0], 202)
        self.assertEqual(self.request("/api/leads/start", "POST", {"limit": 1})[0], 409)
        self.assertEqual(json.loads(self.request("/api/leads/status")[2])["state"], "running")
        self.gate.set()
        job.join(5)
        self.assertEqual(self.request("/api/leads/start", "POST", {"limit": 1})[0], 202)   # снова можно
        job.join(5)

    def test_invalid_input_is_rejected(self):
        self.start()
        bad = [{"date": "вчера"}, {"date": "2026-13-45"}, {"limit": -1}, {"limit": 5000}, {"limit": "много"},
               [1, 2], "строка"]
        for body in bad:
            self.assertEqual(self.request("/api/leads/start", "POST", body)[0], 400, body)
        self.assertEqual(self.request("/api/leads/start", "POST", b"not json")[0], 400)
        self.assertEqual(self.request("/api/leads/start", "POST", b"")[0], 400)
        self.assertEqual(self.request("/api/leads/start", "POST", b"{" + b" " * 3000 + b"}")[0], 400)
        self.assertEqual(self.calls, [])

    def test_stop_and_error_states(self):
        job = self.start()
        self.code = 3
        self.request("/api/leads/start", "POST", {"limit": 1})
        job.join(5)
        data = json.loads(self.request("/api/leads/status")[2])
        self.assertEqual(data["state"], "stopped")
        self.assertIn("Checko", data["message"])

        self.error = RuntimeError("сломалось")
        self.request("/api/leads/start", "POST", {"limit": 1})
        job.join(5)
        data = json.loads(self.request("/api/leads/status")[2])
        self.assertEqual(data["state"], "error")
        self.assertIn("сломалось", data["message"])

    def test_download_before_any_run(self):
        self.start()
        self.assertEqual(self.request("/api/leads/download")[0], 404)
        self.assertEqual(json.loads(self.request("/api/leads/status")[2])["state"], "idle")

    def test_get_on_start_endpoint_does_nothing(self):
        self.start()
        self.assertEqual(self.request("/api/leads/start")[0], 404)
        self.assertEqual(self.calls, [])

    def test_public_site_is_unaffected(self):
        self.start()
        self.assertEqual(self.request("/healthz", token=None)[0], 200)
        self.assertEqual(self.request("/", token=None)[0], 200)


if __name__ == "__main__":
    unittest.main()
