"""Фильтр форм на странице /leads: организации форм «не писать» (ТСН, МБУ, ПК…) не запрашиваются в API Checko."""
import csv
import os
import re
import tempfile
import unittest

import compose
import leads
from leads_job import LeadsJob
from mailing import forms
from tests.test_compose import HEADER as COMPOSE_HEADER, lead as compose_lead
from tests.test_forms_and_fallback import composer
from tests.test_leads import (
    DAY, FakeApi, FakeChecko, FakeEgrul, LeadsCase, api_payload, leads_company)

A, B, C, D, E, F = (f"77070830{n:02d}" for n in range(1, 7))
INN = [A, B, C, D, E, F]

# Решения владельца на сегодня (раздел «Формы» файла okved-map.md)
OWNER_MAP = """## Карта
| 47.91 | Интернет | 1 | |

## Формы
| Форма | Решение | Почему |
|---|---|---|
| ООО | писать | основной сегмент |
| ЧУ | писать | |
| АНО | писать | |
| МБУ | не писать | бюджетное учреждение |
| ПК | не писать | кооператив |
| ТСН | не писать | собственники недвижимости |
"""
OWNER_RULES = {"ООО": True, "ЧУ": True, "АНО": True, "МБУ": False, "ПК": False, "ТСН": False}
NO_MAP_LINE = "правила форм не найдены: фильтр отключён (укажите OUTREACH_OKVED_MAP)"


class ForbiddenApi:
    """Подставной API Checko, который падает, если его вызвали: по этим организациям запросов быть не должно."""

    def __init__(self):
        self.asked = []

    def company(self, inn):
        self.asked.append(inn)
        raise AssertionError(f"запрос к API по {inn}: организация неподходящей формы не должна дойти до API")


def named(*pairs):
    """[(ИНН, название), ...] -> организации из списка Checko."""
    return [leads_company(inn, name) for inn, name in pairs]


def owner_filter(**decisions):
    rules = dict(OWNER_RULES)
    rules.update(decisions)
    return forms.FormFilter(rules)


class FormsCase(LeadsCase):
    def run_forms(self, api, companies, form_filter, **kwargs):
        """Выгрузка через API с фильтром форм, как её запускает сайт (LeadsJob._work)."""
        kwargs.setdefault("pause", 0)
        return leads.run(DAY, self.out, checko=FakeChecko(companies), egrul=FakeEgrul({}), log=self.logs.append,
                         form_filter=form_filter, process=lambda c: leads.process_company_api(api, c, form_filter),
                         **kwargs)

    def log_text(self):
        return "\n".join(self.logs)


class SkipByFormTest(FormsCase):
    def test_a_tsn_is_not_asked_from_the_api_and_is_saved_as_form_skipped(self):          # а
        api = ForbiddenApi()
        self.assertEqual(self.run_forms(api, named((A, 'ТСН "Рассвет"')), owner_filter()), 0)
        self.assertEqual(api.asked, [])
        entry = self.state()[A]
        self.assertEqual(entry["status"], "form_skipped")
        self.assertEqual(leads.FORM_SKIPPED, "form_skipped")
        self.assertEqual(entry["form"], "ТСН")
        self.assertEqual(entry["name"], 'ТСН "Рассвет"')
        self.assertEqual(entry["note"], "форма не соответствует: ТСН (решение: не писать)")
        self.assertEqual(self.read_csv(), [leads.FIELDS])                              # в таблицу не попадает

    def test_the_log_names_the_form_but_never_the_company_or_a_person(self):
        self.run_forms(ForbiddenApi(), named((A, 'ТСН "СЕКРЕТНОЕ"'), (B, 'МБУ "ТАЙНА"')), owner_filter())
        text = self.log_text()
        self.assertIn(f"[1/2] {A}: form_skipped (форма не соответствует: ТСН)", text)
        self.assertIn(f"[2/2] {B}: form_skipped (форма не соответствует: МБУ)", text)
        for secret in ("СЕКРЕТНОЕ", "ТАЙНА"):
            self.assertNotIn(secret, text)

    def test_the_final_line_sums_up_the_skipped_forms(self):
        companies = named((A, 'ТСН "Один"'), (B, 'МБУ "Два"'), (C, 'ТСН "Три"'), (D, 'ПК "Четыре"'), (E, 'ООО "Пять"'))
        api = FakeApi({E: api_payload("e@example.ru")})
        self.run_forms(api, companies, owner_filter())
        self.assertEqual(api.asked, [E])
        done = [line for line in self.logs if line.startswith("Готово.")]
        self.assertEqual(len(done), 1)
        self.assertIn("Пропущено по форме организации (запрос к API не делали): 4 (ТСН 2, МБУ 1, ПК 1)", done[0])
        self.assertEqual(leads.form_summary(self.state()), "ТСН 2, МБУ 1, ПК 1")

    def test_nothing_skipped_leaves_the_final_line_as_it_was(self):
        self.run_forms(FakeApi({A: api_payload("a@example.ru")}), named((A, 'ООО "Ромашка"')), owner_filter())
        self.assertNotIn("Пропущено по форме", self.log_text())

    def test_process_company_api_itself_never_calls_the_api_for_a_blocked_form(self):
        api = ForbiddenApi()
        entry = leads.process_company_api(api, leads_company(A, 'ПК "Колос"'), owner_filter())
        self.assertEqual((entry["status"], entry["form"], api.asked), ("form_skipped", "ПК", []))


class StillAskedTest(FormsCase):
    def test_ooo_is_asked(self):                                                         # б
        api = FakeApi({A: api_payload("a@example.ru")})
        self.run_forms(api, named((A, 'ООО "Ромашка"')), owner_filter())
        self.assertEqual(api.asked, [A])
        self.assertEqual(self.state()[A]["status"], leads.OK)

    def test_a_company_without_a_name_is_asked(self):                                    # в
        api = FakeApi({A: api_payload("a@example.ru")})
        self.run_forms(api, named((A, "")), owner_filter())
        self.assertEqual(api.asked, [A])
        self.assertEqual(self.state()[A]["status"], leads.OK)

    def test_the_other_form_is_asked(self):                                              # г
        names = ['ТЕККИН', 'ПКФ "Ромашка"', 'ТД Ромашка', "ооо в нижнем регистре"]
        self.assertEqual({forms.org_form(name) for name in names}, {forms.OTHER_FORM})
        api = FakeApi({inn: api_payload(f"{inn}@example.ru") for inn in INN[:4]})
        self.run_forms(api, named(*zip(INN, names)), owner_filter())
        self.assertEqual(api.asked, INN[:4])

    def test_a_form_without_a_decision_is_asked(self):
        api = FakeApi({A: api_payload("a@example.ru"), B: api_payload("b@example.ru")})
        self.run_forms(api, named((A, 'АО "Завод"'), (B, "ИП Иванов И. И.")), owner_filter())
        self.assertEqual(api.asked, [A, B])

    def test_forms_the_owner_allows_are_asked(self):
        api = FakeApi({A: api_payload("a@example.ru"), B: api_payload("b@example.ru")})
        self.run_forms(api, named((A, 'ЧУ "Школа"'), (B, 'АНО "Помощь"')), owner_filter())
        self.assertEqual(api.asked, [A, B])

    def test_without_a_filter_nothing_changes(self):
        api = FakeApi({A: api_payload("a@example.ru")})
        self.run_leads(FakeEgrul({}), FakeChecko(named((A, 'ТСН "Рассвет"'))),
                       process=lambda c: leads.process_company_api(api, c))
        self.assertEqual(api.asked, [A])
        self.assertNotIn("правила форм", self.log_text())


class NoRulesTest(FormsCase):
    def test_without_the_map_the_filter_is_off_and_the_log_says_so_once(self):           # д
        for path in ("", "   ", os.path.join(self.tmp.name, "нет-такого-файла.md")):
            self.logs.clear()
            filtered = forms.load_filter(path)
            self.assertFalse(filtered.active)
            api = FakeApi({A: api_payload("a@example.ru")})
            self.run_forms(api, named((A, 'ТСН "Рассвет"')), filtered)
            self.assertEqual(api.asked, [A], path)                                      # запрос идёт: правил нет
            self.assertEqual(self.logs.count(NO_MAP_LINE), 1, path)
            os.remove(self.out)
            os.remove(leads.state_path(self.out))

    def test_an_unreadable_map_switches_the_filter_off_and_says_why(self):
        path = os.path.join(self.tmp.name, "okved-map.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## Формы\n| МБУ | может быть | |\n")
        filtered = forms.load_filter(path)
        self.assertFalse(filtered.active)
        self.assertIn("правила форм не прочитаны: фильтр отключён", filtered.problem)
        self.assertIn("МБУ", filtered.problem)

    def test_a_map_without_the_forms_section_blocks_nobody(self):
        path = os.path.join(self.tmp.name, "okved-map.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## Карта\n| 47.91 | Интернет | 1 | |\n")
        filtered = forms.load_filter(path)
        self.assertTrue(filtered.active)
        self.assertFalse(filtered.blocked("ТСН"))


class RerunTest(FormsCase):
    def test_a_rerun_does_not_check_form_skipped_again(self):                            # е
        companies = named((A, 'ТСН "Рассвет"'), (B, 'ООО "Ромашка"'))
        self.run_forms(FakeApi({B: api_payload("b@example.ru")}), companies, owner_filter())
        self.logs.clear()
        api = ForbiddenApi()
        self.run_forms(api, companies, owner_filter())
        self.assertEqual(api.asked, [])
        self.assertEqual(self.state()[A]["status"], "form_skipped")
        self.assertNotIn("form_skipped (", self.log_text())                              # повторно не выписывается

    def test_when_the_rule_changes_to_write_the_company_is_checked(self):                # е
        companies = named((A, 'ТСН "Рассвет"'))
        self.run_forms(ForbiddenApi(), companies, owner_filter())
        api = FakeApi({A: api_payload("a@example.ru")})
        self.run_forms(api, companies, owner_filter(ТСН=True))
        self.assertEqual(api.asked, [A])
        self.assertEqual(self.state()[A]["status"], leads.OK)
        self.assertEqual(len(self.read_csv()), 2)

    def test_a_form_that_was_removed_from_the_table_is_checked_too(self):
        companies = named((A, 'ТСН "Рассвет"'))
        self.run_forms(ForbiddenApi(), companies, owner_filter())
        rules = dict(OWNER_RULES)
        del rules["ТСН"]
        api = FakeApi({A: api_payload("a@example.ru")})
        self.run_forms(api, companies, forms.FormFilter(rules))
        self.assertEqual(api.asked, [A])

    def test_while_the_rules_cannot_be_read_form_skipped_stays_as_it_is(self):
        companies = named((A, 'ТСН "Рассвет"'))
        self.run_forms(ForbiddenApi(), companies, owner_filter())
        api = ForbiddenApi()
        self.run_forms(api, companies, forms.load_filter(""))
        self.assertEqual(api.asked, [])
        self.assertEqual(self.state()[A]["status"], "form_skipped")

    def test_needs_check_by_state(self):
        skipped = {"status": leads.FORM_SKIPPED, "form": "ТСН"}
        self.assertFalse(leads.needs_check(skipped))
        self.assertFalse(leads.needs_check(skipped, owner_filter()))
        self.assertTrue(leads.needs_check(skipped, owner_filter(ТСН=True)))
        self.assertFalse(leads.needs_check(skipped, forms.load_filter("")))
        self.assertTrue(leads.needs_check(None, owner_filter()))
        self.assertTrue(leads.needs_check({"status": leads.RETRY}, owner_filter()))
        self.assertFalse(leads.needs_check({"status": leads.NO_EMAIL}, owner_filter()))

    def test_an_error_retry_of_a_blocked_form_becomes_form_skipped_without_a_query(self):
        self.run_forms(FakeApi({A: api_payload("")}), named((A, 'ООО "Ромашка"')), owner_filter(ООО=True))
        state = self.state()
        state[A]["status"] = leads.RETRY                                                 # в прошлый раз была ошибка
        leads.save_results(self.out, state)
        api = ForbiddenApi()
        self.run_forms(api, named((A, 'ООО "Ромашка"')), owner_filter(ООО=False))
        self.assertEqual((api.asked, self.state()[A]["status"]), ([], "form_skipped"))


class LimitAndPauseTest(FormsCase):
    def test_skipped_companies_do_not_use_up_the_limit(self):                            # ж
        companies = named((A, 'ТСН "Один"'), (B, 'МБУ "Два"'), (C, 'ТСН "Три"'), (D, 'ООО "Четыре"'), (E, 'ООО "Пять"'),
                          (F, 'ООО "Шесть"'))
        api = FakeApi({inn: api_payload(f"{inn}@example.ru") for inn in (D, E, F)})
        self.run_forms(api, companies, owner_filter(), limit=2)
        self.assertEqual(api.asked, [D, E])                                              # лимит 2 ушёл на настоящие запросы
        statuses = {inn: entry["status"] for inn, entry in self.state().items()}
        self.assertEqual(statuses, {A: "form_skipped", B: "form_skipped", C: "form_skipped", D: leads.OK, E: leads.OK})
        self.assertIn(f"[3/3] {C}: form_skipped", self.log_text())                      # все три пропуска учтены

    def test_skipped_companies_do_not_cost_a_pause(self):
        sleeps = []
        companies = named((A, 'ТСН "Один"'), (B, 'МБУ "Два"'), (C, 'ПК "Три"'))
        api = ForbiddenApi()
        self.run_forms(api, companies, owner_filter(), pause=5, sleep=sleeps.append)
        self.assertEqual((sleeps, api.asked), ([], []))
        sleeps.clear()
        companies = named((A, 'ТСН "Один"'), (D, 'ООО "Два"'), (B, 'МБУ "Три"'), (E, 'ООО "Четыре"'))
        self.run_forms(FakeApi({D: api_payload("d@example.ru"), E: api_payload("e@example.ru")}),
                       companies, owner_filter(), pause=5, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 5)                                                 # одна пауза между двумя запросами

    def test_the_other_process_function_is_not_called_for_a_skipped_company_either(self):
        called = []
        leads.run(DAY, self.out, pause=0, checko=FakeChecko(named((A, 'ТСН "Один"'), (B, 'ООО "Два"'))),
                  egrul=FakeEgrul({}), log=self.logs.append, form_filter=owner_filter(),
                  process=lambda c: called.append(c.inn) or {"status": leads.NO_EMAIL, "note": ""})
        self.assertEqual(called, [B])


class UnknownAndFullFormsTest(FormsCase):
    def test_a_form_the_owner_added_to_the_table_is_known_without_a_code_change(self):
        self.assertNotIn("ЖНК", forms.KNOWN_FORMS)
        name = 'ЖНК "Взаимопомощь"'
        self.assertEqual(forms.org_form(name), forms.OTHER_FORM)                         # без строки в таблице это «другая»
        api = ForbiddenApi()
        self.run_forms(api, named((A, name)), forms.FormFilter({"ЖНК": False}))
        self.assertEqual(api.asked, [])
        self.assertIn(f"[1/1] {A}: form_skipped (форма не соответствует: ЖНК)", self.log_text())
        self.assertEqual(self.state()[A]["form"], "ЖНК")

    def test_without_that_line_the_same_company_is_asked(self):
        api = FakeApi({A: api_payload("a@example.ru")})
        self.run_forms(api, named((A, 'ЖНК "Взаимопомощь"')), owner_filter())
        self.assertEqual(api.asked, [A])

    def test_a_form_written_in_full_is_recognized_too(self):
        api = ForbiddenApi()
        companies = named((A, "Товарищество собственников недвижимости «Рассвет»"),
                          (B, "ТОВАРИЩЕСТВО СОБСТВЕННИКОВ НЕДВИЖИМОСТИ \"ДОМ 5\""),
                          (C, 'Муниципальное бюджетное учреждение культуры "Дом"'),
                          (D, "ПРОИЗВОДСТВЕННЫЙ КООПЕРАТИВ «КОЛОС»"))
        self.run_forms(api, companies, owner_filter())
        self.assertEqual(api.asked, [])
        self.assertEqual({inn: e["form"] for inn, e in self.state().items()}, {A: "ТСН", B: "ТСН", C: "МБУ", D: "ПК"})

    def test_every_full_name_leads_to_a_known_form(self):
        self.assertTrue(forms.FULL_NAMES)
        for form, names in forms.FULL_NAMES.items():
            self.assertIn(form, forms.KNOWN_FORMS)
            for full in names:
                for spelling in (full, full.upper(), full.capitalize(), f"«{full.title()}» Х", full.replace("ё", "е"),
                                 full.replace("-", " ")):
                    self.assertEqual(forms.org_form(f'{spelling} "Х"'), form, spelling)

    def test_a_longer_word_is_not_taken_for_a_full_name(self):
        for name in ("Производственная компания «Х»", "Товарищество собственников", "Акционерное обществоХ",
                     'Садоводческий кооператив "Х"'):
            self.assertEqual(forms.org_form(name), forms.OTHER_FORM, name)

    def test_the_short_form_wins_and_the_ooo_full_name_still_works(self):
        self.assertEqual(forms.org_form('ТСН "Х"'), "ТСН")
        self.assertEqual(forms.org_form('Общество с ограниченной ответственностью "Х"'), "ООО")
        self.assertIsNone(forms.org_form(None))


class JobCase(unittest.TestCase):
    """LeadsJob целиком: правила читаются из файла при каждом запуске, ответ страницы содержит сводку."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map_path = os.path.join(self.tmp.name, "okved-map.md")
        self.companies = []
        self.api = ForbiddenApi()

    def write_map(self, text=OWNER_MAP):
        with open(self.map_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def fake_run(self, target, out, **kwargs):
        return leads.run(target, out, checko=FakeChecko(self.companies), egrul=FakeEgrul({}), **kwargs)

    def job(self, okved_map="map"):
        return LeadsJob("correct-horse-battery", run=self.fake_run, out_dir=self.tmp.name, api=self.api,
                        okved_map=self.map_path if okved_map == "map" else okved_map)

    def start(self, job, limit=0):
        job.start(DAY, limit)
        job.join(10)
        return job.status(DAY)


class JobFormsTest(JobCase):
    def test_status_has_the_counts_the_summary_and_the_table_without_skipped(self):
        self.write_map()
        self.companies = named((A, 'ТСН "Один"'), (B, 'МБУ "Два"'), (C, 'ТСН "Три"'), (D, 'ПК "Четыре"'))
        data = self.start(self.job())
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["counts"], {"ok": 0, "no_email": 0, "retry": 0, "form_skipped": 4})
        self.assertEqual(data["form_summary"], "ТСН 2, МБУ 1, ПК 1")
        self.assertEqual((data["rows"], data["row_notes"], data["forms_notice"]), ([], [], ""))
        self.assertEqual(self.api.asked, [])
        self.assertIn("Пропущено по форме организации (запрос к API не делали): 4 (ТСН 2, МБУ 1, ПК 1)",
                      " ".join(data["log"]))

    def test_the_rules_are_read_at_every_run_so_a_new_line_works_without_a_restart(self):
        self.write_map()
        job = self.job()
        self.companies = named((A, 'ТСН "Один"'))
        self.start(job)
        self.write_map(OWNER_MAP + "| ЖНК | не писать | решил владелец |\n")             # дописали строку, сайт не трогали
        self.companies = named((A, 'ТСН "Один"'), (B, 'ЖНК "Новая форма"'))
        data = self.start(job)
        self.assertEqual(self.api.asked, [])
        self.assertEqual(data["form_summary"], "ЖНК 1, ТСН 1")          # поровну - по алфавиту
        self.assertIn(f"{B}: form_skipped (форма не соответствует: ЖНК)", " ".join(data["log"]))

    def test_no_map_shows_a_notice_and_keeps_asking_the_api(self):                       # д
        self.api = FakeApi({A: api_payload("a@example.ru")})
        self.companies = named((A, 'ТСН "Один"'))
        for path in ("", os.path.join(self.tmp.name, "нет.md")):
            job = self.job(path)
            self.assertEqual(job.status()["forms_notice"], "правила форм не найдены, фильтр отключён")   # и до запуска
            data = self.start(job)
            self.assertEqual(data["forms_notice"], "правила форм не найдены, фильтр отключён")
            self.assertEqual(data["log"].count(NO_MAP_LINE), 1)
            self.assertEqual(data["counts"]["form_skipped"], 0)
            self.assertEqual(self.api.asked, [A])
            for name in os.listdir(self.tmp.name):
                if name.startswith("leads_"):
                    os.remove(os.path.join(self.tmp.name, name))
            self.api.asked.clear()

    def test_the_path_comes_from_outreach_okved_map_when_it_is_not_given_directly(self):
        self.write_map()
        job = self.job(okved_map=None)
        self.assertEqual(job.status()["forms_notice"], "правила форм не найдены, фильтр отключён")
        old = os.environ.get("OUTREACH_OKVED_MAP")
        os.environ["OUTREACH_OKVED_MAP"] = self.map_path
        try:
            self.assertEqual(job.status()["forms_notice"], "")
            self.assertTrue(job.form_filter().blocked("ТСН"))
        finally:
            if old is None:
                del os.environ["OUTREACH_OKVED_MAP"]
            else:
                os.environ["OUTREACH_OKVED_MAP"] = old

    def test_rows_collected_earlier_are_marked_and_the_counts_are_not_changed(self):
        entry = {"status": leads.OK, "name": 'ТСН "Рассвет"', "director": "Иванов Иван", "email": "a@example.ru",
                 "okved": "68.32", "okved_name": "Управление", "email_source": "ЕГРЮЛ", "note": ""}
        other = dict(entry, name='ООО "Ромашка"', email="b@example.ru")
        out = os.path.join(self.tmp.name, f"leads_{DAY.isoformat()}.csv")
        leads.save_results(out, {A: entry, B: other}, sent={})
        self.write_map()
        data = self.job().status(DAY)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["row_notes"], ["форма не соответствует: ТСН", ""])
        self.assertEqual(data["counts"]["ok"], 2)
        self.assertEqual(self.job("").status(DAY)["row_notes"], ["", ""])                # правил нет - пометок нет

    def test_the_state_file_keeps_the_form_and_the_note_for_a_later_run(self):
        self.write_map()
        self.companies = named((A, 'МБУ "Два"'))
        self.start(self.job())
        saved = leads.load_state(leads.state_path(os.path.join(self.tmp.name, f"leads_{DAY.isoformat()}.csv")))
        self.assertEqual(saved[A], {"status": "form_skipped", "name": 'МБУ "Два"', "form": "МБУ",
                                    "note": "форма не соответствует: МБУ (решение: не писать)"})


class ComposeUnchangedTest(unittest.TestCase):
    """compose.py после переноса форм в mailing/forms.py пропускает те же формы, что и раньше."""

    # Как org_form выглядела в compose.py до переноса (копия кода, не импорт).
    OLD_KNOWN = frozenset("""
        ООО ОАО ЗАО ПАО НАО АО ИП КФХ ПК СПК ТОО
        МБУ МАУ МКУ МУП МУ ГБУ ГАУ ГКУ ГУП ГУ ФГБУ ФГАУ ФГКУ ФГУП ФБУ КГБУ КГАУ КГКУ ОГБУ БУ АУ КУ
        МБОУ МАОУ МКОУ МБДОУ МАДОУ МКДОУ ГБОУ ГАОУ ГБПОУ ГАПОУ ФГБОУ ФГАОУ ГБДОУ МБУК МБУДО МАУДО МБУЗ ГБУЗ КГБУЗ
        АНО АНОО НКО НП ТСЖ ТСН СНТ ГСК ЖСК ТОС РОО МОО ОО ЧУ ЧОУ НОУ НОЧУ ЧУДО АНПО
    """.split())
    OLD_FORM_WORD = re.compile(r"^[\s«\"“<(]*([А-ЯЁ]{2,8})(?![А-ЯЁа-яё])")
    OLD_FULL_OOO = re.compile(r"^[\s«\"“<(]*общество\s+с\s+ограниченной\s+ответственностью", re.I)

    def old_org_form(self, name):
        if name is None:
            return None
        if self.OLD_FULL_OOO.match(name):
            return "ООО"
        found = self.OLD_FORM_WORD.match(name)
        return found.group(1) if found and found.group(1) in self.OLD_KNOWN else "другая"

    def names(self):
        names = ['ООО "ТЕККИН"', 'ООО <<ТАТЬЯНА>>', "ООО«Рога»", 'МБУ МП МЦ "КЦМИ"', 'ПК "ГК "ТРИУМФ"', "АО «Завод»",
                 "ИП Иванов И. И.", "АНО «Помощь»", 'Общество с ограниченной ответственностью "Х"', "  ооо в нижнем регистре",
                 "ТЕККИН", 'ПКФ "Ромашка"', "ТД Ромашка", "", "ЖНК \"Взаимопомощь\"", 'ТСН "Рассвет"']
        names += [f'{form} "Х"' for form in sorted(self.OLD_KNOWN)]
        names += [f"{form}-1" for form in ("ООО", "МБУ")]
        # полные названия других форм compose.py по-прежнему не узнаёт (это знает только /leads)
        names += [f'{full.capitalize()} "Х"' for names_ in forms.FULL_NAMES.values() for full in names_]
        return names

    def test_the_forms_are_the_same_as_before(self):                                     # з
        self.assertEqual(forms.KNOWN_FORMS, self.OLD_KNOWN)
        self.assertEqual((compose.KNOWN_FORMS, compose.DEFAULT_FORMS, compose.OTHER_FORM),
                         (self.OLD_KNOWN, frozenset({"ООО"}), "другая"))
        for name in self.names() + [None]:
            self.assertEqual(compose.org_form(name), self.old_org_form(name), name)

    def test_the_full_name_of_a_form_other_than_ooo_is_still_not_recognized_by_compose(self):
        self.assertEqual(compose.org_form("Товарищество собственников недвижимости «Рассвет»"), compose.OTHER_FORM)
        self.assertEqual(forms.org_form("Товарищество собственников недвижимости «Рассвет»"), "ТСН")

    def test_the_letters_go_to_the_same_companies_as_before(self):
        written = {
            'ООО "А"': True, 'ЧУ "Б"': True, 'АНО "В"': True, 'Общество с ограниченной ответственностью "Г"': True,
            'МБУ "Д"': False, 'ПК "Е"': False, 'ТСН "Ж"': False,                          # решение владельца: не писать
            'АО "З"': False, "ИП Иванов": False, "ТЕККИН": False,                         # решения нет: по умолчанию только ООО
            'Автономная некоммерческая организация "И"': False,                           # полное название: для compose «другая»
            "Товарищество собственников недвижимости «К»": False,
        }
        rows = [compose_lead(f"n{number}@x.ru", "47.91", name=name) for number, name in enumerate(written)]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "leads.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh, delimiter=";").writerows([COMPOSE_HEADER] + rows)
            leads_table = compose.read_leads(path)
            result = compose.compose_first(composer(form_rules=OWNER_RULES), leads_table, "Тема", folder, frozenset(),
                                           frozenset())
        sent = {letter[0] for letter in result.letters}
        self.assertEqual(sent, {f"n{number}@x.ru" for number, (name, ok) in enumerate(written.items()) if ok})
        # и без решений владельца: только ООО
        with tempfile.TemporaryDirectory() as folder:
            result = compose.compose_first(composer(), leads_table, "Тема", folder, frozenset(), frozenset())
        self.assertEqual({letter[0] for letter in result.letters}, {"n0@x.ru", "n3@x.ru"})


if __name__ == "__main__":
    unittest.main()
