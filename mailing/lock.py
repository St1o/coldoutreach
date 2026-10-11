"""Замок «в этой папке уже идёт отправка»: две отправки одновременно нарушили бы паузы и дневной лимит."""
import os
import time


LOCK_NAME = "mailer.lock"        # в папке таблицы: пока он есть, в этой папке уже идёт отправка
LOCK_MAX_AGE = 24 * 3600         # замок старше суток считаем забытым
STILL_ACTIVE = 259       # код «процесс ещё работает» у GetExitCodeProcess


def _windows_pid_alive(pid: int, kernel32=None) -> bool:
    """OpenProcess открывает и уже завершившийся процесс, пока кто-то держит его дескриптор, поэтому одного
    успешного открытия мало: смотрим код завершения."""
    import ctypes
    from ctypes import wintypes
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True                                          # не удалось узнать: считаем живым, так безопаснее
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Locked(Exception):
    def __init__(self, pid: int):
        super().__init__(f"в этой папке уже идёт отправка (процесс {pid})")
        self.pid = pid


def lock_holder(folder: str) -> int:
    """PID работающей отправки в папке или 0. Замок мёртвого или забытого процесса удаляется."""
    path = os.path.join(folder, LOCK_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
        age = time.time() - os.path.getmtime(path)
    except (OSError, ValueError):
        pid, age = 0, 0
    if pid and age < LOCK_MAX_AGE and pid_alive(pid):
        return pid
    try:
        os.remove(path)
    except OSError:
        pass
    return 0


class SendLock:
    """Одна отправка на папку: две одновременные нарушили бы паузы между письмами и дневной лимит."""

    def __init__(self, folder: str):
        self.path = os.path.join(folder, LOCK_NAME)

    def __enter__(self):
        pid = lock_holder(os.path.dirname(self.path))
        if pid and pid != os.getpid():
            raise Locked(pid)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Locked(lock_holder(os.path.dirname(self.path)) or -1)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
