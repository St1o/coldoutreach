"""Диагностика: достаёт ли этот компьютер до API Checko (api.checko.ru) и принимает ли оно ключ.

Запуск: правой кнопкой на файле -> Run 'check_checko'. Программа ничего не отправляет и не меняет: проверяет по шагам
адрес, соединение, шифрование и запрос, потом простыми словами пишет, где обрыв. Ключ CHECKO_API_KEY берётся из
local.env и на экран не выводится. Если ключ задан, в конце делается один настоящий запрос (это 1 из 100 бесплатных
запросов в сутки), чтобы проверить сам ключ.
"""
import os
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import checko_api
from mailing import config

HOST, PORT = "api.checko.ru", 443
PROBE_KEY = "check-checko-probe"         # заведомо неверный ключ: для проверки соединения хватает любого ответа
SAMPLE_INN = "7707083893"                # известная организация для одного настоящего запроса с вашим ключом
MODES = ("default", "no-proxy", "tls12")
MODE_NAMES = {"default": "как в программе", "no-proxy": "без прокси", "tls12": "с TLS 1.2"}


@dataclass
class Report:
    proxies: dict = field(default_factory=dict)
    dns: tuple = (False, "")
    tcp: tuple = (False, "")
    tls: dict = field(default_factory=dict)          # "default" / "tls12" -> (ok, текст)
    api: dict = field(default_factory=dict)          # режим -> (ok, текст)
    key: tuple = None                                # (ok, текст) или None, если ключа нет


def hide_login(url: str) -> str:
    """Адрес прокси без логина и пароля."""
    parts = urllib.parse.urlsplit(url if "//" in url else "//" + url)
    host = parts.hostname or url
    return f"{host}:{parts.port}" if parts.port else host


def probe_proxies() -> dict:
    return {kind: hide_login(address) for kind, address in urllib.request.getproxies().items()
            if kind in ("http", "https", "socks", "socks5")}


def probe_dns(host: str = HOST) -> tuple:
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, PORT, proto=socket.IPPROTO_TCP)})
    except OSError as exc:
        return False, f"адрес не найден ({exc})"
    return True, ", ".join(addresses)


def probe_tcp(host: str = HOST) -> tuple:
    try:
        with socket.create_connection((host, PORT), timeout=10):
            return True, "порт 443 открыт"
    except OSError as exc:
        return False, f"порт 443 недоступен ({exc})"


def probe_tls(tls12: bool, host: str = HOST) -> tuple:
    context = ssl.create_default_context()           # проверка сертификата включена
    if tls12:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((host, PORT), timeout=15) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return True, f"шифрование установлено ({tls.version()})"
    except ssl.SSLCertVerificationError as exc:
        return False, f"сертификат не принят ({exc.verify_message})"
    except (ssl.SSLError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def probe_api(mode: str, key: str = PROBE_KEY) -> tuple:
    """Запрос так, как его делает программа. Годится любой ответ сервера: даже «неверный ключ» значит, что связь есть."""
    try:
        session = checko_api.new_session(mode)
        response = session.get(checko_api.API_URL, params={"key": key, "inn": SAMPLE_INN, "source": "false"}, timeout=20)
    except Exception as exc:                          # любой сбой: ключ в тексте скрываем
        text = str(exc).replace(key, "***") if key else str(exc)
        return False, f"{type(exc).__name__}: {text[:160]}"
    return True, f"ответ получен (HTTP {response.status_code})"


def probe_key(key: str) -> tuple:
    """Один настоящий запрос с вашим ключом: принят ли он."""
    api = checko_api.CheckoApi(key)
    try:
        api.company(SAMPLE_INN)
    except checko_api.CheckoApiQuotaExceeded as exc:
        return False, f"лимит или баланс: {exc}"
    except checko_api.CheckoApiNotFound:
        return True, "ключ принят"
    except checko_api.CheckoApiError as exc:
        return False, str(exc)
    return True, "ключ принят"


def collect(key: str = "", probes=None) -> Report:
    p = probes or {}
    report = Report(proxies=(p.get("proxies") or probe_proxies)())
    report.dns = (p.get("dns") or probe_dns)()
    if report.dns[0]:
        report.tcp = (p.get("tcp") or probe_tcp)()
    if report.tcp[0]:
        report.tls["default"] = (p.get("tls") or probe_tls)(False)
        report.tls["tls12"] = (p.get("tls") or probe_tls)(True)
    for mode in MODES:
        report.api[mode] = (p.get("api") or probe_api)(mode)
    if key and any(ok for ok, _ in report.api.values()):
        report.key = (p.get("key") or probe_key)(key)
    return report


def verdict(report: Report) -> list:
    """Выводы простыми словами и что делать."""
    api = {mode: report.api.get(mode, (False, ""))[0] for mode in MODES}
    lines = []
    if api["default"]:
        lines.append("Связь с Checko работает так же, как в программе.")
    elif api["no-proxy"]:
        lines.append("С этого компьютера Checko не отвечает напрямую, но отвечает БЕЗ прокси. Мешает прокси или VPN: "
                     "отключите его или исключите api.checko.ru. Программа теперь сама пробует путь без прокси.")
    elif api["tls12"]:
        lines.append("Checko отвечает только при шифровании TLS 1.2: новое рукопожатие что-то обрывает по дороге "
                     "(антивирус, прокси, провайдер). Программа теперь сама переключается на TLS 1.2.")
    elif not report.dns[0] and report.proxies:
        lines.append("Все три способа не прошли, а на компьютере настроен прокси (прямая проверка адреса при прокси не "
                     "показательна: адрес находит сам прокси). Проверьте, что прокси или VPN работает, или отключите "
                     "его и запустите проверку снова.")
    elif not report.dns[0]:
        lines.append("Адрес api.checko.ru не находится: нет интернета, мешает DNS или VPN. Проверьте, что сайт "
                     "checko.ru открывается в браузере, и отключите VPN.")
    elif not report.tcp[0]:
        lines.append("Порт 443 до api.checko.ru закрыт: брандмауэр, антивирус или провайдер не пускают программу. "
                     "Попробуйте отключить VPN и разрешить Python в брандмауэре.")
    else:
        lines.append("Соединение устанавливается, но обрывается при согласовании шифрования, во всех трёх вариантах. "
                     "Вероятные причины по частоте: 1) VPN или прокси с чужим адресом: многие российские сервисы "
                     "закрывают такие адреса, отключите VPN и повторите; 2) антивирус с проверкой защищённых "
                     "соединений (Kaspersky, ESET, Dr.Web): отключите проверку HTTPS или добавьте api.checko.ru в "
                     "исключения; 3) блокировка на стороне провайдера. Если ничего не помогло, напишите в поддержку "
                     "Checko, что запросы к api.checko.ru с вашего адреса обрываются при шифровании.")
    if report.proxies and not api["default"] and api["no-proxy"]:
        lines.append("На компьютере настроен прокси (см. выше): с ним и связаны сбои.")
    if report.key is not None:
        lines.append(("Ключ CHECKO_API_KEY: " + report.key[1]) if report.key[0]
                     else "Ключ CHECKO_API_KEY не принят: " + report.key[1])
    return lines


def main(argv=None, out=print) -> int:
    env_file = argv[1] if argv and len(argv) > 1 else config.ENV_FILE        # python check_checko.py [файл.env]
    key = ""
    try:
        key = config.read_env_file(env_file).get("CHECKO_API_KEY", "")
    except (OSError, ValueError):
        pass
    out(f"Проверяем соединение с {HOST} (ключ: {'задан, ' + str(len(key)) + ' знаков' if key else 'не задан в local.env'})...")
    report = collect(key)
    out("Прокси на этом компьютере: " + (", ".join(f"{kind} {addr}" for kind, addr in report.proxies.items())
                                         if report.proxies else "нет"))
    out(f"1. Адрес api.checko.ru: {'найден' if report.dns[0] else 'НЕ найден'} - {report.dns[1]}")
    if report.dns[0]:
        out(f"2. Соединение (порт 443): {'есть' if report.tcp[0] else 'НЕТ'} - {report.tcp[1]}")
    for name, label in (("default", "Обычное шифрование"), ("tls12", "Шифрование TLS 1.2")):
        if name in report.tls:
            ok, text = report.tls[name]
            out(f"3. {label}: {'ок' if ok else 'СБОЙ'} - {text}")
    for mode in MODES:
        ok, text = report.api[mode]
        out(f"4. Запрос к API ({MODE_NAMES[mode]}): {'ок' if ok else 'СБОЙ'} - {text}")
    out("")
    for line in verdict(report):
        out(line)
    return 0 if any(ok for ok, _ in report.api.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
