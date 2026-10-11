"""Форма организации (ООО, МБУ…) и отдельный шаблон письма для запасной группы."""
import csv
import os
import tempfile
import unittest

import compose
from compose import ComposeError, org_form
from mailing.agentmd import parse_form_rules, parse_letters, parse_segment, check_texts
from tests.test_compose import HEADER, LETTERS, LINK, SEGMENT, SIGNATURE, lead

SECRET = "СЕКРЕТНАЯ"          # часть названия компании: в списки для агента и в письма попадать не должна


class OrgFormTest(unittest.TestCase):
    def test_form_is_read_from_the_start_of_the_name(self):
        for name, expected in [('ООО <<ТАТЬЯНА>>', "ООО"), ('ООО "ТЕККИН"', "ООО"), ("ООО БЗИ", "ООО"),
                               ("ООО«Рога»", "ООО"), ('МБУ МП МЦ "КЦМИ"', "МБУ"), ('ПК "ГК "ТРИУМФ"', "ПК"),
                               ("АО «Завод»", "АО"), ("ИП Иванов И. И.", "ИП"), ("АНО «Помощь»", "АНО"),
                               ('Общество с ограниченной ответственностью "Х"', "ООО"),
                               ("  ооо в нижнем регистре", compose.OTHER_FORM)]:
            self.assertEqual(org_form(name), expected, name)

    def test_a_first_word_that_is_not_a_known_form_is_not_taken_for_one(self):
        for name in ("ТЕККИН", 'ПКФ "Ромашка"', "ТД Ромашка", ""):
            self.assertEqual(org_form(name), compose.OTHER_FORM, name)

    def test_no_name_column_means_the_form_is_not_checked(self):
        self.assertIsNone(org_form(None))


class ParseFormRulesTest(unittest.TestCase):
    TEXT = """## Карта
| 47.91 | Интернет | 1 | |

## Формы организаций
| Форма | Решение | Почему |
|---|---|---|
| ООО | писать | основной сегмент |
| **МБУ** | не писать | бюджетное учреждение |
| ПК | Не писать, пока не решим | кооператив |
| АО | писать, но другим текстом | |

## Заметки
| ТСЖ | писать | вне раздела «Формы», не читается |
"""

    def test_decisions_by_form(self):
        self.assertEqual(parse_form_rules(self.TEXT), {"ООО": True, "МБУ": False, "ПК": False, "АО": True})

    def test_no_forms_section_means_no_rules(self):
        self.assertEqual(parse_form_rules("## Карта\n| 47.91 | Интернет | 1 |\n"), {})

    def test_unclear_or_conflicting_decisions_are_errors(self):
        with self.assertRaises(ComposeError):
            parse_form_rules("## Формы\n| МБУ | может быть | |\n")
        with self.assertRaises(ComposeError):
            parse_form_rules("## Формы\n| МБУ | писать | |\n| МБУ | не писать | |\n")


def composer(**kw):
    groups = parse_segment(SEGMENT)
    texts = parse_letters(kw.pop("letters", LETTERS))
    check_texts(texts, groups)
    return compose.Composer(groups, texts, LINK, SIGNATURE, **kw)


class FormCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def first(self, comp, *rows):
        leads = [{"email": r[0], "okved": "47.91", "source": "ЕГРЮЛ", "form": r[1]} for r in rows]
        return compose.compose_first(comp, leads, "Тема", self.dir.name, frozenset(), frozenset())

    def test_by_default_only_ooo_is_written_and_the_other_forms_are_counted_for_the_agent(self):
        result = self.first(composer(), ("a@x.ru", "ООО"), ("b@x.ru", "МБУ"), ("c@x.ru", "ПК"), ("d@x.ru", "МБУ"),
                            ("e@x.ru", compose.OTHER_FORM))
        self.assertEqual([letter[0] for letter in result.letters], ["a@x.ru"])
        self.assertEqual(result.unknown_forms, {"МБУ": 2, "ПК": 1, compose.OTHER_FORM: 1})
        self.assertEqual(result.skipped["форма организации «МБУ»: решения агента ещё нет, по умолчанию пишем только ООО"], 2)
        self.assertEqual(result.skipped["форма организации не определена по названию: решения агента ещё нет, "
                                        "по умолчанию пишем только ООО"], 1)

    def test_the_agent_decision_beats_the_default_in_both_directions(self):
        comp = composer(form_rules={"ООО": False, "ПК": True, "МБУ": False})
        result = self.first(comp, ("a@x.ru", "ООО"), ("b@x.ru", "ПК"), ("c@x.ru", "МБУ"), ("d@x.ru", "АО"))
        self.assertEqual([letter[0] for letter in result.letters], ["b@x.ru"])
        self.assertEqual(result.skipped["форма организации «ООО»: агент решил не писать"], 1)
        self.assertEqual(result.skipped["форма организации «МБУ»: агент решил не писать"], 1)
        self.assertEqual(result.unknown_forms, {"АО": 1})                      # решения по АО нет

    def test_a_table_without_names_is_not_checked(self):
        result = self.first(composer(), ("a@x.ru", None), ("b@x.ru", None))
        self.assertEqual(len(result.letters), 2)
        self.assertFalse(result.unknown_forms)

    def test_a_skipped_form_does_not_ask_the_agent_about_its_okved(self):
        result = self.first(composer(), ("a@x.ru", "МБУ"))
        self.assertFalse(result.unmapped)


class ReadLeadsFormTest(unittest.TestCase):
    def test_only_the_form_is_kept_from_the_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "leads.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh, delimiter=";").writerows([
                    HEADER, lead("a@x.ru", "47.91", name=f'ООО "{SECRET}"'), lead("b@x.ru", "47.91", name='МБУ "КЦМИ"')])
            leads = compose.read_leads(path)
        self.assertEqual([item["form"] for item in leads], ["ООО", "МБУ"])
        self.assertNotIn(SECRET, repr(leads))

    def test_an_old_table_without_the_name_column_has_no_form(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "leads.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh, delimiter=";").writerows([["Почта", "ОКВЭД (код)", "Источник почты"], ["a@x.ru", "47.91", "ЕГРЮЛ"]])
            self.assertIsNone(compose.read_leads(path)[0]["form"])


class FormReportTest(unittest.TestCase):
    def test_the_report_names_forms_but_never_companies(self):
        with tempfile.TemporaryDirectory() as folder:
            comp = composer()
            path = compose.write_okved_report(folder, comp, compose.Counter(),
                                              compose.Counter({"МБУ": 2, compose.OTHER_FORM: 1}))
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("## Формы организаций без решения", text)
        self.assertIn("| МБУ | 2 |", text)
        self.assertIn("| (не определена по названию) | 1 |", text)
        self.assertNotIn("## Виды деятельности", text)                        # кодов без решения нет: раздела нет
        self.assertNotIn(SECRET, text)


class MainFormsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "data")
        self.agents = os.path.join(self.dir.name, "agents", "01-Segment", "output")
        os.makedirs(self.work)
        os.makedirs(self.agents)
        self.segment = os.path.join(self.agents, "segment.md")
        self.letters = os.path.join(self.agents, "letters.md")
        for path, text in [(self.segment, SEGMENT), (self.letters, LETTERS)]:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.leads = os.path.join(self.work, "leads.csv")
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows([
                HEADER, lead("a@x.ru", "47.91", name=f'ООО "{SECRET}"'), lead("b@x.ru", "47.91", name='МБУ "КЦМИ"'),
                lead("c@x.ru", "47.91", name='ПК "Ромашка"')])
        self.logs = []

    def go(self):
        return compose.main([self.leads, "--letters", self.letters, "--segment", self.segment,
                             "--env", os.path.join(self.dir.name, "нет.env"), "--link", LINK, "--signature", SIGNATURE],
                            log=self.logs.append)

    def groups_written(self):
        with open(os.path.join(self.work, "letters_1.csv"), encoding="utf-8-sig", newline="") as fh:
            return [row["Почта"] for row in csv.DictReader(fh, delimiter=";")]

    def test_by_default_only_ooo_is_written_and_the_dialog_says_why(self):
        self.assertEqual(self.go(), 0)
        output = "\n".join(self.logs)
        self.assertEqual(self.groups_written(), ["a@x.ru"])
        self.assertIn("формы организаций без решения агента: МБУ - 1, ПК - 1; пока пишем только ООО", output)
        with open(os.path.join(self.work, "okved_to_classify.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("| МБУ | 1 |", report)
        self.assertNotIn(SECRET, report)

    def test_the_forms_table_of_the_agent_is_picked_up_with_the_map(self):
        with open(os.path.join(self.agents, "okved-map.md"), "w", encoding="utf-8") as fh:
            fh.write("## Карта\n| 47.91 | Интернет | 1 | |\n\n## Формы\n| Форма | Решение | Почему |\n|---|---|---|\n"
                     "| ООО | писать | |\n| МБУ | не писать | бюджетное |\n| ПК | писать | |\n")
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.groups_written(), ["a@x.ru", "c@x.ru"])
        output = "\n".join(self.logs)
        self.assertIn("форма организации «МБУ»: агент решил не писать - 1", output)
        self.assertNotIn("формы организаций без решения агента", output)
        self.assertFalse(os.path.exists(os.path.join(self.work, "okved_to_classify.md")))     # решать больше нечего


# --- отдельный шаблон письма для запасной группы ------------------------------------------------------------

FALLBACK_LETTERS = LETTERS + """

## Письмо 1. Запасной шаблон (группа 12 и группы без своего текста)

```
Здравствуйте! Запасное письмо 1. {{боль_1}}
Опрос: {{ссылка_на_опрос}}
{{подпись}}
```

## Дожим. Запасной шаблон

```
Запасной дожим без блока боли. Опрос: {{ссылка_на_опрос}}
{{подпись}}
```

## Запасные значения подстановок

| Подстановка | Если пусто |
|---|---|
| `{{ссылка_на_опрос}}` | письмо не отправлять |
"""


class FallbackTemplateTest(unittest.TestCase):
    def test_the_fallback_templates_are_read_and_do_not_replace_the_main_ones(self):
        texts = parse_letters(FALLBACK_LETTERS)
        self.assertEqual(sorted(texts.fallback_templates), [1, 2])
        self.assertIn("Запасное письмо 1", texts.fallback_templates[1])
        self.assertIn("Это студия. Пишем вам", texts.templates[1])               # основной шаблон на месте
        self.assertIn("Это снова студия", texts.templates[2])
        self.assertEqual(texts.pain2["торговля"], "дожим торговли про остатки.")  # блоки боли основного дожима целы

    def test_written_groups_use_the_main_template_and_the_rest_the_fallback_one(self):
        comp = composer(letters=FALLBACK_LETTERS, include_unwritten=True)
        self.assertIn("Это студия. Пишем вам", comp.letter_text(1, "1"))             # торговля, текст написан
        self.assertIn("Запасное письмо 1. запасная боль.", comp.letter_text(1, "12"))
        self.assertIn("Запасное письмо 1. запасная боль.", comp.letter_text(1, "6"))   # группа без блока боли
        self.assertIn("Запасной дожим без блока боли", comp.letter_text(2, "12"))
        self.assertIn("Это последнее письмо", comp.letter_text(3, "12"))              # запасного шаблона для 3 нет

    def test_without_fallback_templates_nothing_changes(self):
        comp = composer(include_unwritten=True)
        self.assertIn("Это студия. Пишем вам", comp.letter_text(1, "12"))
        self.assertIn("запасная боль.", comp.letter_text(1, "12"))

    def test_a_fallback_template_makes_the_fallback_pain_blocks_unnecessary_for_its_stage(self):
        only_templates = FALLBACK_LETTERS.replace(
            "**Запасной вариант (группа 12, неизвестный ОКВЭД)**\nзапасная боль.\n\n", "").replace(
            "*Запасной вариант:* запасной дожим.\n\n", "")
        texts = parse_letters(only_templates)
        self.assertEqual((texts.fallback1, texts.fallback2), ("", ""))
        check_texts(texts, parse_segment(SEGMENT))                                   # не падает
        no_dozhim_template = only_templates.replace("## Дожим. Запасной шаблон", "## Что-то другое")
        with self.assertRaises(ComposeError):                                         # у дожима нет ни блока, ни шаблона
            check_texts(parse_letters(no_dozhim_template), parse_segment(SEGMENT))

    def test_a_signature_placeholder_only_in_a_fallback_template_is_still_noticed(self):
        letters = LETTERS.replace("{{подпись}}", "Студия") + FALLBACK_LETTERS[len(LETTERS):]   # подпись только у запасных
        texts = parse_letters(letters)
        self.assertFalse(any("{{подпись}}" in t for t in texts.templates.values()))
        self.assertTrue(compose.uses_placeholder(texts, "подпись"))
        with self.assertRaises(ComposeError):
            compose.Composer(parse_segment(SEGMENT), texts, LINK, "")                # подписи нет, а шаблон её просит

    def test_the_test_letters_show_the_fallback_template_for_the_fallback_group(self):
        comp = composer(letters=FALLBACK_LETTERS)
        result = compose.compose_test(comp, "Тема", 1, ["a@x.ru", "b@x.ru", "c@x.ru"])
        self.assertEqual([letter[3] for letter in result.letters], ["1", "3", "12"])
        self.assertIn("Запасное письмо 1", result.letters[2][2])


if __name__ == "__main__":
    unittest.main()
