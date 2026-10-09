import os
import unittest

from egrul_pdf import ExtractError, find_director, find_email, parse_extract, pdf_to_text

# Раскладка строк как в настоящих выписках (pypdf), данные вымышленные.
WITH_EMAIL = """\
Адрес электронной почты
10 E-mail INFO@EXAMPLE.RU
11 ГРН и дата внесения в ЕГРЮЛ записи,
содержащей указанные сведения
1222300047771
09.09.2022
Сведения о регистрации
12 Способ образования Создание юридического лица
Сведения о лице, имеющем право без доверенности действовать от имени юридического
лица
25 ГРН и дата внесения в ЕГРЮЛ сведений о
данном лице
1222300047771
09.09.2022
Страница 2 из
Выписка из ЕГРЮЛ
09.10.2026 21:42 ОГРН 1222300047771
11
26 Фамилия
Имя
Отчество
ИВАНОВ
ИВАН
ИВАНОВИЧ
27 ИНН 123456789047
28 ГРН и дата внесения в ЕГРЮЛ записи,
содержащей указанные сведения
1222300047771
09.09.2022
29 Должность ГЕНЕРАЛЬНЫЙ ДИРЕКТОР
Сведения об уставном капитале / складочном капитале / уставном фонде / паевом фонде
31 Вид УСТАВНЫЙ КАПИТАЛ
Сведения об участниках / учредителях юридического лица
38 Фамилия
Имя
Отчество
ПЕТРОВ
ПЁТР
ПЕТРОВИЧ
39 ИНН 987654321098
"""

WITHOUT_EMAIL = WITH_EMAIL.replace(
    "Адрес электронной почты\n10 E-mail INFO@EXAMPLE.RU\n", "")


def with_director(block: str) -> str:
    return ("Сведения о лице, имеющем право без доверенности действовать от имени юридического\n"
            "лица\n15 ГРН и дата внесения в ЕГРЮЛ сведений о\nданном лице\n2176196996512\n29.09.2017\n"
            + block + "\nСведения об уставном капитале\n")


class OkvedTest(unittest.TestCase):
    def test_code_and_name_on_one_line(self):
        text = ("Сведения об основном виде деятельности\n"
                "50 Код и наименование вида деятельности 62.01 Разработка компьютерного\nпрограммного обеспечения\n"
                "51 ГРН и дата внесения в ЕГРЮЛ записи\n1222300047771\n09.09.2022\n")
        data = parse_extract(text)
        self.assertEqual((data.okved, data.okved_name), ("62.01", "Разработка компьютерного программного обеспечения"))

    def test_code_and_name_in_separate_lines(self):
        text = ("Сведения об основном виде деятельности\n50 Код и наименование вида деятельности\n"
                "47.19.1\nТорговля розничная прочая в неспециализированных магазинах\n"
                "51 ГРН и дата внесения\n")
        data = parse_extract(text)
        self.assertEqual((data.okved, data.okved_name),
                         ("47.19.1", "Торговля розничная прочая в неспециализированных магазинах"))

    def test_additional_activities_are_not_taken(self):
        text = ("Сведения об основном виде деятельности\n50 Код 62.01 Разработка\n51 ГРН и дата\n"
                "Сведения о дополнительных видах деятельности\n60 Код 47.91 Торговля по почте\n")
        self.assertEqual(parse_extract(text).okved, "62.01")

    def test_section_numbers_and_dates_are_not_mistaken_for_a_code(self):
        text = "Сведения об основном виде деятельности\n50 Код и наименование вида деятельности\n51 ГРН 09.09.2022\n"
        self.assertEqual(parse_extract(text).okved, "")

    def test_no_section_no_okved(self):
        data = parse_extract(WITH_EMAIL)
        self.assertEqual((data.okved, data.okved_name), ("", ""))


class DirectorTest(unittest.TestCase):
    def test_columns_layout_with_page_break(self):
        self.assertEqual(parse_extract(WITH_EMAIL).director, "Иванов Иван Иванович")

    def test_takes_director_not_founder(self):
        # у учредителя другие ФИО, они идут позже
        self.assertNotIn("Петров", parse_extract(WITH_EMAIL).director)

    def test_hyphenated_surname_and_title_case(self):
        text = with_director("16 Фамилия\nИмя\nОтчество\nИВАНОВ-ПЕТРОВ\nАННА\nСЕРГЕЕВНА\n17 ИНН 1")
        self.assertEqual(parse_extract(text).director, "Иванов-Петров Анна Сергеевна")

    def test_no_patronymic(self):
        text = with_director("16 Фамилия\nИмя\nКИМ\nЛИ\n17 ИНН 1")
        self.assertEqual(parse_extract(text).director, "Ким Ли")

    def test_rows_layout_fallback(self):
        text = with_director("Фамилия СИДОРОВ Имя СИДОР Отчество СИДОРОВИЧ ИНН 1")
        self.assertEqual(parse_extract(text).director, "Сидоров Сидор Сидорович")

    def test_entrepreneur(self):
        text = "Сведения об индивидуальном предпринимателе\n2 Фамилия\nИмя\nОтчество\nСМИРНОВ\nАЛЕКСЕЙ\nПЕТРОВИЧ\n3 ИНН 1"
        self.assertEqual(find_director(" ".join(text.split())), "Смирнов Алексей Петрович")

    def test_management_company_does_not_pick_a_founder(self):
        # руководитель - управляющая организация: в разделе нет ФИО, дальше идут учредители
        text = (with_director("16 Полное наименование УК ООО РОМАШКА")
                + "Сведения об участниках / учредителях\n38 Фамилия\nИмя\nОтчество\nПЕТРОВ\nПЁТР\nПЕТРОВИЧ\n39 ИНН 1")
        self.assertEqual(parse_extract(text).director, "")

    def test_nothing_found(self):
        self.assertEqual(parse_extract("просто текст").director, "")


class EmailTest(unittest.TestCase):
    def test_email_row(self):
        self.assertEqual(parse_extract(WITH_EMAIL).email, "info@example.ru")

    def test_no_email_section(self):
        self.assertEqual(parse_extract(WITHOUT_EMAIL).email, "")

    def test_wrapped_email_is_joined(self):
        text = "10 E-mail VERY.LONG.ADDRESS@EXAMPLE-\nCOMPANY.RU\n11 ГРН и дата внесения"
        self.assertEqual(find_email(" ".join(text.split())), "very.long.address@example-company.ru")

    def test_tax_office_address_is_ignored(self):
        self.assertEqual(find_email("10 E-mail INSPECTOR@NALOG.RU 11 ГРН"), "")
        self.assertEqual(find_email("пишите на 7700@mail.nalog.ru"), "")

    def test_unlabelled_email_as_last_resort(self):
        self.assertEqual(find_email("Контакты sales@Example.ru далее"), "sales@example.ru")

    def test_no_email_at_all(self):
        self.assertEqual(find_email("ничего похожего 12345"), "")


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _reportlab():
    try:
        import reportlab  # noqa: F401
        return os.path.exists(FONT)
    except ImportError:
        return False


@unittest.skipUnless(_reportlab(), "для создания тестового PDF нужен reportlab и шрифт DejaVu")
class PdfRoundTripTest(unittest.TestCase):
    def make_pdf(self, text: str) -> bytes:
        import io
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
        pdfmetrics.registerFont(TTFont("DejaVu", FONT))
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        pdf.setFont("DejaVu", 9)
        y = 800
        for line in text.splitlines():
            pdf.drawString(40, y, line)
            y -= 12
            if y < 40:
                pdf.showPage()
                pdf.setFont("DejaVu", 9)
                y = 800
        pdf.save()
        return buf.getvalue()

    def test_text_pdf_is_parsed(self):
        data = parse_extract(pdf_to_text(self.make_pdf(WITH_EMAIL)))
        self.assertEqual((data.director, data.email), ("Иванов Иван Иванович", "info@example.ru"))

    def test_pdf_without_email(self):
        data = parse_extract(pdf_to_text(self.make_pdf(WITHOUT_EMAIL)))
        self.assertEqual((data.director, data.email), ("Иванов Иван Иванович", ""))


class BrokenPdfTest(unittest.TestCase):
    def test_garbage_raises_extract_error(self):
        with self.assertRaises(ExtractError):
            pdf_to_text(b"not a pdf")


if __name__ == "__main__":
    unittest.main()
