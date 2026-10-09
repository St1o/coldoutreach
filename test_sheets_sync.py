import unittest

import requests

from sheets_sync import CHECKED, SheetsError, SheetsSync

URL = "https://script.google.com/macros/s/ABC123/exec"
TOKEN = "SECRET-TOKEN-123"


class FakeResponse:
    def __init__(self, body=None, status=200):
        self._body, self.status_code = body, status

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def request(self, method, url, timeout=None, params=None, json=None):
        self.calls.append((method, url, params, json))
        if self.error:
            raise self.error
        return self.response


def sheets(response=None, error=None):
    session = FakeSession(response, error)
    return SheetsSync(URL, TOKEN, session), session


class SheetsSyncTest(unittest.TestCase):
    def test_checked_inns_are_the_first_column(self):
        api, session = sheets(FakeResponse({"header": ["ИНН", "Статус"], "rows": [["0105000001", "ok"], ["7707083893", "no_email"], []]}))
        self.assertEqual(api.checked_inns(), {"0105000001", "7707083893"})      # ведущий ноль не потерян
        self.assertEqual(session.calls[0], ("GET", URL, {"token": TOKEN, "sheet": CHECKED}, None))

    def test_table_is_a_list_of_dicts(self):
        api, session = sheets(FakeResponse({"header": ["Название", "Почта"], "rows": [["ООО А", "a@b.ru"], ["ООО Б", "c@d.ru"]]}))
        self.assertEqual(api.table(), [{"Название": "ООО А", "Почта": "a@b.ru"}, {"Название": "ООО Б", "Почта": "c@d.ru"}])
        self.assertEqual(session.calls[0][2], {"token": TOKEN, "sheet": "companies"})

    def test_append_sends_the_token_and_rows(self):
        rows = [{"inn": "7707083893", "status": "ok", "date": "2026-10-09", "name": "ООО А", "director": "Иванов Иван",
                 "email": "a@b.ru", "okved": "Разработка ПО"}]
        api, session = sheets(FakeResponse({"ok": True, "added": 1}))
        self.assertEqual(api.append(rows), 1)
        method, url, _, body = session.calls[0]
        self.assertEqual((method, url, body), ("POST", URL, {"token": TOKEN, "rows": rows}))

    def test_error_from_the_script_hides_the_token(self):
        api, _ = sheets(FakeResponse({"error": f"неверный пароль {TOKEN}"}))
        with self.assertRaises(SheetsError) as ctx:
            api.checked_inns()
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("неверный пароль", str(ctx.exception))

    def test_answer_that_is_not_json_explains_what_to_check(self):
        api, _ = sheets(FakeResponse(None, 200))
        with self.assertRaises(SheetsError) as ctx:
            api.append([])
        self.assertIn("доступ «Все»", str(ctx.exception))

    def test_network_error_hides_the_token_and_the_address(self):
        api, _ = sheets(error=requests.ConnectionError(f"failed {URL}?token={TOKEN}"))
        with self.assertRaises(SheetsError) as ctx:
            api.checked_inns()
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn("ABC123", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
