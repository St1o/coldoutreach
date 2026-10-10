"""Шаблон первого письма: название компании, ФИО директора, сфера -> тема и текст.

Данные из ЕГРЮЛ приходят в разном виде (ООО "РОМАШКА", ИВАНОВ ИВАН ИВАНОВИЧ), поэтому перед вставкой
в письмо название и ФИО приводятся к обычному виду: ООО «Ромашка», Иван Иванович.
"""
import re

LEGAL_FORMS = ("ООО", "АО", "ПАО", "ЗАО", "НАО", "ОАО", "АНО", "НКО", "МУП", "ГУП", "ИП")
_QUOTES = re.compile("[\"'«»“”„‟]")

DEFAULT_OFFER = "Хочу коротко рассказать, чем мы можем быть полезны вашей компании."


def _capitalize(word: str) -> str:
    return "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))


def _title(text: str) -> str:
    return " ".join(_capitalize(word) for word in text.split())


def normalize_person(full_name: str) -> str:
    """«ПЕТРОВ сергей николаевич» -> «Петров Сергей Николаевич»."""
    return _title(full_name or "")


def address_form(full_name: str) -> str:
    """Обращение «Имя Отчество» из «Фамилия Имя Отчество». Если имени не видно - пустая строка."""
    parts = normalize_person(full_name).split()
    return " ".join(parts[1:]) if len(parts) >= 2 else ""


def normalize_company(raw: str) -> str:
    """ооо ромашка / ООО "РОМАШКА" -> ООО «Ромашка»; ИП ИВАНОВ ИВАН -> ИП Иванов Иван."""
    text = " ".join(_QUOTES.sub(" ", raw or "").split())
    if not text:
        return ""
    first, _, rest = text.partition(" ")
    form = first.upper() if first.upper() in LEGAL_FORMS else ""
    name = rest if form else text
    if name.isupper() or name.islower():            # смешанный регистр - значит, так и задумано
        name = _title(name)
    if form == "ИП":
        return f"ИП {name}".strip()
    if not name:
        return form
    return f"{form} «{name}»".strip()


def render_letter(company: str = "", director: str = "", okved: str = "", signature: str = "",
                  offer: str = DEFAULT_OFFER):
    """Возвращает (тема, текст)."""
    name = normalize_company(company)
    addressee = address_form(director)
    sphere = (okved or "").strip().lower()

    subject = f"Вопрос для {name}" if name else "Вопрос к руководителю"
    greeting = f"Добрый день, {addressee}!" if addressee else "Добрый день!"
    whom = f"компании {name}" if name else "компании"
    intro = f"Пишу вам как руководителю {whom}" + (f" (сфера деятельности: {sphere})" if sphere else "") + "."
    if (offer or "").strip():
        intro += " " + offer.strip()

    blocks = [
        greeting,
        intro,
        "Если тема интересна, ответьте на это письмо, и я расскажу подробнее. "
        "Если письмо пришло не по адресу или не интересно, ответьте «Отказ»: больше писать не будем.",
        "С уважением,\n" + (signature or "").strip() if (signature or "").strip() else "С уважением.",
    ]
    return subject, "\n\n".join(blocks)
