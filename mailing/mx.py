"""Проверка почтового домена по MX: стоит ли почта на серверах Яндекса (mx.yandex.ru, mx.yandex.net, …).

Только стандартная библиотека: в ней нет запроса MX, поэтому здесь маленький клиент DNS (UDP, при обрезанном ответе TCP).
Серверы DNS: MX_DNS_SERVERS из local.env (через запятую или пробел), иначе системные (resolv.conf; в Windows GetNetworkParams),
затем запасные публичные. Результат по домену живёт в mx_cache.json в папке данных сутки (в файле только домены и имена
почтовых серверов, без адресов). Сбой DNS не роняет сборку: домен остаётся непроверенным, и решает вызывающий код."""
import json
import os
import re
import secrets
import socket
import struct
from datetime import datetime, timedelta, timezone

from . import atomic

CACHE_NAME = "mx_cache.json"
CACHE_TTL = timedelta(hours=24)
TIMEOUT = 2.5                              # секунд на один сервер DNS
MAX_SERVERS = 4                            # сколько серверов пробуем на один домен
MAX_FAILURES = 3                           # столько доменов подряд без ответа DNS: дальше в этом запуске не спрашиваем
FALLBACK_SERVERS = ("1.1.1.1", "8.8.8.8")  # если системные не нашлись или молчат
MX_TYPE, IN_CLASS = 15, 1

YANDEX, CLEAN, UNCHECKED = "yandex", "clean", "unchecked"      # итог по домену
_YANDEX_HOST = re.compile(r"(?:^|\.)yandex\.(?:[a-z]{2,}|com\.[a-z]{2})$")


class MxError(Exception):
    """DNS не дал понятного ответа (таймаут, отказ, обрыв, неверный пакет)."""


def is_yandex_host(host: str) -> bool:
    """mx.yandex.ru, mx.yandex.net, yandex.ru, mx.yandex.com.tr -> True; notyandex.ru и yandex-mx.example.com -> False."""
    return bool(_YANDEX_HOST.search((host or "").strip().lower().rstrip(".")))


# --- DNS по проводу --------------------------------------------------------------------------------------

def build_query(domain: str, ident: int) -> bytes:
    try:
        name = domain.strip().rstrip(".").lower().encode("idna")
    except UnicodeError as exc:
        raise MxError("неверное имя домена") from exc
    labels = name.split(b".")
    if not name or any(not label or len(label) > 63 for label in labels):
        raise MxError("неверное имя домена")
    question = b"".join(bytes([len(label)]) + label for label in labels) + b"\x00" + struct.pack("!HH", MX_TYPE, IN_CLASS)
    return struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + question     # флаг RD: нужен рекурсивный ответ


def _read_name(data: bytes, pos: int):
    """Имя с учётом сжатия (указатели на прежние места пакета) -> (имя, позиция после имени в этом месте)."""
    labels, end, jumped, hops = [], pos, False, 0
    while True:
        if pos >= len(data):
            raise MxError("обрыв имени в ответе DNS")
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise MxError("обрыв указателя в ответе DNS")
            if not jumped:
                end = pos + 2
            jumped, hops = True, hops + 1
            pos = ((length & 0x3F) << 8) | data[pos + 1]
            if hops > 20 or pos >= len(data):
                raise MxError("неверный указатель в ответе DNS")
            continue
        if length & 0xC0:
            raise MxError("неверная метка в ответе DNS")
        labels.append(data[pos + 1:pos + 1 + length])
        pos += 1 + length
    return ".".join(label.decode("ascii", "replace") for label in labels).lower(), (end if jumped else pos)


def parse_response(data: bytes, ident: int):
    """Ответ DNS -> (код ответа, обрезан ли, [почтовые серверы по возрастанию предпочтения])."""
    if len(data) < 12:
        raise MxError("слишком короткий ответ DNS")
    got, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    if got != ident or not flags & 0x8000:
        raise MxError("ответ DNS не на наш запрос")
    rcode, truncated = flags & 0x000F, bool(flags & 0x0200)
    if rcode != 0 or truncated:
        return rcode, truncated, []
    pos, found = 12, []
    try:
        for _ in range(questions):
            _, pos = _read_name(data, pos)
            pos += 4
        for _ in range(answers):
            _, pos = _read_name(data, pos)
            rtype, rclass, _ttl, length = struct.unpack("!HHIH", data[pos:pos + 10])
            pos += 10
            if rtype == MX_TYPE and rclass == IN_CLASS:
                (preference,) = struct.unpack("!H", data[pos:pos + 2])
                host, _ = _read_name(data, pos + 2)
                found.append((preference, host))
            pos += length
    except struct.error as exc:
        raise MxError("обрыв ответа DNS") from exc
    return rcode, False, [host for _, host in sorted(found)]


def _address(server):
    if isinstance(server, tuple):
        return server
    return str(server).strip(), 53


def _ask_udp(server, query: bytes, ident: int, timeout: float) -> bytes:
    host, port = _address(server)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.send(query)
        for _ in range(4):                                       # чужие и запоздалые пакеты пропускаем
            data = sock.recv(4096)
            if len(data) >= 2 and struct.unpack("!H", data[:2])[0] == ident:
                return data
    raise MxError("нет ответа на наш запрос")


def _ask_tcp(server, query: bytes, timeout: float) -> bytes:
    host, port = _address(server)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(struct.pack("!H", len(query)) + query)

        def take(size):
            chunks = b""
            while len(chunks) < size:
                part = sock.recv(size - len(chunks))
                if not part:
                    raise MxError("обрыв ответа DNS по TCP")
                chunks += part
            return chunks

        return take(struct.unpack("!H", take(2))[0])


def lookup_mx(domain: str, servers, timeout: float = TIMEOUT) -> list:
    """Почтовые серверы домена (имена в нижнем регистре, без точки на конце). Домена нет или MX нет - []. Ни один сервер DNS
    не дал ответа - MxError."""
    last = "нет серверов DNS"
    for server in list(servers)[:MAX_SERVERS]:
        ident = secrets.randbits(16)
        query = build_query(domain, ident)
        try:
            rcode, truncated, hosts = parse_response(_ask_udp(server, query, ident, timeout), ident)
            if truncated:                                        # ответ не влез в UDP: тот же вопрос по TCP
                rcode, truncated, hosts = parse_response(_ask_tcp(server, query, timeout), ident)
        except socket.timeout:
            last = "таймаут"
            continue
        except (OSError, MxError) as exc:
            last = str(exc) or type(exc).__name__
            continue
        if rcode == 3:                                           # NXDOMAIN: такого домена нет, значит и Яндекса нет
            return []
        if rcode != 0:
            last = f"сервер DNS ответил кодом {rcode}"
            continue
        return hosts
    raise MxError(f"DNS не ответил ({last})")


# --- какие серверы DNS спрашивать ------------------------------------------------------------------------

def _resolv_conf_servers(path: str = "/etc/resolv.conf") -> list:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "nameserver" and "%" not in parts[1]:
            found.append(parts[1])
    return found


def _windows_servers() -> list:
    """Серверы DNS из настроек Windows (GetNetworkParams); любой сбой - пустой список."""
    import ctypes

    class IpAddrString(ctypes.Structure):
        pass

    IpAddrString._fields_ = [("Next", ctypes.POINTER(IpAddrString)), ("IpAddress", ctypes.c_char * 16),
                             ("IpMask", ctypes.c_char * 16), ("Context", ctypes.c_ulong)]

    class FixedInfo(ctypes.Structure):
        _fields_ = [("HostName", ctypes.c_char * 132), ("DomainName", ctypes.c_char * 132),
                    ("CurrentDnsServer", ctypes.POINTER(IpAddrString)), ("DnsServerList", IpAddrString),
                    ("NodeType", ctypes.c_uint), ("ScopeId", ctypes.c_char * 260), ("EnableRouting", ctypes.c_uint),
                    ("EnableProxy", ctypes.c_uint), ("EnableDns", ctypes.c_uint)]

    try:
        api = ctypes.windll.iphlpapi.GetNetworkParams
        size = ctypes.c_ulong(0)
        api(None, ctypes.byref(size))                            # первый вызов только узнаёт нужный размер
        buffer = ctypes.create_string_buffer(size.value)
        if api(buffer, ctypes.byref(size)) != 0:
            return []
        node, found = ctypes.cast(buffer, ctypes.POINTER(FixedInfo)).contents.DnsServerList, []
        while True:
            address = node.IpAddress.decode("ascii", "ignore").strip()
            if address:
                found.append(address)
            if not node.Next:
                return found
            node = node.Next.contents
    except Exception:                                            # нет iphlpapi, чужая ОС, неожиданная раскладка
        return []


def dns_servers(environ=None) -> list:
    """MX_DNS_SERVERS (если задан - только они), иначе системные серверы DNS, затем запасные публичные."""
    environ = environ if environ is not None else os.environ
    own = [part for part in re.split(r"[,;\s]+", str(environ.get("MX_DNS_SERVERS", "")).strip()) if part]
    if own:
        return own
    system = _windows_servers() if os.name == "nt" else _resolv_conf_servers()
    return list(dict.fromkeys(system + list(FALLBACK_SERVERS)))


def make_lookup(servers=None, timeout: float = TIMEOUT):
    """Функция «домен -> список почтовых серверов» по настоящему DNS; серверы определяются один раз."""
    chosen = list(servers) if servers is not None else dns_servers()

    def lookup(domain: str) -> list:
        return lookup_mx(domain, chosen, timeout)

    return lookup


# --- проверка домена с кешем на сутки --------------------------------------------------------------------

class MxChecker:
    """status(домен) -> YANDEX | CLEAN | UNCHECKED. Один домен спрашивается один раз за запуск, ответ DNS хранится в
    mx_cache.json (папка данных) сутки. Сбой DNS (любой) - UNCHECKED, в кеш он не попадает; после MAX_FAILURES доменов подряд
    без ответа DNS в этом запуске больше не спрашивается (чтобы не ждать таймауты на каждом домене)."""

    def __init__(self, folder: str = "", lookup=None, now=None, ttl: timedelta = CACHE_TTL, max_failures: int = MAX_FAILURES):
        self.path = os.path.join(folder, CACHE_NAME) if folder else ""
        self._lookup = lookup or make_lookup()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl, self._max_failures = ttl, max_failures
        self._failures, self._broken, self._dirty = 0, False, False
        self.cause = ""                                          # первая причина сбоя DNS в этом запуске
        self.verdicts = {}                                       # домен -> YANDEX, CLEAN или UNCHECKED
        self.cache = self._load()

    def _load(self) -> dict:
        if not self.path:
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        domains = data.get("domains") if isinstance(data, dict) else None
        return {domain: entry for domain, entry in (domains or {}).items() if isinstance(entry, dict)}

    def _fresh(self, entry: dict) -> bool:
        try:
            age = self._now() - datetime.fromisoformat(entry["time"])
        except (KeyError, TypeError, ValueError):
            return False
        return timedelta(0) <= age < self._ttl and isinstance(entry.get("yandex"), bool)

    def status(self, domain: str) -> str:
        domain = (domain or "").strip().lower().rstrip(".")
        if domain in self.verdicts:
            return self.verdicts[domain]
        entry = self.cache.get(domain)
        if entry is not None and self._fresh(entry):
            verdict = YANDEX if entry["yandex"] else CLEAN
        elif self._broken:
            verdict = UNCHECKED
        else:
            try:
                hosts = [host.strip().lower().rstrip(".") for host in self._lookup(domain)]
            except Exception as exc:                             # сбой проверки не должен ронять сборку писем
                self._failures += 1
                self._broken = self._failures >= self._max_failures
                self.cause = self.cause or (str(exc) or type(exc).__name__)
                verdict = UNCHECKED
            else:
                self._failures = 0
                yandex = any(is_yandex_host(host) for host in hosts)
                self.cache[domain] = {"time": self._now().isoformat(), "yandex": yandex, "mx": hosts}
                self._dirty = True
                verdict = YANDEX if yandex else CLEAN
        self.verdicts[domain] = verdict
        return verdict

    def count(self, verdict: str) -> int:
        return sum(1 for value in self.verdicts.values() if value == verdict)

    @property
    def checked(self) -> int:
        """Сколько доменов проверено (по DNS сейчас или по кешу), то есть по ним есть итог."""
        return len(self.verdicts) - self.count(UNCHECKED)

    def save(self) -> None:
        """Кеш на диск (устаревшие записи уходят). Сбой записи кеш не отменяет: он только ускоряет следующий запуск."""
        if not self.path or not self._dirty:
            return
        fresh = {domain: entry for domain, entry in self.cache.items() if self._fresh(entry)}
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "domains": fresh}, fh, ensure_ascii=False)
            atomic.replace(tmp, self.path)
        except OSError:
            return
        self._dirty = False
