"""Мгновенная остановка сборки по кнопке «Стоп».

Раньше кнопка только ставила флаг «остановиться после текущего шага», а шаг (проверка кадров Claude Code,
ожидание сброса лимита, монтаж) мог идти десятки минут. Теперь:
  * все дочерние процессы программы (Claude Code, ffmpeg) запоминаются;
  * «Стоп» сразу их завершает (вместе с их дочерними процессами);
  * пока стоп в силе, новые процессы не запускаются — код, который их ждёт, получает ошибку
    «Остановлено пользователем», и сборка заканчивается. Всё уже сделанное лежит в папке проекта.
"""
import os
import subprocess
import threading
import time
import weakref

EVENT = threading.Event()
MSG = "Остановлено пользователем"
_PROCS = weakref.WeakSet()
_LOCK = threading.Lock()
_ORIG = subprocess.Popen


class StoppedError(RuntimeError):
    pass


class _TrackedPopen(_ORIG):
    def __init__(self, *a, **kw):
        if EVENT.is_set():
            raise StoppedError(MSG)
        super().__init__(*a, **kw)
        with _LOCK:
            _PROCS.add(self)


def install():
    """Подменить subprocess.Popen (subprocess.run и check_output тоже идут через него)."""
    if subprocess.Popen is not _TrackedPopen:
        subprocess.Popen = _TrackedPopen


def reset():
    EVENT.clear()


def requested():
    return EVENT.is_set()


def check():
    if EVENT.is_set():
        raise StoppedError(MSG)


def sleep(sec, step=2.0):
    """Пауза, которую прерывает «Стоп»."""
    end = time.time() + sec
    while time.time() < end:
        check()
        time.sleep(min(step, max(0.05, end - time.time())))
    check()


def stop():
    """Нажали «Стоп»: завершить все запущенные процессы сборки."""
    EVENT.set()
    with _LOCK:
        procs = list(_PROCS)
    n = 0
    for p in procs:
        try:
            if p.poll() is not None:
                continue
            if os.name == "nt":                 # вместе с дочерними (Claude Code запускает node)
                _ORIG(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, creationflags=0x08000000).wait(timeout=10)
            else:
                p.kill()
            n += 1
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    return n
