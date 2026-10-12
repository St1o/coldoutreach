"""В тестах адреса вымышлены на одном домене x.ru: для правила «одно письмо на домен» он считается общей бесплатной почтой."""
import os

import compose

compose.PUBLIC_DOMAINS = compose.PUBLIC_DOMAINS | {"x.ru"}

# Копия письма в «Отправленные» по IMAP в тестах не нужна и настоящего imap.mail.ru они трогать не должны: по умолчанию выключена.
# Тесты копии (test_sentcopy.py) включают её сами и подставляют поддельное IMAP-соединение.
os.environ.setdefault("SAVE_SENT_COPY", "0")
