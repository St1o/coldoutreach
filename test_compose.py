import csv
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

import compose
import mailer
from compose import ComposeError

LINK = "https://docs.google.com/forms/d/e/ABC123/viewform"
SIGNATURE = "Анна, руководитель"

# Тексты ниже вымышлены, но устроены так же, как файлы агентов (Рассылка/01-Segment, Рассылка/02-Write).
SEGMENT = """# Сегмент

## 3. Ядро

| 1 | Ситуация | не группа |

## 4. Группы видов деятельности

| Группа (классы ОКВЭД) | Ситуация для письма 1 |
|---|---|
| 1. Торговля (45–47) | написано |
| 3. Строительство (41–43) | написано |
| 6. Услуги для бизнеса: консалтинг, маркетинг (70, 73) | не написано |
| 6б. Юристы, учёт (69, 71) | не написано |
| 11. Сельское хозяйство (01) | не написано |
| 12. Запасной: 02, 03, 05–09 и неизвестный ОКВЭД | запасной |

## 5. Предложение
| 1. Это не группа (99) | строка из другого раздела |
"""

LETTERS = """# Письма

## Темы
Одна тема на все три письма.
1. Первая тема (рекомендую)
2. Вторая тема

## Письмо 1. Шаблон

```
Здравствуйте!

Это студия. Пишем вам, потому что компания недавно зарегистрирована, вид деятельности — {{группа_словами}}.

Вопросы такого характера: {{боль_1}}

Опрос: {{ссылка_на_опрос}}

{{подпись}}
Студия
```

## Блоки боли для письма 1

**Торговля**
боль торговли про заказы; вторая строка боли.

**Строительство**
боль стройки про сметы.

**Запасной вариант (группа 12, неизвестный ОКВЭД)**
запасная боль.

## Дожим. Шаблон

```
Здравствуйте!

Это снова студия, компания недавно зарегистрирована, вид деятельности — {{группа_словами}}. Ещё вопрос: {{боль_2}}

Опрос: {{ссылка_на_опрос}}

{{подпись}}
```

**Блоки `{{боль_2}}`**

*Торговля:* дожим торговли про остатки.

*Строительство:* дожим стройки про объекты.

*Запасной вариант:* запасной дожим.

## Закрывающее письмо. Шаблон

```
Это последнее письмо, вид деятельности — {{группа_словами}}. Опрос: {{ссылка_на_опрос}}

{{подпись}}
```
"""

HEADER = ["Название", "ФИО директора", "Почта", "ОКВЭД (код)", "ОКВЭД (название)", "Источник почты"]
NAMES = ["Иванов Иван Иванович", "ООО СЕКРЕТНАЯ ФИРМА"]          # этого в письмах быть не должно


def lead(email, okved, source="ЕГРЮЛ", name=NAMES[1], director=NAMES[0]):
    return [name, director, email, okved, "", source]


def make_composer(**kw):
    groups = compose.parse_segment(SEGMENT)
    texts = compose.parse_letters(LETTERS)
    compose.check_texts(texts, groups)
    return compose.Composer(groups, texts, kw.pop("link", LINK), kw.pop("signature", SIGNATURE), **kw)


class ParseSegmentTest(unittest.TestCase):
    def setUp(self):
        self.groups = {group.id: group for group in compose.parse_segment(SEGMENT)}

    def test_groups_classes_and_names(self):
        self.assertEqual(list(self.groups), ["1", "3", "6", "6б", "11", "12"])
        self.assertEqual(self.groups["1"].classes, frozenset({45, 46, 47}))
        self.assertEqual(self.groups["6"].classes, frozenset({70, 73}))
        self.assertEqual(self.groups["6"].name, "Услуги для бизнеса: консалтинг, маркетинг")
        self.assertEqual(self.groups["11"].classes, frozenset({1}))
        self.assertEqual(self.groups["12"].classes, frozenset())

    def test_only_section_4_is_read(self):
        self.assertNotIn("99", [str(c) for g in self.groups.values() for c in g.classes])

    def test_words_are_the_lowercase_name_and_empty_for_the_fallback(self):
        self.assertEqual(self.groups["1"].words, "торговля")
        self.assertEqual(self.groups["12"].words, "")

    def test_missing_section_is_an_error(self):
        with self.assertRaises(ComposeError):
            compose.parse_segment("# Сегмент\n\n## 1. Вердикт\nтекст\n")


class OkvedTest(unittest.TestCase):
    def test_class_survives_excel_damage(self):
        for raw, expected in [("62.01", 62), ("01.11", 1), ("1.11", 1), ("1,11", 1), ("47", 47), (" 41.20 ", 41),
                              ("", None), ("abc", None), ("01.11.2026", 1)]:
            self.assertEqual(compose.okved_class(raw), expected, raw)

    def test_group_lookup_and_fallback(self):
        composer = make_composer()
        for code, group in [("47.91", "1"), ("41.20", "3"), ("01.11", "11"), ("1.11", "11"), ("70.22", "6"),
                            ("96.02", "12"), ("", "12"), ("мусор", "12")]:
            self.assertEqual(composer.group_for(code), group, code)


class ParseLettersTest(unittest.TestCase):
    def setUp(self):
        self.texts = compose.parse_letters(LETTERS)

    def test_subjects_mark_the_recommended_one(self):
        self.assertEqual(self.texts.subjects, [("Первая тема", True), ("Вторая тема", False)])
        self.assertEqual(compose.pick_subject(self.texts), "Первая тема")
        self.assertEqual(compose.pick_subject(self.texts, 2), "Вторая тема")
        with self.assertRaises(ComposeError):
            compose.pick_subject(self.texts, 3)

    def test_templates_pains_and_fallbacks(self):
        self.assertIn("{{группа_словами}}", self.texts.templates[1])
        self.assertIn("{{боль_2}}", self.texts.templates[2])
        self.assertIn("последнее", self.texts.templates[3])
        self.assertEqual(self.texts.pain1["торговля"], "боль торговли про заказы; вторая строка боли.")
        self.assertEqual(self.texts.pain2["строительство"], "дожим стройки про объекты.")
        self.assertEqual((self.texts.fallback1, self.texts.fallback2), ("запасная боль.", "запасной дожим."))

    def test_pain_block_for_an_unknown_group_is_an_error(self):
        broken = LETTERS.replace("**Строительство**", "**Космос**")
        with self.assertRaises(ComposeError) as ctx:
            compose.check_texts(compose.parse_letters(broken), compose.parse_segment(SEGMENT))
        self.assertIn("космос", str(ctx.exception))

    def test_missing_template_is_an_error(self):
        with self.assertRaises(ComposeError):
            compose.check_texts(compose.parse_letters("## Темы\n1. Т\n"), compose.parse_segment(SEGMENT))


class ValuesAndFillTest(unittest.TestCase):
    def test_link_rules(self):
        self.assertEqual(compose.check_link(" " + LINK + " "), LINK)
        for bad in ["", "https://forms.gle/abc", "https://example.com/forms/x",
                    "https://docs.google.com/forms/d/ABC/edit", "https://docs.google.com/forms/d/e/A B"]:
            with self.assertRaises(ComposeError, msg=bad):
                compose.check_link(bad)

    def test_signature_rules(self):
        self.assertEqual(compose.check_signature("Анна\\nруководитель"), "Анна\nруководитель")
        for bad in ["", "   ", "[ЗАПОЛНИТЬ]", "{{подпись}}"]:
            with self.assertRaises(ComposeError, msg=bad):
                compose.check_signature(bad)

    def test_composer_refuses_without_link_or_signature(self):
        with self.assertRaises(ComposeError):
            make_composer(link="")
        with self.assertRaises(ComposeError):
            make_composer(signature="")

    def test_signature_is_needed_only_when_the_templates_use_it(self):
        without = compose.parse_letters(LETTERS.replace("{{подпись}}\n", ""))
        groups = compose.parse_segment(SEGMENT)
        composer = compose.Composer(groups, without, LINK, "")                  # подпись не задана - и не нужна
        text = composer.letter_text(1, "1")
        self.assertNotIn("{{", text)
        self.assertNotIn(SIGNATURE, text)
        compose.Composer(groups, without, LINK, "[ЗАПОЛНИТЬ]")                  # лишнее значение не мешает
        with self.assertRaises(ComposeError) as ctx:
            compose.Composer(groups, compose.parse_letters(LETTERS), LINK, "")
        self.assertIn("{{подпись}}", str(ctx.exception))

    def test_empty_words_remove_the_whole_clause(self):
        text = make_composer().letter_text(1, "12")
        self.assertIn("недавно зарегистрирована.", text)
        self.assertNotIn("вид деятельности", text)
        self.assertIn("запасная боль.", text)

    def test_group_letter_uses_its_own_blocks(self):
        text = make_composer().letter_text(1, "1")
        self.assertIn("вид деятельности — торговля.", text)
        self.assertIn("боль торговли про заказы", text)
        self.assertIn(LINK, text)
        self.assertTrue(text.rstrip().endswith("Студия"))
        self.assertIn(SIGNATURE, text)
        self.assertIn("дожим торговли про остатки", make_composer().letter_text(2, "1"))

    def test_unknown_placeholder_and_extra_link_are_errors(self):
        composer = make_composer()
        composer.texts.templates[1] += "\n{{имя}}"
        with self.assertRaises(ComposeError):
            composer.letter_text(1, "1")
        composer = make_composer()
        composer.texts.fallback1 = "см. https://example.ru/x"
        with self.assertRaises(ComposeError) as ctx:
            composer.letter_text(1, "12")
        self.assertIn("ровно одна ссылка", str(ctx.exception))


class FirstLetterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.composer = make_composer()

    def run_first(self, leads, stoplist=frozenset(), responded=frozenset(), **kw):
        return compose.compose_first(self.composer, leads, "Тема", self.dir.name, stoplist, responded, **kw)

    def leads(self, *rows):
        return [{"email": r[2], "okved": r[3], "source": r[5]} for r in rows]

    def test_selection_and_reasons(self):
        result = self.run_first(self.leads(
            lead("a@x.ru", "47.91"), lead("b@x.ru", "41.20"), lead("c@x.ru", "96.02"),
            lead("d@x.ru", "47.91", source="контакты Checko"), lead("e@x.ru", "47.91", source=""),
            lead("A@x.ru", "47.91"), lead("не-адрес", "47.91"), lead("stop@x.ru", "47.91"),
            lead("done@x.ru", "47.91"), lead("filled@x.ru", "47.91"), lead("f@x.ru", "70.22")),
            stoplist=frozenset({"stop@x.ru"}), responded=frozenset({"filled@x.ru"}))
        self.assertEqual({letter[0]: letter[3] for letter in result.letters},
                         {"a@x.ru": "1", "b@x.ru": "3", "c@x.ru": "12", "done@x.ru": "1"})
        self.assertEqual(result.unwritten, {"6": 1})                 # группа 6 ещё без блока боли
        self.assertEqual(result.skipped["почта не из ЕГРЮЛ (контакты Checko или источник неизвестен)"], 2)
        self.assertEqual(result.skipped["повтор адреса (одна цепочка на адрес)"], 1)
        self.assertEqual(result.skipped["в стоп-листе"], 1)
        self.assertEqual(result.skipped["уже заполнил опрос"], 1)
        self.assertEqual(result.skipped["некорректный адрес"], 1)

    def test_unwritten_group_can_use_the_fallback_on_request(self):
        self.composer.include_unwritten = True
        result = self.run_first(self.leads(lead("f@x.ru", "70.22")))
        self.assertEqual([letter[3] for letter in result.letters], ["6"])
        self.assertIn("запасная боль.", result.letters[0][2])

    def test_addresses_already_in_any_journal_are_not_written_to_again(self):
        for stage, address in [(1, "one@x.ru"), (2, "two@x.ru"), (3, "three@x.ru")]:
            mailer.save_state(compose.journal_path(self.dir.name, stage), {address: {"status": "rejected"}})
        result = self.run_first(self.leads(lead("one@x.ru", "47.91"), lead("two@x.ru", "47.91"),
                                           lead("three@x.ru", "47.91"), lead("new@x.ru", "47.91")))
        self.assertEqual([letter[0] for letter in result.letters], ["new@x.ru"])

    def test_limit_takes_the_first_letters_in_table_order(self):
        result = self.run_first(self.leads(*[lead(f"a{i}@x.ru", "47.91") for i in range(5)]), limit=2)
        self.assertEqual([letter[0] for letter in result.letters], ["a0@x.ru", "a1@x.ru"])

    def test_names_and_directors_never_reach_the_letters(self):
        result = self.run_first(self.leads(lead("a@x.ru", "47.91")))
        everything = " ".join(" ".join(letter) for letter in result.letters)
        for secret in NAMES + ["Иванов", "СЕКРЕТНАЯ"]:
            self.assertNotIn(secret, everything)

    def test_output_is_readable_by_mailer_with_group_and_source(self):
        result = self.run_first(self.leads(lead("a@x.ru", "47.91"), lead("b@x.ru", "96.02")))
        path = os.path.join(self.dir.name, "letters_1.csv")
        compose.write_letters(path, result.letters)
        letters, problems = mailer.read_letters(path)
        self.assertEqual(problems, [])
        self.assertEqual([(l.email, l.meta) for l in letters],
                         [("a@x.ru", {"Группа": "1", "Источник адреса": "ЕГРЮЛ"}),
                          ("b@x.ru", {"Группа": "12", "Источник адреса": "ЕГРЮЛ"})])
        self.assertIn("\n", letters[0].body)                       # многострочный текст в кавычках сохранился


class BusinessDaysTest(unittest.TestCase):
    def test_weekends_do_not_count(self):
        friday, monday, thursday = date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 15)
        self.assertEqual(compose.business_days_since(friday, monday), 1)
        self.assertEqual(compose.business_days_since(monday, thursday), 3)
        self.assertEqual(compose.business_days_since(monday, monday), 0)


def sent(address, day, group="1", subject="Первая тема", status="sent", source="ЕГРЮЛ"):
    when = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
    entry = {"status": status, "time": when.isoformat(timespec="seconds"), "note": "", "subject": subject}
    if group:
        entry["meta"] = {"Группа": group, "Источник адреса": source}
    return address, entry


class FollowUpTest(unittest.TestCase):
    MONDAY = date(2026, 10, 5)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.composer = make_composer()

    def journal(self, stage, *entries):
        mailer.save_state(compose.journal_path(self.dir.name, stage), dict(entries))

    def second(self, today, stoplist=frozenset(), responded=frozenset(), **kw):
        return compose.compose_next(self.composer, 2, self.dir.name, stoplist, responded, today, **kw)

    def test_follow_up_after_three_business_days_with_the_same_subject(self):
        self.journal(1, sent("a@x.ru", self.MONDAY, "3", "Вторая тема"))
        self.assertEqual(self.second(self.MONDAY + timedelta(days=2)).letters, [])           # среда: 2 рабочих дня
        result = self.second(self.MONDAY + timedelta(days=3))                                 # четверг: 3
        (address, subject, text, group, source), = result.letters
        self.assertEqual((address, subject, group, source), ("a@x.ru", "Вторая тема", "3", "ЕГРЮЛ"))
        self.assertIn("дожим стройки про объекты", text)
        self.assertIn("вид деятельности — строительство", text)

    def test_exclusions(self):
        old = self.MONDAY
        self.journal(1,
                     sent("ok@x.ru", old), sent("stop@x.ru", old), sent("filled@x.ru", old),
                     sent("rej@x.ru", old, status="rejected"), sent("already@x.ru", old),
                     sent("nometa@x.ru", old, group=""))
        self.journal(2, sent("already@x.ru", old))
        result = self.second(old + timedelta(days=7), stoplist=frozenset({"stop@x.ru"}),
                             responded=frozenset({"filled@x.ru"}))
        self.assertEqual([letter[0] for letter in result.letters], ["ok@x.ru"])
        self.assertEqual(result.skipped["в стоп-листе"], 1)
        self.assertEqual(result.skipped["заполнил опрос"], 1)

    def test_closing_letter_needs_the_follow_up_and_seven_days_from_the_first_letter(self):
        self.journal(1, sent("a@x.ru", self.MONDAY), sent("b@x.ru", self.MONDAY))
        self.journal(2, sent("a@x.ru", self.MONDAY + timedelta(days=4)))
        too_early = compose.compose_next(self.composer, 3, self.dir.name, frozenset(), frozenset(),
                                         self.MONDAY + timedelta(days=6))
        self.assertEqual(too_early.letters, [])
        result = compose.compose_next(self.composer, 3, self.dir.name, frozenset(), frozenset(),
                                      self.MONDAY + timedelta(days=7))
        self.assertEqual([letter[0] for letter in result.letters], ["a@x.ru"])         # b не получал дожим
        self.assertIn("последнее", result.letters[0][2])

    def test_whole_chain_through_the_real_mailer_journal(self):
        first = compose.compose_first(self.composer, [{"email": "a@x.ru", "okved": "47.91", "source": "ЕГРЮЛ"}],
                                      "Тема", self.dir.name, frozenset(), frozenset())
        path = os.path.join(self.dir.name, "letters_1.csv")
        compose.write_letters(path, first.letters)

        class Server:
            sent = []

            def login(self, *a): pass
            def noop(self): return 250, b"ok"
            def send_message(self, msg): self.sent.append(msg)
            def quit(self): pass
            def close(self): pass

        server = Server()
        code = mailer.run(path, send=True, sender=mailer.MailruSender("me@mail.ru", "p", connect=lambda *a: server),
                          log=lambda m: None, unsubscribe=True)
        self.assertEqual(code, 0)
        self.assertEqual(server.sent[0]["List-Unsubscribe"], "<mailto:me@mail.ru?subject=unsubscribe>")
        today = datetime.now(timezone.utc).astimezone().date() + timedelta(days=10)
        result = self.second(today)
        self.assertEqual([(l[0], l[1], l[3]) for l in result.letters], [("a@x.ru", "Тема", "1")])


class TestLettersTest(unittest.TestCase):
    def test_one_letter_per_address_groups_in_turn_fallback_last(self):
        result = compose.compose_test(make_composer(), "Тема", 1, ["me@x.ru", "me2@x.ru", "me3@x.ru", "me4@x.ru"])
        self.assertEqual([letter[3] for letter in result.letters], ["1", "3", "12", "1"])
        self.assertEqual({letter[4] for letter in result.letters}, {"тест"})


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "data")
        self.project = os.path.join(self.dir.name, "agents")
        os.makedirs(self.work)
        os.makedirs(os.path.join(self.project, "Рассылка"))
        open(os.path.join(self.project, "CLAUDE.md"), "w").close()
        self.letters = os.path.join(self.project, "Рассылка", "letters.md")
        self.segment = os.path.join(self.project, "Рассылка", "segment.md")
        for path, text in [(self.letters, LETTERS), (self.segment, SEGMENT)]:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.leads = os.path.join(self.work, "leads.csv")
        self.write_leads([HEADER, lead("a@x.ru", "47.91"), lead("b@x.ru", "96.02")])
        self.logs = []
        self.nowhere = os.path.join(self.dir.name, "нет.env")

    def write_leads(self, rows):
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)

    def go(self, *extra, leads=None, **kw):
        argv = [leads or self.leads, "--letters", self.letters, "--segment", self.segment, "--env", self.nowhere,
                "--link", LINK, "--signature", SIGNATURE, *extra]
        return compose.main(argv, log=self.logs.append, **kw)

    def output(self):
        return "\n".join(self.logs)

    def test_writes_letters_next_to_the_table(self):
        self.assertEqual(self.go(), 0)
        letters, problems = mailer.read_letters(os.path.join(self.work, "letters_1.csv"))
        self.assertEqual((len(letters), problems), (2, []))
        self.assertIn("python mailer.py", self.output())

    def test_link_and_signature_may_come_from_local_env(self):
        env = os.path.join(self.dir.name, "local.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write(f"SURVEY_LINK={LINK}\nMAIL_SIGNATURE=Анна\\nруководитель\n")
        from unittest import mock
        with mock.patch.dict(os.environ):
            os.environ.pop("SURVEY_LINK", None)
            os.environ.pop("MAIL_SIGNATURE", None)
            code = compose.main([self.leads, "--letters", self.letters, "--segment", self.segment, "--env", env],
                                log=self.logs.append)
        self.assertEqual(code, 0)
        letters, _ = mailer.read_letters(os.path.join(self.work, "letters_1.csv"))
        self.assertIn("Анна\nруководитель", letters[0].body)

    def test_agent_file_paths_may_come_from_local_env(self):
        env = os.path.join(self.dir.name, "local.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write(f"OUTREACH_LETTERS={self.letters}\nOUTREACH_SEGMENT={self.segment}\n")
        from unittest import mock
        with mock.patch.dict(os.environ):
            for key in ("OUTREACH_LETTERS", "OUTREACH_SEGMENT"):
                os.environ.pop(key, None)
            code = compose.main([self.leads, "--env", env, "--link", LINK, "--signature", SIGNATURE],
                                log=self.logs.append)
            self.assertEqual(code, 0)
            for key in ("OUTREACH_LETTERS", "OUTREACH_SEGMENT"):
                os.environ.pop(key, None)               # первый вызов записал их в окружение
            self.assertEqual(compose.main([self.leads, "--env", self.nowhere, "--link", LINK,
                                           "--signature", SIGNATURE], log=self.logs.append), 1)
        self.assertIn("OUTREACH_LETTERS", self.output())

    def test_no_link_means_no_file(self):
        from unittest import mock
        with mock.patch.dict(os.environ):
            os.environ.pop("SURVEY_LINK", None)
            code = compose.main([self.leads, "--letters", self.letters, "--segment", self.segment,
                                 "--env", self.nowhere, "--signature", SIGNATURE], log=self.logs.append)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.work, "letters_1.csv")))

    def test_old_table_without_okved_columns_is_explained(self):
        self.write_leads([["Название", "ФИО директора", "Почта"], ["ООО", "Иванов", "a@x.ru"]])
        self.assertEqual(self.go(), 1)
        self.assertIn("ОКВЭД (код)", self.output())

    def test_table_inside_the_agents_project_is_refused(self):
        inside = os.path.join(self.project, "Рассылка", "leads.csv")
        self.write_leads([HEADER, lead("a@x.ru", "47.91")])
        os.replace(self.leads, inside)
        self.assertEqual(self.go(leads=inside), 1)
        self.assertIn("папке проекта агентов", self.output())
        self.assertFalse(os.path.exists(os.path.join(self.project, "Рассылка", "letters_1.csv")))

    def test_responses_file_and_stoplist_next_to_the_table_are_used(self):
        with open(os.path.join(self.work, "stoplist.txt"), "w", encoding="utf-8") as fh:
            fh.write("a@x.ru\n")
        with open(os.path.join(self.work, "responses.csv"), "w", encoding="utf-8") as fh:
            fh.write("Время;Контакт\n1;Пишите на B@x.ru или в Telegram\n")
        self.assertEqual(self.go(), 0)
        self.assertIn("Писать некому", self.output())
        self.assertFalse(os.path.exists(os.path.join(self.work, "letters_1.csv")))

    def test_subject_option_and_preview(self):
        self.assertEqual(self.go("--subject", "2", "--preview"), 0)
        letters, _ = mailer.read_letters(os.path.join(self.work, "letters_1.csv"))
        self.assertEqual({l.subject for l in letters}, {"Вторая тема"})
        self.assertIn("пример, группа 1", self.output())
        self.assertNotIn("a@x.ru", self.output().split("Файл:")[0])           # адресов на экране нет

    def test_test_mode_does_not_read_the_table(self):
        code = self.go("--test-to", "me@x.ru,me2@x.ru", leads=os.path.join(self.work, "нет.csv"))
        self.assertEqual(code, 0)
        letters, _ = mailer.read_letters(os.path.join(self.work, "letters_test.csv"))
        self.assertEqual([l.email for l in letters], ["me@x.ru", "me2@x.ru"])
        self.assertEqual(self.go("--test-to", "не-адрес", leads=os.path.join(self.work, "нет.csv")), 1)

    def test_follow_up_stage_from_the_journal(self):
        mailer.save_state(compose.journal_path(self.work, 1), dict([sent("a@x.ru", date(2026, 10, 5), "1")]))
        self.assertEqual(self.go("--stage", "2", today=date(2026, 10, 9)), 0)
        letters, _ = mailer.read_letters(os.path.join(self.work, "letters_2.csv"))
        self.assertEqual([l.email for l in letters], ["a@x.ru"])
        self.assertIn("Ещё вопрос", letters[0].body)


if __name__ == "__main__":
    unittest.main()
