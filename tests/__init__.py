"""В тестах адреса вымышлены на одном домене x.ru: для правила «одно письмо на домен» он считается общей бесплатной почтой."""
import compose

compose.PUBLIC_DOMAINS = compose.PUBLIC_DOMAINS | {"x.ru"}
