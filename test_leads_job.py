import datetime
import os
import tempfile
import unittest
from types import SimpleNamespace

import leads
from leads_job import SHEET_BATCH, LeadsJob
from sheets_sync import SheetsError

DAY = datetime.date(2026, 10, 9)
OK_ENTRY = {"status": leads.OK, "name": "ООО А", "director": "Иванов Иван", "okved": "Разработка ПО", "email": "a@b.ru", "note": ""}


class FakeSheets:
    def __init__(self, known=(), fail_times=0, unreachable=False):
        self.known, self.fail_times, self.unreachable = set(known), fail_times, unreachable
        self.batches = []

    def checked_inns(self):
        if self.unreachable:
            raise SheetsError("нет связи со скриптом Google: тест")
        return set(self.known)

    def append(self, rows):
        if self.fail_times:
            self.fail_times -= 1
            raise SheetsError("временный сбой")
        self.batches.append(list(rows))
        return len(rows)


class LeadsJobSheetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kwargs, self.runs = {}, 0

    def run_job(self, sheets, inns, entry=OK_ENTRY, statuses=None):
        def fake_run(target, out, limit=0, log=print, skip=frozenset(), on_result=None, **kwargs):
            self.runs += 1
            self.kwargs = dict(kwargs, skip=skip)
            for inn in inns:
                if inn in skip:
                    continue
                result = dict(entry, status=(statuses or {}).get(inn, entry["status"]))
                if on_result:
                    on_result(SimpleNamespace(inn=inn), result)
            return 0

        job = LeadsJob("x" * 12, run=fake_run, out_dir=self.tmp.name, api=object(), sheets=sheets)
        job.start(DAY, 0)
        job.join(5)
        return job

    def test_rows_go_to_the_sheet_in_batches_and_the_rest_at_the_end(self):
        sheets = FakeSheets()
        inns = [f"77070{n:05d}" for n in range(SHEET_BATCH + 2)]
        job = self.run_job(sheets, inns)
        self.assertEqual([len(b) for b in sheets.batches], [SHEET_BATCH, 2])
        self.assertEqual(sheets.batches[0][0], {"inn": inns[0], "status": "ok", "date": "2026-10-09", "name": "ООО А",
                                                "director": "Иванов Иван", "email": "a@b.ru", "okved": "Разработка ПО"})
        self.assertEqual(job.status()["state"], "done")
        self.assertTrue(job.status()["sheets"])

    def test_inns_already_in_the_sheet_are_skipped_before_any_paid_request(self):
        sheets = FakeSheets(known={"7707000001"})
        self.run_job(sheets, ["7707000001", "7707000002"])
        self.assertEqual(self.kwargs["skip"], {"7707000001"})
        self.assertEqual([[r["inn"] for r in b] for b in sheets.batches], [["7707000002"]])

    def test_no_email_companies_are_remembered_too(self):
        sheets = FakeSheets()
        self.run_job(sheets, ["7707000001"], statuses={"7707000001": leads.NO_EMAIL})
        self.assertEqual(sheets.batches[0][0]["status"], "no_email")

    def test_unreachable_sheet_stops_before_the_run(self):
        job = self.run_job(FakeSheets(unreachable=True), ["7707000001"])
        self.assertEqual(self.runs, 0)                           # ни одного платного запроса
        status = job.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("скрипт", status["message"])

    def test_failed_write_is_retried_with_the_next_batch(self):
        sheets = FakeSheets(fail_times=1)
        inns = [f"77070{n:05d}" for n in range(SHEET_BATCH + 1)]
        job = self.run_job(sheets, inns)
        self.assertEqual([len(b) for b in sheets.batches], [SHEET_BATCH + 1])      # ничего не потеряно и не задвоено
        self.assertTrue(any("не записалось" in line for line in job.status()["log"]))
        self.assertNotIn("Часть строк", job.status()["message"])

    def test_write_that_never_succeeds_is_reported(self):
        sheets = FakeSheets(fail_times=99)
        job = self.run_job(sheets, ["7707000001"])
        self.assertEqual(sheets.batches, [])
        self.assertEqual(job.status()["state"], "done")
        self.assertIn("не записалась в Google-таблицу", job.status()["message"])

    def test_results_left_only_on_the_server_are_added_to_the_sheet(self):
        out = os.path.join(self.tmp.name, "leads_2026-10-09.csv")
        leads.save_results(out, {"7707000009": dict(OK_ENTRY), "7707000008": dict(OK_ENTRY, status=leads.RETRY)})
        sheets = FakeSheets()
        self.run_job(sheets, [])
        self.assertEqual([[r["inn"] for r in b] for b in sheets.batches], [["7707000009"]])    # RETRY не пишем

    def test_without_a_sheet_nothing_changes(self):
        job = self.run_job(None, ["7707000001"])
        self.assertNotIn("on_result", self.kwargs)
        self.assertFalse(job.status()["sheets"])


if __name__ == "__main__":
    unittest.main()
