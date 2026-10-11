"""Официальный API Checko: сведения об организации по ИНН (без капчи, по ключу).

Запрос:  GET https://api.checko.ru/v2/company?key=<КЛЮЧ>&inn=<ИНН>&source=true
Ответ:   {"data": {...}, "source_data": {...}, "meta": {"status": "ok"|"error", "message": ...}}

Из ответа берутся:
    * название  - data.НаимСокр / data.НаимПолн;
    * директор  - data.Руковод[].ФИО (первый с ФИО);
    * основной ОКВЭД - data.ОКВЭД (код и название); если там пусто, из исходных данных ЕГРЮЛ
                  (СвОКВЭДОсн: КодОКВЭД / НаимОКВЭД);
    * почта     - сначала из исходных данных ЕГРЮЛ (source_data, это та же почта, что в выписке),
                  иначе из data.Контакты.Емэйл (контакты «из открытых источников», могут быть
                  неполными или устаревшими - такие случаи помечаются).

Ключ в адрес запроса попадает, поэтому во всех сообщениях об ошибках он заменяется на ***.
Лимит API - 32 запроса в секунду с адреса, бесплатно 100 запросов в сутки (по документации Checko).
"""
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from egrul_pdf import find_email

API_URL = "https://api.checko.ru/v2/company"

_EMAIL_KEY_RE = re.compile(r"почт|e-?mail", re.IGNORECASE)
_NOT_FOUND_WORDS = ("не найден",)
_QUOTA_WORDS = ("лимит", "баланс", "средств", "превыш")


class CheckoApiError(Exception):
    """Ошибка обращения к API Checko."""


class CheckoApiNotFound(CheckoApiError):
    """Организации с таким ИНН нет в базе Checko."""


class CheckoApiQuotaExceeded(CheckoApiError):
    """Исчерпан лимит запросов или баланс: дальше стучаться бессмысленно."""


@dataclass
class CompanyInfo:
    name: str = ""
    director: str = ""
    email: str = ""
    email_from_register: bool = False      # True - почта из ЕГРЮЛ, False - из контактов Checko
    okved: str = ""                        # код основного вида деятельности, например «62.01»
    okved_name: str = ""


def _strings(node):
    """Все строки внутри вложенной структуры."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _register_email(node) -> str:
    """Почта из исходных данных ЕГРЮЛ: значения у ключей вида «...Почт...» / «E-mail»."""
    if isinstance(node, dict):
        for key, value in node.items():
            if _EMAIL_KEY_RE.search(str(key)):
                email = find_email(" ".join(_strings(value)))
                if email:
                    return email
            email = _register_email(value)
            if email:
                return email
    elif isinstance(node, list):
        for item in node:
            email = _register_email(item)
            if email:
                return email
    return ""


def _first(node: dict, *keys) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _find_key(node, key):
    """Первое значение по ключу во вложенной структуре (в разборе ЕГРЮЛ ключ лежит глубоко)."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _main_okved(payload: dict):
    """(код, название) основного ОКВЭД. Сначала поле Checko, затем исходные данные ЕГРЮЛ."""
    own = (payload.get("data") or {}).get("ОКВЭД")
    if isinstance(own, dict):
        code, name = _first(own, "Код"), _first(own, "Наим", "Наименование", "Назв")
        if code:
            return code, name
    elif isinstance(own, str) and own.strip():
        code, _, name = own.strip().partition(" ")
        return code, name.strip()

    main = _find_key(payload.get("source_data"), "СвОКВЭДОсн")
    if isinstance(main, dict):
        attrs = main.get("@attributes") if isinstance(main.get("@attributes"), dict) else main
        return _first(attrs, "КодОКВЭД"), _first(attrs, "НаимОКВЭД")
    return "", ""


def parse_company(payload: dict) -> CompanyInfo:
    data = payload.get("data") or {}
    info = CompanyInfo(name=data.get("НаимСокр") or data.get("НаимПолн") or "")

    for person in data.get("Руковод") or []:
        fio = (person.get("ФИО") or "").strip()
        if fio:
            info.director = fio.title() if fio.isupper() else fio
            break

    info.okved, info.okved_name = _main_okved(payload)

    info.email = _register_email(payload.get("source_data"))
    info.email_from_register = bool(info.email)
    if not info.email:
        contacts = (data.get("Контакты") or {}).get("Емэйл") or []
        info.email = find_email(" ".join(_strings(contacts)))
    return info


class _Tls12Adapter(HTTPAdapter):
    """Соединение не новее TLS 1.2: некоторые прокси и системы защиты обрывают более новое рукопожатие."""

    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = context
        super().init_poolmanager(*args, **kwargs)


def find_curl() -> Optional[str]:
    """Системная программа curl: на Windows 10 и новее лежит в System32 и шифрует средствами Windows, а не Python."""
    if os.name == "nt":
        own = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe")
        if os.path.exists(own):
            return own
    return shutil.which("curl")


class _PlainResponse:
    def __init__(self, status: int, body: str):
        self.status_code, self._body = status, body

    def json(self):
        return json.loads(self._body)                   # не JSON - ValueError, как у requests


class _CurlSession:
    """Запрос через curl вместо Python. Ключ передаётся через стандартный ввод, а не в командной строке,
    чтобы его не было в списке процессов."""

    def __init__(self, path: str):
        self.path = path

    def get(self, url, params=None, timeout=20.0):
        query = urllib.parse.urlencode(params or {})
        config = f'url = "{url}{"?" + query if query else ""}"\n'
        command = [self.path, "--silent", "--show-error", "--max-time", str(max(1, int(timeout))),
                   "--config", "-", "--write-out", "\n%{http_code}"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            done = subprocess.run(command, input=config.encode("utf-8"), capture_output=True,
                                  timeout=timeout + 5, creationflags=flags)
        except (OSError, subprocess.SubprocessError) as exc:
            raise requests.exceptions.ConnectionError(f"curl не запустился: {exc}") from None
        if done.returncode != 0:
            reason = done.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")
            raise requests.exceptions.ConnectionError(f"curl: {reason[:150]}")
        body, _, status = done.stdout.decode("utf-8", errors="replace").rpartition("\n")
        try:
            return _PlainResponse(int(status), body)
        except ValueError:
            raise requests.exceptions.ConnectionError("curl вернул ответ не в том виде") from None


IP_UNICAST_IF = 31          # параметр сокета Windows: слать пакеты через указанную сетевую карту, а не по таблице маршрутов

# Все карты со статусом Up и маршрутом по умолчанию: имя, описание, метрика, шлюз, настоящая ли (не виртуальная) карта.
_ADAPTER_SCRIPT = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"      # русские имена карт: иначе приходит кодовая страница консоли
    "$ErrorActionPreference='Stop';"
    "$up=@(Get-NetAdapter | Where-Object { $_.Status -eq 'Up' });"
    "$routes=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0');"
    "$out=@(foreach ($r in $routes) {"
    " $a=$up | Where-Object { $_.ifIndex -eq $r.ifIndex } | Select-Object -First 1;"
    " if ($a) { [pscustomobject]@{ ifIndex=[int]$r.ifIndex; NextHop=[string]$r.NextHop;"
    " Metric=[int]($r.RouteMetric + $r.InterfaceMetric); Name=[string]$a.Name;"
    " Description=[string]$a.InterfaceDescription; Hardware=[bool]$a.HardwareInterface } } });"
    "ConvertTo-Json -InputObject $out -Compress")
# Туннели VPN, виртуальные машины и подобное: через них мимо VPN не выйти
_VIRTUAL_LIKE = re.compile(
    r"\btap\b|\btun\b|wintun|wireguard|openvpn|vpn|tailscale|zerotier|hyper-v|virtualbox|vmware|loopback|bluetooth|"
    r"vethernet|npcap|anyconnect|fortinet|nord|proton|surfshark|windscribe|amnezia|hiddify|sing-box|clash|xray|v2ray|"
    r"outline|warp|mullvad|expressvpn|cyberghost|hotspot shield|softether|pritunl|tunnel", re.IGNORECASE)
_adapters_cache = []


def _is_windows() -> bool:
    return os.name == "nt"


def adapter_candidates() -> tuple:
    """([карты со статусом Up и выходом в интернет], причина сбоя или ''). Только Windows, права администратора не нужны."""
    if not _is_windows():
        return [], "это не Windows"
    if _adapters_cache:
        return _adapters_cache[0]
    candidates, error = [], ""
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _ADAPTER_SCRIPT],
                              capture_output=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = done.stdout.decode("utf-8", errors="replace").strip()
        if done.returncode != 0:
            error = "PowerShell вернул ошибку: " + (done.stderr.decode("utf-8", errors="replace").strip() or text)[:150]
        elif text:
            data = json.loads(text)
            for item in (data if isinstance(data, list) else [data]):
                candidates.append({"ifIndex": int(item["ifIndex"]), "NextHop": str(item.get("NextHop", "")),
                                   "Metric": int(item.get("Metric", 0)), "Name": str(item.get("Name", "")),
                                   "Description": str(item.get("Description", "")),
                                   "Hardware": bool(item.get("Hardware"))})
    except subprocess.TimeoutExpired:
        error = "PowerShell не ответил за 40 секунд"
    except OSError as exc:
        error = f"PowerShell не запустился ({exc.strerror or exc})"
    except (ValueError, KeyError, TypeError):
        error = "не удалось разобрать ответ PowerShell"
    _adapters_cache.append((candidates, error))
    return _adapters_cache[0]


def pick_adapter(candidates: list) -> Optional[dict]:
    """Какая карта годится для обхода VPN: сначала настоящая и не похожая на туннель, потом просто не похожая на туннель
    (флаг «настоящая» у некоторых карт, например USB-модема, бывает неверным); из подходящих - с меньшей метрикой."""
    plain = [c for c in candidates if not _VIRTUAL_LIKE.search(c["Name"] + " " + c["Description"])]
    for tier in ([c for c in plain if c["Hardware"]], plain):
        if tier:
            return min(tier, key=lambda c: c["Metric"])
    return None


def physical_interface_index() -> Optional[int]:
    """Номер сетевой карты для обхода VPN (Ethernet или Wi-Fi, а не туннель) или None."""
    chosen = pick_adapter(adapter_candidates()[0])
    return chosen["ifIndex"] if chosen else None


def open_bound_socket(host: str, port: int, if_index: int, timeout: float, bind: bool = True) -> socket.socket:
    """TCP-соединение через указанную сетевую карту (только IPv4): на Windows IP_UNICAST_IF уводит его мимо туннеля VPN,
    у которого маршрут по умолчанию выше. Номер карты для IPv4 задаётся в сетевом порядке байтов."""
    errors = []
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            if bind:
                sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", if_index))
            sock.settimeout(timeout)
            sock.connect(address)
            return sock
        except OSError as exc:
            sock.close()
            errors.append(exc)
    raise errors[-1] if errors else OSError("адрес не найден")


_CERT_REASONS = (
    (("unable to get local issuer certificate", "unable to verify the first certificate"),
     "сервер прислал неполную цепочку сертификатов или центр сертификации неизвестен этому компьютеру"),
    (("self signed certificate", "self-signed certificate"),
     "сертификат самоподписанный: так выглядит подмена соединения (антивирус, провайдер, роутер)"),
    (("certificate has expired",), "срок сертификата истёк (или неверные дата и время на компьютере)"),
    (("hostname mismatch", "doesn't match"), "сертификат выдан на другое имя сайта: так выглядит подмена соединения"),
)


def describe_cert_error(exc: Exception) -> str:
    """Почему сертификат не принят, простыми словами и с исходным текстом."""
    message = getattr(exc, "verify_message", "") or str(exc)
    for needles, text in _CERT_REASONS:
        if any(needle in message.lower() for needle in needles):
            return f"{text} [{message}]"
    return message


def os_trust_context() -> Optional[ssl.SSLContext]:
    """Контекст, который проверяет сертификат средствами операционной системы (как браузер: Windows сама докачивает
    недостающие промежуточные сертификаты). Нужен пакет truststore; нет его - None."""
    try:
        import truststore
    except ImportError:
        return None
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def own_ca_context() -> ssl.SSLContext:
    """Обычная проверка сертификата; если в local.env задан CHECKO_CA_FILE (файл PEM с корневым сертификатом), его
    доверенный центр добавляется к обычным."""
    context = ssl.create_default_context()
    path = os.environ.get("CHECKO_CA_FILE", "").strip()
    if path and os.path.exists(path):
        context.load_verify_locations(cafile=path)
    return context


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS-соединение через указанную сетевую карту (см. open_bound_socket)."""

    def __init__(self, host, port, if_index, timeout, context, bind=True):
        super().__init__(host, port, timeout=timeout, context=context)
        self.if_index, self._bind = if_index, bind

    def connect(self):
        sock = open_bound_socket(self.host, self.port, self.if_index, self.timeout, self._bind)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except OSError:
            sock.close()
            raise


class _BoundSession:
    """Запрос в обход VPN: напрямую через настоящую сетевую карту (см. _BoundHTTPSConnection)."""

    def __init__(self, if_index: int, context: Optional[ssl.SSLContext] = None, bind: bool = True):
        self.if_index, self.bind = if_index, bind
        self.context = context if context is not None else own_ca_context()      # проверка сертификата включена

    def get(self, url, params=None, timeout=20.0):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.urlencode(params or {})
        conn = _BoundHTTPSConnection(parts.hostname, parts.port or 443, self.if_index, timeout, self.context, self.bind)
        try:
            conn.request("GET", parts.path + ("?" + query if query else ""),
                         headers={"Accept": "application/json", "User-Agent": "coldoutreach", "Connection": "close"})
            response = conn.getresponse()
            return _PlainResponse(response.status, response.read().decode("utf-8", errors="replace"))
        except ssl.SSLCertVerificationError as exc:
            raise requests.exceptions.ConnectionError(
                f"в обход VPN: сертификат не принят: {describe_cert_error(exc)}"[:260]) from None
        except (OSError, http.client.HTTPException) as exc:             # включая обрыв шифрования
            raise requests.exceptions.ConnectionError(f"в обход VPN: {type(exc).__name__}: {exc}"[:150]) from None
        finally:
            conn.close()


# Если обычное соединение обрывается, пробуем без системных настроек прокси, потом в обход VPN (напрямую через сетевую
# карту; второй раз - с проверкой сертификата средствами Windows), потом с TLS 1.2, потом через системный curl.
# Что сработало, то остаётся до конца работы программы.
FALLBACK_MODES = ("no-proxy", "bypass", "bypass-os", "tls12", "curl")


def new_session(mode: str = "default"):
    """Сеанс для запросов. Режимы curl, bypass и bypass-os возвращают None, если нет curl, подходящей сетевой карты
    или (для bypass-os) пакета truststore."""
    if mode in ("bypass", "bypass-os"):
        index = physical_interface_index()
        context = os_trust_context() if mode == "bypass-os" else own_ca_context()
        return _BoundSession(index, context) if index and context is not None else None
    if mode == "curl":
        path = find_curl()
        return _CurlSession(path) if path else None
    session = requests.Session()
    if mode == "no-proxy":
        session.trust_env = False                   # без HTTP(S)_PROXY и настроек прокси Windows
    elif mode == "tls12":
        session.mount("https://", _Tls12Adapter())
    return session


class CheckoApi:
    def __init__(self, key: str, session: Optional[requests.Session] = None, timeout: float = 20.0):
        self.key = key
        self._own_session = session is None
        self.session = session or new_session()
        self.timeout = timeout
        self.mode = "default"

    def _scrub(self, text) -> str:
        return str(text).replace(self.key, "***") if self.key else str(text)

    def _explain(self, exc) -> str:
        """Причина сбоя простыми словами и короткий технический хвост (ключ скрыт)."""
        if isinstance(exc, requests.exceptions.SSLError):
            head = "защищённое соединение с api.checko.ru оборвано (SSL)"
        elif isinstance(exc, requests.exceptions.ProxyError):
            head = "прокси не пропустил запрос к api.checko.ru"
        elif isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
            head = "api.checko.ru не ответил вовремя"
        elif isinstance(exc, requests.exceptions.ConnectionError):
            head = "нет соединения с api.checko.ru"
        else:
            head = "сбой запроса к api.checko.ru"
        text = self._scrub(exc)
        cause = re.search(r"Caused by (.*)$", text, re.S)
        tail = (cause.group(1) if cause else text).strip().replace("\n", " ")
        return f"{head}: {tail}"[:200]

    def _get(self, inn: str):
        """Запрос; при обрыве защищённого соединения пробует запасные пути (см. FALLBACK_MODES)."""
        params = {"key": self.key, "inn": inn, "source": "true"}
        try:
            return self.session.get(API_URL, params=params, timeout=self.timeout)
        except requests.exceptions.ConnectionError as first:      # включая обрыв шифрования (SSLError) и прокси
            if isinstance(first, requests.exceptions.Timeout) or not self._own_session or self.mode != "default":
                raise                                              # долгое молчание - не повод менять способ соединения
            for mode in FALLBACK_MODES:
                session = new_session(mode)
                if session is None:
                    continue
                try:
                    response = session.get(API_URL, params=params, timeout=self.timeout)
                except requests.RequestException:
                    continue
                self.session, self.mode = session, mode      # дальше идём этим путём
                return response
            raise first

    def company(self, inn: str) -> dict:
        """Ответ API по ИНН (JSON). Бросает CheckoApiNotFound / CheckoApiQuotaExceeded / CheckoApiError."""
        try:
            response = self._get(inn)
        except requests.RequestException as exc:
            raise CheckoApiError(self._explain(exc)) from None

        status = response.status_code
        if status in (402, 429):
            raise CheckoApiQuotaExceeded(f"HTTP {status}: лимит запросов или баланс")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        message = ""
        if isinstance(payload, dict):
            message = self._scrub((payload.get("meta") or {}).get("message") or "")
        if status == 404:
            raise CheckoApiNotFound(message or "HTTP 404")
        if status >= 400:
            raise CheckoApiError(f"HTTP {status}" + (f": {message}" if message else ""))
        if not isinstance(payload, dict):
            raise CheckoApiError("Ответ API не в формате JSON")

        meta_status = (payload.get("meta") or {}).get("status")
        if meta_status == "error":
            lowered = message.lower()
            if any(word in lowered for word in _QUOTA_WORDS):
                raise CheckoApiQuotaExceeded(message)
            if any(word in lowered for word in _NOT_FOUND_WORDS):
                raise CheckoApiNotFound(message)
            raise CheckoApiError(message or "API вернул ошибку")
        if not payload.get("data"):
            raise CheckoApiNotFound(message or "в ответе нет данных")
        return payload
