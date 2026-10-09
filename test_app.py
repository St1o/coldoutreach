import json
import threading
import unittest
import urllib.error
import urllib.request

from app import Service, make_server
from egrul_inn_search import EgrulCaptchaRequired, EgrulError, EgrulRecord, EgrulTimeout

INN = "7707083893"
PDF = b"%PDF-1.7 fake"


class FakeClient:
    """Подставной клиент ЕГРЮЛ: считает обращения и может падать на заданном шаге."""
    searches = 0
    downloads = 0
    records = []
    search_error = None
    download_error = None

    def search_by_inn(self, inn):
        FakeClient.searches += 1
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
        self.server = make_server("127.0.0.1", 0, Service(FakeClient, min_interval=0))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get_json(self, path):
        status, _, body = self.get(path)
        return status, json.loads(body)

    def test_index_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Получить выписку".encode(), body)

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

    def test_extract_not_found(self):
        FakeClient.records = []
        status, data = self.get_json(f"/api/extract?inn={INN}")
        self.assertEqual(status, 404)
        self.assertEqual(FakeClient.downloads, 0)

    def test_error_mapping(self):
        cases = [(EgrulCaptchaRequired("c"), 429), (EgrulTimeout("t"), 504), (EgrulError("e"), 502)]
        for error, expected in cases:
            FakeClient.search_error = error
            status, data = self.get_json(f"/api/search?inn={INN}")
            self.assertEqual(status, expected, error)
            self.assertNotIn("e", data["error"].split("Traceback"))   # без трассировок
        FakeClient.search_error = EgrulCaptchaRequired("c")
        self.assertTrue(self.get_json(f"/api/search?inn={INN}")[1]["captcha"])

    def test_failed_download_drops_cache_so_next_try_searches_again(self):
        self.get_json(f"/api/search?inn={INN}")
        FakeClient.download_error = EgrulError("token expired")
        self.assertEqual(self.get(f"/api/extract?inn={INN}")[0], 502)
        FakeClient.download_error = None
        self.assertEqual(self.get(f"/api/extract?inn={INN}")[0], 200)
        self.assertEqual(FakeClient.searches, 2)

    def test_unknown_path(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/app.py")[0], 404)     # исходники не раздаются


if __name__ == "__main__":
    unittest.main()
