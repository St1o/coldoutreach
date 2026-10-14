"""Замена файла целиком: запись во временный файл рядом и os.replace. В Windows os.replace падает с «[WinError 5] Отказано
в доступе», если файл в этот миг открыт другой программой (страница сайта читает его при опросе состояния, антивирус,
индексатор, Excel): чтение длится миллисекунды, поэтому замену повторяем несколько секунд."""
import os
import time

ATTEMPTS = 40          # попыток замены
DELAY = 0.15           # пауза между попытками, с: всего до 6 с


def replace(tmp: str, path: str, attempts: int = ATTEMPTS, delay: float = DELAY, sleep=time.sleep) -> None:
    """os.replace(tmp, path) с повтором при PermissionError; не вышло за все попытки - ошибка поднимается как есть
    (файл навсегда занят: например, таблица открыта в Excel)."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            sleep(delay)
