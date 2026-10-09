import os
import tempfile
import unittest

import requests

from egrul_inn_search import (
    EgrulCaptchaRequired, EgrulClient, EgrulError, EgrulTimeout, validate_inn,
)


class FakeResponse:
    def __init__(self, payload, status=200, content=b""):
        self._payload, self.status_code, self.content = payload, status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Подставная сессия: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, post_responses, get_responses):
        self.headers = {}
        self.post_responses = list(post_responses)
        self.get_responses = list(get_responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        response = self.post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if url.endswith("/index.html"):     # главная страница (cookie), в очередь не входит
            return FakeResponse({})
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


SBER_ROW = {"i": "7707083893", "o": "1027700132195", "p": "773601001", "k": "ul",
            "n": "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО \"СБЕРБАНК РОССИИ\"", "c": "ПАО СБЕРБАНК",
            "g": "директор", "r": "16.08.2002", "t": "rowtoken"}


def make_client(post, get):
    session = FakeSession(post, get)
    return EgrulClient(session=session, poll_interval=0, max_wait=0.05), session


class ValidateInnTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(validate_inn("7707083893"))      # ЮЛ, 10 цифр
        self.assertTrue(validate_inn("500100732259"))    # ИП, 12 цифр

    def test_bad_checksum(self):
        self.assertFalse(validate_inn("7707083894"))
        self.assertFalse(validate_inn("500100732258"))

    def test_bad_format(self):
        for bad in ("", "123", "77070838931", "77070838ab", " 7707083893", None, 7707083893):
            self.assertFalse(validate_inn(bad), bad)


class SearchByInnTest(unittest.TestCase):
    def test_polls_until_rows_ready(self):
        client, session = make_client(
            [FakeResponse({"t": "tok123", "captchaRequired": False})],
            [FakeResponse({"status": "wait"}), FakeResponse({"rows": [SBER_ROW]})])
        client.max_wait = 5
        records = client.search_by_inn("7707083893")

        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual((rec.inn, rec.ogrn, rec.kpp, rec.kind),
                         ("7707083893", "1027700132195", "773601001", "ЮЛ"))
        self.assertEqual(rec.short_name, "ПАО СБЕРБАНК")
        self.assertTrue(rec.is_active)
        # POST с ИНН, затем два GET по токену
        self.assertEqual([c[0] for c in session.calls], ["GET", "POST", "GET", "GET"])
        self.assertTrue(session.calls[0][1].endswith("/index.html"))
        self.assertEqual(session.calls[1][2]["query"], "7707083893")
        self.assertTrue(all(c[1].endswith("/search-result/tok123") for c in session.calls[2:]))

    def test_exact_filter(self):
        other = dict(SBER_ROW, i="7728168971")
        client, _ = make_client([FakeResponse({"t": "x"})],
                                [FakeResponse({"rows": [other, SBER_ROW]})])
        self.assertEqual([r.inn for r in client.search_by_inn("7707083893")], ["7707083893"])

        client, _ = make_client([FakeResponse({"t": "x"})],
                                [FakeResponse({"rows": [other, SBER_ROW]})])
        self.assertEqual(len(client.search_by_inn("7707083893", exact=False)), 2)

    def test_not_found_returns_empty_list(self):
        client, _ = make_client([FakeResponse({"t": "x"})], [FakeResponse({"rows": []})])
        self.assertEqual(client.search_by_inn("7707083893"), [])

    def test_terminated_entity(self):
        row = dict(SBER_ROW, e="01.01.2020")
        client, _ = make_client([FakeResponse({"t": "x"})], [FakeResponse({"rows": [row]})])
        self.assertFalse(client.search_by_inn("7707083893")[0].is_active)

    def test_invalid_inn_makes_no_requests(self):
        client, session = make_client([], [])
        with self.assertRaises(ValueError):
            client.search_by_inn("1234567890")
        self.assertEqual(session.calls, [])

    def test_captcha_required(self):
        client, _ = make_client([FakeResponse({"captchaRequired": True})], [])
        with self.assertRaises(EgrulCaptchaRequired):
            client.search_by_inn("7707083893")

    def test_timeout_when_never_ready(self):
        client, _ = make_client([FakeResponse({"t": "x"})],
                                [FakeResponse({"status": "wait"})] * 1000)
        with self.assertRaises(EgrulTimeout):
            client.search_by_inn("7707083893")

    def test_http_and_json_errors(self):
        client, _ = make_client([FakeResponse({}, status=503)], [])
        with self.assertRaises(EgrulError):
            client.search_by_inn("7707083893")
        client, _ = make_client([FakeResponse(None)], [])
        with self.assertRaises(EgrulError):
            client.search_by_inn("7707083893")

    def test_network_error_is_wrapped(self):
        client, _ = make_client([requests.ConnectionError("no route")], [])
        with self.assertRaises(EgrulError):
            client.search_by_inn("7707083893")

    def test_missing_token(self):
        client, _ = make_client([FakeResponse({"foo": "bar"})], [])
        with self.assertRaises(EgrulError):
            client.search_by_inn("7707083893")


class FailedResponse(FakeResponse):
    """Ответ с ошибкой HTTP, у которого есть тело (как у настоящего requests)."""

    def __init__(self, status, text="", payload=None):
        super().__init__(payload, status)
        self.text = text

    def raise_for_status(self):
        error = requests.HTTPError(f"{self.status_code} Client Error")
        error.response = self
        raise error


class ErrorBodyTest(unittest.TestCase):
    def test_error_shows_what_the_site_answered(self):
        client, _ = make_client([FailedResponse(400, text="  Bad request:\n  query is blocked  ")], [])
        with self.assertRaises(EgrulError) as ctx:
            client.search_by_inn("7707083893")
        self.assertIn("400", str(ctx.exception))
        self.assertIn("ответ сайта: Bad request: query is blocked", str(ctx.exception))

    def test_captcha_in_the_body_of_an_error_response(self):
        client, _ = make_client([FailedResponse(400, payload={"captchaRequired": True})], [])
        with self.assertRaises(EgrulCaptchaRequired):
            client.search_by_inn("7707083893")

    def test_long_body_is_cut(self):
        client, _ = make_client([FailedResponse(502, text="x" * 5000)], [])
        with self.assertRaises(EgrulError) as ctx:
            client.search_by_inn("7707083893")
        self.assertLess(len(str(ctx.exception)), 600)


PDF = b"%PDF-1.7 fake extract"


def extract_queue(rows, status_replies=(), download=None):
    """Очередь GET-ответов для полного цикла: поиск -> vyp-request -> vyp-status -> vyp-download."""
    queue = [FakeResponse({"rows": rows}), FakeResponse({"t": "dl1"})]
    queue += [FakeResponse(r) for r in status_replies] or [FakeResponse({"status": "ready"})]
    queue.append(download or FakeResponse(None, content=PDF))
    return queue


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = os.path.join(self.tmp.name, "vyp")   # папки ещё нет

    def client(self, post, get):
        client, session = make_client(post, get)
        client.max_wait = 5
        return client, session

    def test_full_cycle_saves_pdf(self):
        client, session = self.client(
            [FakeResponse({"t": "s1"})],
            extract_queue([SBER_ROW], [{"status": "wait"}, {"status": "ready"}]))
        res = client.fetch_extract("7707083893", self.out_dir)

        self.assertTrue(res.ok)
        self.assertEqual(res.path, os.path.join(self.out_dir, "7707083893.pdf"))
        with open(res.path, "rb") as fh:
            self.assertEqual(fh.read(), PDF)
        self.assertEqual(os.listdir(self.out_dir), ["7707083893.pdf"])   # без .part
        urls = [c[1].replace("https://egrul.nalog.ru", "") for c in session.calls]
        self.assertEqual(urls, [
            "/index.html", "/", "/search-result/s1", "/vyp-request/rowtoken",
            "/vyp-status/dl1", "/vyp-status/dl1", "/vyp-download/dl1"])

    def test_request_without_new_token_falls_back_to_row_token(self):
        queue = extract_queue([SBER_ROW])
        queue[1] = FakeResponse({})     # vyp-request не вернул "t"
        client, session = self.client([FakeResponse({"t": "s1"})], queue)
        client.fetch_extract("7707083893", self.out_dir)
        self.assertTrue(session.calls[-1][1].endswith("/vyp-download/rowtoken"))

    def test_prefers_active_record(self):
        old = dict(SBER_ROW, e="01.01.2020", t="old")
        new = dict(SBER_ROW, t="new")
        client, session = self.client([FakeResponse({"t": "s1"})], extract_queue([old, new]))
        res = client.fetch_extract("7707083893", self.out_dir)
        self.assertTrue(res.record.is_active)
        self.assertTrue(any(c[1].endswith("/vyp-request/new") for c in session.calls))

    def test_non_pdf_is_rejected_and_not_saved(self):
        html = FakeResponse(None, content=b"<html>captcha</html>")
        client, _ = self.client([FakeResponse({"t": "s1"})], extract_queue([SBER_ROW], download=html))
        with self.assertRaises(EgrulError):
            client.fetch_extract("7707083893", self.out_dir)
        self.assertFalse(os.path.exists(self.out_dir))

    def test_status_never_ready_times_out(self):
        queue = extract_queue([SBER_ROW], [{"status": "wait"}] * 1000)
        client, _ = self.client([FakeResponse({"t": "s1"})], queue)
        client.max_wait = 0.05
        with self.assertRaises(EgrulTimeout):
            client.fetch_extract("7707083893", self.out_dir)

    def test_nothing_found(self):
        client, _ = self.client([FakeResponse({"t": "s1"})], [FakeResponse({"rows": []})])
        with self.assertRaises(EgrulError):
            client.fetch_extract("7707083893", self.out_dir)

    def test_invalid_inn_cannot_escape_out_dir(self):
        client, session = self.client([], [])
        with self.assertRaises(ValueError):
            client.fetch_extract("../../etc/passwd", self.out_dir)
        self.assertEqual(session.calls, [])

    def test_batch_isolates_errors_and_skips_existing(self):
        os.makedirs(self.out_dir)
        existing = os.path.join(self.out_dir, "7707083893.pdf")
        with open(existing, "wb") as fh:
            fh.write(b"old")
        ip_row = dict(SBER_ROW, i="500100732259", k="fl", t="iprow")
        client, session = self.client([FakeResponse({"t": "s1"})], extract_queue([ip_row]))

        results = list(client.fetch_extracts(
            ["7707083893", "1234567890", "500100732259"], self.out_dir, pause=0))

        self.assertEqual([r.inn for r in results], ["7707083893", "1234567890", "500100732259"])
        self.assertTrue(results[0].skipped and results[0].ok)
        self.assertFalse(results[1].ok)                  # неверный ИНН
        self.assertTrue(results[2].ok and not results[2].skipped)
        with open(existing, "rb") as fh:
            self.assertEqual(fh.read(), b"old")          # существующий не тронут

    def test_overwrite_redownloads(self):
        os.makedirs(self.out_dir)
        path = os.path.join(self.out_dir, "7707083893.pdf")
        with open(path, "wb") as fh:
            fh.write(b"old")
        client, _ = self.client([FakeResponse({"t": "s1"})], extract_queue([SBER_ROW]))
        client.fetch_extract("7707083893", self.out_dir, overwrite=True)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), PDF)

    def test_batch_stops_on_captcha(self):
        client, _ = self.client([FakeResponse({"captchaRequired": True})], [])
        results = list(client.fetch_extracts(
            ["7707083893", "500100732259"], self.out_dir, pause=0))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].captcha and not results[0].ok)


if __name__ == "__main__":
    unittest.main()
