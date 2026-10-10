import unittest

from letter_template import address_form, normalize_company, normalize_person, render_letter


class NormalizeTest(unittest.TestCase):
    def test_company_forms(self):
        cases = {
            "ооо ромашка": "ООО «Ромашка»",
            'ООО "РОМАШКА"': "ООО «Ромашка»",
            "ООО «Торговый Дом Ромашка»": "ООО «Торговый Дом Ромашка»",     # смешанный регистр не трогаем
            'АО "ТОРГОВЫЙ ДОМ РОМАШКА"': "АО «Торговый Дом Ромашка»",
            "ИП ИВАНОВ ИВАН ИВАНОВИЧ": "ИП Иванов Иван Иванович",
            "Ромашка": "«Ромашка»",
            "  ": "",
            "": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_company(raw), expected, raw)

    def test_person_and_address(self):
        self.assertEqual(normalize_person("петров сергей николаевич"), "Петров Сергей Николаевич")
        self.assertEqual(normalize_person("ИВАНОВА-ПЕТРОВА АННА"), "Иванова-Петрова Анна")
        self.assertEqual(address_form("ИВАНОВ ИВАН ИВАНОВИЧ"), "Иван Иванович")
        self.assertEqual(address_form("Иванов Иван"), "Иван")
        self.assertEqual(address_form("Иванов"), "")
        self.assertEqual(address_form(""), "")


class RenderTest(unittest.TestCase):
    def test_full_letter(self):
        subject, body = render_letter("ооо ромашка", "иванов иван иванович", "Торговля",
                                      "Пётр Петров, ООО «Пример»", "Предлагаю сотрудничество.")
        self.assertEqual(subject, "Вопрос для ООО «Ромашка»")
        self.assertTrue(body.startswith("Добрый день, Иван Иванович!\n\n"))
        self.assertIn("руководителю компании ООО «Ромашка» (сфера деятельности: торговля). Предлагаю сотрудничество.", body)
        self.assertIn("ответьте «Отказ»", body)
        self.assertTrue(body.endswith("С уважением,\nПётр Петров, ООО «Пример»"))

    def test_missing_data_gives_neutral_letter_without_gaps(self):
        subject, body = render_letter()
        self.assertEqual(subject, "Вопрос к руководителю")
        self.assertTrue(body.startswith("Добрый день!\n\n"))
        self.assertNotIn("()", body)
        self.assertNotIn("{", body)
        self.assertTrue(body.endswith("С уважением."))
        self.assertNotIn("  ", body.split("\n\n")[1])

    def test_no_stray_whitespace_in_blocks(self):
        _, body = render_letter("ромашка", "иванов иван иванович", "", "  Подпись  ", "  ")
        for block in body.split("\n\n"):
            self.assertEqual(block, block.strip())


if __name__ == "__main__":
    unittest.main()
