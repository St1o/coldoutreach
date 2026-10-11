"""Диагностика: достаёт ли этот компьютер до API Checko (api.checko.ru) и принимает ли оно ключ.

Запуск: правой кнопкой на файле -> Run 'check_checko'. Программа ничего не отправляет и не меняет: проверяет по шагам
адрес, соединение, шифрование, запрос из Python и через системный curl, контрольные сайты, потом простыми словами
пишет, где обрыв. Ключ CHECKO_API_KEY берётся из local.env и на экран не выводится. Если ключ задан, в конце делается
один настоящий запрос (это 1 из 100 бесплатных запросов в сутки), чтобы проверить сам ключ.
"""
import errno
import os
import re
import socket
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import requests

import checko_api
from mailing import config

HOST, PORT = "api.checko.ru", 443
PROBE_KEY = "check-checko-probe"         # заведомо неверный ключ: для проверки соединения хватает любого ответа
SAMPLE_INN = "7707083893"                # известная организация для одного настоящего запроса с вашим ключом
MODES = ("default", "no-proxy", "bypass", "bypass-os", "tls12", "curl")
MODE_NAMES = {"default": "как в программе", "no-proxy": "без прокси", "bypass": "в обход VPN, через сетевую карту",
              "bypass-os": "в обход VPN, сертификат проверяет Windows", "tls12": "с TLS 1.2",
              "curl": "через curl, шифрование Windows"}
NO_WAY = {"bypass": "настоящая сетевая карта не найдена (или это не Windows)",
          "bypass-os": "нужны сетевая карта и пакет truststore (pip install -r requirements.txt)",
          "curl": "программы curl на этом компьютере нет"}
RUSSIAN_CA = re.compile(r"russian trusted|минцифр|российск", re.IGNORECASE)
ANTIVIRUS_CA = re.compile(r"kaspersky|eset|avast|\bavg\b|dr\.?web|bitdefender|fortinet|zscaler|mcafee|norton|sophos|"
                          r"trend micro|panda|comodo|cisco umbrella|securly", re.IGNORECASE)
RUSSIAN_ROOT_URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
CONTROL_SITES = (("checko.ru", "https://checko.ru/"), ("ya.ru", "https://ya.ru/"))
DOH_URL = "https://dns.google/resolve"   # публичный DNS поверх HTTPS: чтобы сверить адрес, который отдаёт ваш DNS


@dataclass
class Report:
    proxies: dict = field(default_factory=dict)
    dns: tuple = (False, "")
    dns_ips: tuple = ()                              # адреса из системного DNS
    public_dns: tuple = (None, "")                   # (ok или None, адреса или причина)
    tcp: tuple = (False, "")
    tls: dict = field(default_factory=dict)          # "default" / "tls12" -> (ok, текст)
    control: dict = field(default_factory=dict)      # сайт -> (ok, текст): обычные сайты из Python
    api: dict = field(default_factory=dict)          # режим -> (ok, текст); ok None - способа нет на этом компьютере
    adapters: dict = field(default_factory=lambda: {"candidates": [], "error": "", "chosen": None})
    cert: dict = None                                # сертификат, присланный через сетевую карту (см. probe_certificate)
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


def probe_public_dns(host: str = HOST) -> tuple:
    """Какой адрес отдаёт публичный DNS. (None, причина) - спросить не удалось: это тоже сведения (HTTPS из Python)."""
    try:
        answer = requests.get(DOH_URL, params={"name": host, "type": "A"}, timeout=15).json()
        found = sorted({item["data"] for item in answer.get("Answer", []) if item.get("type") == 1})
    except Exception as exc:
        return None, f"спросить не удалось ({type(exc).__name__})"
    return (True, ", ".join(found)) if found else (None, "публичный DNS адрес не вернул")


def probe_tcp(host: str = HOST) -> tuple:
    try:
        with socket.create_connection((host, PORT), timeout=10):
            return True, "порт 443 открыт"
    except OSError as exc:
        return False, f"порт 443 недоступен ({exc})"


def describe_tls_error(exc: Exception) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"сертификат не принят ({exc.verify_message})"
    if isinstance(exc, FileNotFoundError) or getattr(exc, "errno", None) == errno.ENOENT:
        # файл не пропал: так Python иногда сообщает, что сервер закрыл соединение посреди рукопожатия
        return "соединение оборвано при установке шифрования (Python пишет «No such file or directory», файл тут ни при чём)"
    return f"{type(exc).__name__}: {exc}"


def probe_tls(tls12: bool, host: str = HOST) -> tuple:
    context = ssl.create_default_context()           # проверка сертификата включена
    if tls12:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((host, PORT), timeout=15) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return True, f"шифрование установлено ({tls.version()})"
    except (ssl.SSLError, OSError) as exc:
        return False, describe_tls_error(exc)


def probe_control(url: str) -> tuple:
    """Обычный сайт из Python: если и он не открывается, сломан не Checko, а HTTPS у Python на этом компьютере."""
    try:
        response = requests.get(url, timeout=15)
    except Exception as exc:
        return False, f"{type(exc).__name__}"
    return True, f"открылся (HTTP {response.status_code})"


def probe_api(mode: str, key: str = PROBE_KEY) -> tuple:
    """Запрос так, как его делает программа. Годится любой ответ сервера: даже «неверный ключ» значит, что связь есть."""
    session = checko_api.new_session(mode)
    if session is None:
        if mode == "bypass-os" and checko_api.os_trust_context() is None:
            return None, ("пакет truststore не установлен: выполните pip install -r requirements.txt "
                          "(установить нужно в то окружение, которым запускается проект)")
        return None, NO_WAY.get(mode, "способа нет")
    try:
        response = session.get(checko_api.API_URL, params={"key": key, "inn": SAMPLE_INN, "source": "false"}, timeout=20)
    except Exception as exc:                          # любой сбой: ключ в тексте скрываем
        text = str(exc).replace(key, "***") if key else str(exc)
        return False, f"{type(exc).__name__}: {text[:160]}"
    return True, f"ответ получен (HTTP {response.status_code})"


def probe_adapter() -> dict:
    """Сетевые карты с выходом в интернет и та, через которую запросы пойдут в обход VPN."""
    candidates, error = checko_api.adapter_candidates()
    return {"candidates": candidates, "error": error, "chosen": checko_api.pick_adapter(candidates)}


def adapter_lines(adapters: dict) -> list:
    """Список карт для вывода: имя, метрика, шлюз, настоящая или виртуальная, выбранная для обхода VPN."""
    lines = ["6. Сетевые карты с выходом в интернет:"]
    if adapters["error"]:
        lines.append("   список получить не удалось: " + adapters["error"])
    elif not adapters["candidates"]:
        lines.append("   нет ни одной подключённой карты с маршрутом в интернет")
    for card in adapters["candidates"]:
        kind = "настоящая" if card["Hardware"] else "виртуальная"
        mark_chosen = "  <- через неё идёт обход VPN" if card is adapters["chosen"] else ""
        lines.append(f"   №{card['ifIndex']} {card['Name']} ({card['Description']}): метрика {card['Metric']}, "
                     f"шлюз {card['NextHop']}, {kind}{mark_chosen}")
    if adapters["candidates"] and not adapters["chosen"]:
        lines.append("   Подходящей карты для обхода VPN нет: все выглядят как туннели VPN или виртуальные.")
    return lines


def exclusion_hint() -> str:
    """Что добавить в исключения VPN, если у него есть раздельное туннелирование по программам. Venv-овский python.exe на
    Windows запускает базовый, поэтому нужны оба пути."""
    paths = []
    for path in (sys.executable, getattr(sys, "_base_executable", "")):
        if path and path not in paths:
            paths.append(path)
    return ("Если в вашем VPN есть исключения по программам (раздельное туннелирование), добавьте туда: "
            + "; ".join(paths) + ". Или исключите адреса api.checko.ru и checko.ru.")


def describe_name(name) -> str:
    return ", ".join(f"{key}={value}" for part in name for key, value in part)


def probe_certificate(card) -> dict:
    """Сертификат, который присылает api.checko.ru при соединении через сетевую карту (в обход VPN): проверка Python и то,
    что в сертификате написано (для чтения проверка выключена, ключ и данные при этом не передаются)."""
    result = {"verified": None, "verify_error": "", "error": "", "subject": "", "issuer": "", "valid": ""}
    if not card:
        result["error"] = "нет подходящей сетевой карты"
        return result
    index = card["ifIndex"]
    try:
        sock = checko_api.open_bound_socket(HOST, PORT, index, 15)
        with ssl.create_default_context().wrap_socket(sock, server_hostname=HOST):
            result["verified"] = True
    except ssl.SSLCertVerificationError as exc:
        result["verified"], result["verify_error"] = False, checko_api.describe_cert_error(exc)
    except OSError as exc:
        result["error"] = describe_tls_error(exc)
        return result
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE        # только чтение сертификата
    try:
        sock = checko_api.open_bound_socket(HOST, PORT, index, 15)
        with context.wrap_socket(sock, server_hostname=HOST) as tls:
            der = tls.getpeercert(binary_form=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "server.pem")
            with open(path, "w") as fh:
                fh.write(ssl.DER_cert_to_PEM_cert(der))
            info = ssl._ssl._test_decode_cert(path)       # служебная функция: в новых Python может исчезнуть
        result["subject"] = describe_name(info.get("subject", ()))
        result["issuer"] = describe_name(info.get("issuer", ()))
        result["valid"] = f"{info.get('notBefore')} - {info.get('notAfter')}"
    except Exception as exc:
        result["error"] = result["error"] or f"сертификат прочитать не удалось ({type(exc).__name__})"
    return result


def cert_lines(cert) -> list:
    if not cert:
        return []
    lines = ["7. Сертификат, который присылает Checko через сетевую карту (в обход VPN):"]
    if cert["error"] and not cert["issuer"]:
        return lines + ["   не получен: " + cert["error"]]
    if cert["issuer"]:
        lines += ["   кому выдан : " + cert["subject"], "   кем выдан  : " + cert["issuer"],
                  "   действует  : " + cert["valid"]]
    lines.append("   проверка Python: " + ("принят" if cert["verified"] else "ОТКАЗ - " + cert["verify_error"]))
    return lines


def cert_advice(cert) -> list:
    """Что делать, если сертификат не принят: по тому, кем он выдан."""
    if not cert or cert["verified"] is not False:
        return []
    issuer = cert.get("issuer", "")
    if RUSSIAN_CA.search(issuer):
        return ["Сертификат Checko выдан российским удостоверяющим центром (Минцифры): Windows и Python по умолчанию ему не "
                f"доверяют. Скачайте корневой сертификат {RUSSIAN_ROOT_URL}, положите файл в любую папку и впишите в "
                "local.env строку CHECKO_CA_FILE=полный\\путь\\к\\russian_trusted_root_ca_pem.crt (для запроса в обход "
                "VPN этого достаточно; если сервер не присылает промежуточный сертификат, понадобится и "
                "russian_trusted_sub_ca_pem.crt, его можно дописать в тот же файл)."]
    if ANTIVIRUS_CA.search(issuer):
        return ["Сертификат подменяет антивирус или программа защиты (" + issuer + "): отключите в ней проверку "
                "защищённых соединений (HTTPS) или добавьте api.checko.ru в исключения."]
    subject, text = cert.get("subject", ""), cert.get("verify_error", "")
    if issuer and issuer == subject:
        return ["Сертификат самоподписанный: так выглядит подмена соединения роутером, провайдером или защитной "
                "программой. Попробуйте другую сеть (раздача с телефона)."]
    if "неполную цепочку" in text:
        return ["Сервер присылает неполную цепочку сертификатов: Windows и браузеры докачивают недостающее сами, Python "
                "нет. Установите пакет truststore (pip install -r requirements.txt) и запустите проверку снова: "
                "программа тогда проверит сертификат средствами Windows."]
    return []


def probe_key(key: str) -> tuple:
    """Один настоящий запрос с вашим ключом (программа сама выберет рабочий путь): принят ли он."""
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
    report.adapters = (p.get("adapter") or probe_adapter)()
    report.dns = (p.get("dns") or probe_dns)()
    if report.dns[0]:
        report.dns_ips = tuple(ip.strip() for ip in report.dns[1].split(","))
        report.tcp = (p.get("tcp") or probe_tcp)()
    report.public_dns = (p.get("public_dns") or probe_public_dns)()
    if report.tcp[0]:
        report.tls["default"] = (p.get("tls") or probe_tls)(False)
        report.tls["tls12"] = (p.get("tls") or probe_tls)(True)
    for name, url in CONTROL_SITES:
        report.control[name] = (p.get("control") or probe_control)(url)
    for mode in MODES:
        report.api[mode] = (p.get("api") or probe_api)(mode)
    if report.adapters["chosen"] and report.api.get("bypass", (None, ""))[0] is not True:
        report.cert = (p.get("cert") or probe_certificate)(report.adapters["chosen"])
    if key and any(ok is True for ok, _ in report.api.values()):
        report.key = (p.get("key") or probe_key)(key)
    return report


def dns_mismatch(report: Report) -> bool:
    """Системный DNS отдаёт не те адреса, что публичный: вероятна подмена адреса у провайдера или в настройках."""
    public = report.public_dns
    if not (report.dns_ips and public[0] and public[1]):
        return False
    return not set(report.dns_ips) & {ip.strip() for ip in public[1].split(",")}


def verdict(report: Report) -> list:
    """Выводы простыми словами и что делать."""
    api = {mode: report.api.get(mode, (False, ""))[0] is True for mode in MODES}
    control_ok = any(ok for ok, _ in report.control.values())
    lines = []
    if api["default"]:
        lines.append("Связь с Checko работает так же, как в программе.")
    elif api["no-proxy"]:
        lines.append("С этого компьютера Checko не отвечает напрямую, но отвечает БЕЗ прокси. Мешает прокси или VPN: "
                     "отключите его или исключите api.checko.ru. Программа сама пробует путь без прокси.")
    elif api["bypass"]:
        lines.append("Checko закрыт для адреса вашего VPN, но открыт напрямую, через настоящую сетевую карту. Программа "
                     "сама отправляет запросы к Checko в обход VPN: выключать VPN не нужно. Остальной интернет у вас "
                     "по-прежнему идёт через VPN.")
    elif api["bypass-os"]:
        lines.append("Checko закрыт для адреса вашего VPN, но открыт напрямую, и Windows принимает его сертификат, а Python "
                     "сам не может его проверить (так бывает, когда сервер присылает неполную цепочку сертификатов). "
                     "Программа сама отправляет запросы к Checko в обход VPN и проверяет сертификат средствами Windows: "
                     "выключать VPN не нужно, проверка сертификата при этом не отключается.")
    elif api["tls12"]:
        lines.append("Checko отвечает только при шифровании TLS 1.2: новое рукопожатие что-то обрывает по дороге "
                     "(антивирус, прокси, провайдер). Программа сама переключается на TLS 1.2.")
    elif api["curl"]:
        lines.append("Python не может открыть защищённое соединение с Checko, а системная программа curl (шифрование "
                     "самой Windows) может. Программа сама переключается на curl для запросов к Checko, делать ничего "
                     "не нужно. Причина обычно в защите, которая режет соединения Python: антивирус с проверкой HTTPS "
                     "или программа обхода блокировок (zapret, GoodbyeDPI и подобные).")
    elif not report.dns[0] and report.proxies:
        lines.append("Все способы не прошли, а на компьютере настроен прокси (прямая проверка адреса при прокси не "
                     "показательна: адрес находит сам прокси). Проверьте, что прокси или VPN работает, или отключите "
                     "его и запустите проверку снова.")
    elif not report.dns[0]:
        lines.append("Адрес api.checko.ru не находится: нет интернета, мешает DNS или VPN. Проверьте, что сайт "
                     "checko.ru открывается в браузере, и отключите VPN.")
    elif not report.tcp[0]:
        lines.append("Порт 443 до api.checko.ru закрыт: брандмауэр, антивирус или провайдер не пускают программу. "
                     "Попробуйте отключить VPN и разрешить Python в брандмауэре.")
    elif report.control and not control_ok:
        lines.append("Python не открывает защищённые соединения вообще ни с какими сайтами, не только с Checko. Мешает "
                     "антивирус с проверкой защищённых соединений (Kaspersky, ESET, Dr.Web), программа обхода блокировок "
                     "(zapret, GoodbyeDPI) или VPN: отключите их на время проверки и запустите её снова.")
    else:
        lines.append("Обычные сайты из Python открываются, а api.checko.ru нет: ни из Python, ни через curl, ни в обход "
                     "VPN соединение не устанавливается. Значит, адрес Checko API закрыт для вашей сети. Чаще всего так "
                     "закрывают не российские адреса (VPN, зарубежный интернет) или это блокировка у провайдера. "
                     "Проверьте с другого интернета (например, раздача с телефона) и без VPN: если заработает, причина "
                     "в сети. Если у VPN включена блокировка трафика вне VPN («kill switch»), она не даёт обойти его: "
                     "выключите её или добавьте программу в исключения VPN (см. ниже). Если и это не поможет, напишите "
                     "в поддержку Checko, что запросы к api.checko.ru с вашего адреса обрываются при шифровании.")
    lines.extend(cert_advice(report.cert))
    if dns_mismatch(report):
        lines.append(f"Ваш DNS отдаёт для api.checko.ru адрес {', '.join(report.dns_ips)}, а публичный DNS - "
                     f"{report.public_dns[1]}: адрес, вероятно, подменён у провайдера или в настройках. Поставьте в "
                     "настройках сети DNS 1.1.1.1 или 8.8.8.8 и запустите проверку снова.")
    if report.proxies and not api["default"] and api["no-proxy"]:
        lines.append("На компьютере настроен прокси (см. выше): с ним и связаны сбои.")
    if report.key is not None:
        lines.append(("Ключ CHECKO_API_KEY: " + report.key[1]) if report.key[0]
                     else "Ключ CHECKO_API_KEY не принят: " + report.key[1])
    return lines


def mark(ok) -> str:
    return "ок" if ok else ("нет такого способа" if ok is None else "СБОЙ")


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
    out(f"   Публичный DNS (Google) отдаёт: {report.public_dns[1]}")
    if report.dns[0]:
        out(f"2. Соединение (порт 443): {'есть' if report.tcp[0] else 'НЕТ'} - {report.tcp[1]}")
    for name, label in (("default", "Обычное шифрование"), ("tls12", "Шифрование TLS 1.2")):
        if name in report.tls:
            ok, text = report.tls[name]
            out(f"3. {label}: {mark(ok)} - {text}")
    for site, (ok, text) in report.control.items():
        out(f"4. Другой сайт из Python, {site}: {mark(ok)} - {text}")
    for mode in MODES:
        ok, text = report.api[mode]
        out(f"5. Запрос к API ({MODE_NAMES[mode]}): {mark(ok)} - {text}")
    for line in adapter_lines(report.adapters):
        out(line)
    for line in cert_lines(report.cert):
        out(line)
    out("")
    for line in verdict(report):
        out(line)
    if not report.api["default"][0]:
        out(exclusion_hint())
    return 0 if any(ok is True for ok, _ in report.api.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
