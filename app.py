"""DocuStudio — генератор документальных видео для YouTube.
Запуск: start.bat или ярлык на рабочем столе. Окно программы открывается как отдельное приложение.

На Windows программа работает под собственным именем процесса DocuStudio.exe
(копия pythonw.exe в .venv\\Scripts). Так её можно выбрать в VPN-клиенте
в списке приложений для раздельной маршрутизации."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
EXE_NAME = "DocuStudio.exe"


def _base_python_dir():
    """Папка настоящего Python, на котором построено окружение .venv."""
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip().lower() == "home" and Path(v.strip()).is_dir():
                return Path(v.strip())
    except OSError:
        pass
    base = getattr(sys, "_base_executable", "") or sys.executable
    return Path(base).parent


def _embed_icon(exe, ico):
    """Вшивает иконку DocuStudio в DocuStudio.exe — тогда и панель задач, и закреплённый значок,
    и Alt+Tab показывают её, а не змейку Python. True — получилось."""
    if os.name != "nt" or not Path(ico).exists():
        return False
    import ctypes
    import struct
    from ctypes import wintypes as wt
    try:
        data = Path(ico).read_bytes()
        _, typ, count = struct.unpack_from("<HHH", data, 0)
        if typ != 1 or not count:
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LoadLibraryExW.restype = wt.HMODULE
        k32.BeginUpdateResourceW.restype = wt.HANDLE
        k32.UpdateResourceW.argtypes = [wt.HANDLE, wt.LPVOID, wt.LPVOID, wt.WORD, wt.LPVOID, wt.DWORD]
        RT_ICON, RT_GROUP_ICON = 3, 14
        # какие группы иконок уже есть в exe (их и заменяем, чтобы Windows точно взяла нашу)
        names, langs = [], []
        h = k32.LoadLibraryExW(str(exe), None, 0x2)                 # LOAD_LIBRARY_AS_DATAFILE
        if h:
            ENUMNAME = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMODULE, wt.LPVOID, wt.LPVOID, ctypes.c_ssize_t)
            ENUMLANG = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMODULE, wt.LPVOID, wt.LPVOID, wt.WORD, ctypes.c_ssize_t)

            def on_name(hm, t, name, p):
                v = name or 0
                names.append(v if v < 0x10000 else ctypes.wstring_at(name))
                return True

            def on_lang(hm, t, name, lang, p):
                langs.append(lang)
                return True
            k32.EnumResourceNamesW(h, ctypes.c_void_p(RT_GROUP_ICON), ENUMNAME(on_name), 0)
            if names:
                n0 = names[0]
                k32.EnumResourceLanguagesW(h, ctypes.c_void_p(RT_GROUP_ICON),
                                           ctypes.c_void_p(n0) if isinstance(n0, int) else ctypes.c_wchar_p(n0),
                                           ENUMLANG(on_lang), 0)
            k32.FreeLibrary(h)
        names = names or [1]
        langs = sorted(set(langs)) or [0]
        hu = k32.BeginUpdateResourceW(str(exe), False)
        if not hu:
            return False
        group = struct.pack("<HHH", 0, 1, count)
        for i in range(count):
            w, hgt, colors, res, planes, bits, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
            img = data[off:off + size]
            buf = ctypes.create_string_buffer(img, len(img))
            for lang in langs:
                k32.UpdateResourceW(hu, ctypes.c_void_p(RT_ICON), ctypes.c_void_p(500 + i), lang, buf, len(img))
            group += struct.pack("<BBBBHHIH", w, hgt, colors, res, planes, bits, size, 500 + i)
        gbuf = ctypes.create_string_buffer(group, len(group))
        for n in names:
            nm = ctypes.c_void_p(n) if isinstance(n, int) else ctypes.c_wchar_p(n)
            for lang in langs:
                k32.UpdateResourceW(hu, ctypes.c_void_p(RT_GROUP_ICON), nm, lang, gbuf, len(group))
        return bool(k32.EndUpdateResourceW(hu, False))
    except Exception:
        return False


def branded_exe():
    """Создаёт .venv\\Scripts\\DocuStudio.exe (копия pythonw.exe + его dll) и проверяет, что он запускается.
    Возвращает путь или None."""
    if os.name != "nt":
        return None
    scripts = ROOT / ".venv" / "Scripts"
    if not scripts.is_dir():
        return None
    target = scripts / EXE_NAME
    ok_flag = scripts / "DocuStudio.ok"
    base = _base_python_dir()
    src = base / "pythonw.exe"
    if not src.exists():
        return None
    stamp = scripts / "DocuStudio.stamp"
    want = f"{src.stat().st_size}:{int((ROOT / 'app.ico').stat().st_mtime) if (ROOT / 'app.ico').exists() else 0}:2"
    try:
        fresh = not target.exists() or not stamp.exists() or stamp.read_text().strip() != want
        if fresh:
            ok_flag.unlink(missing_ok=True)
            shutil.copy2(src, target)
            if not _embed_icon(target, ROOT / "app.ico"):
                shutil.copy2(src, target)                      # не вышло — просто копия, без своей иконки
            stamp.write_text(want)
            for dll in base.glob("*.dll"):                     # python3xx.dll, vcruntime и т.п.
                d = scripts / dll.name
                if not d.exists() or d.stat().st_size != dll.stat().st_size:
                    shutil.copy2(dll, d)
        if not ok_flag.exists():
            env = {k: v for k, v in os.environ.items() if k != "__PYVENV_LAUNCHER__"}
            p = subprocess.run([str(target), "-c", "import requests, PIL, numpy"], cwd=str(ROOT),
                               env=env, timeout=60, creationflags=0x08000000)
            if p.returncode != 0:
                shutil.copy2(src, target)                      # вдруг виновата вшитая иконка — пробуем чистую копию
                p = subprocess.run([str(target), "-c", "import requests, PIL, numpy"], cwd=str(ROOT),
                                   env=env, timeout=60, creationflags=0x08000000)
                if p.returncode != 0:
                    return None
            ok_flag.write_text("ok")
        return target
    except (OSError, subprocess.SubprocessError):
        return None


def relaunch_as_branded():
    """Если запущены не как DocuStudio.exe — перезапускаемся под этим именем. True — текущий процесс должен выйти."""
    if os.name != "nt" or os.environ.get("DOCUSTUDIO_CHILD"):
        return False
    if Path(sys.executable).name.lower() == EXE_NAME.lower():
        return False
    exe = branded_exe()
    if not exe:
        return False
    env = {k: v for k, v in os.environ.items() if k != "__PYVENV_LAUNCHER__"}
    env["DOCUSTUDIO_CHILD"] = "1"
    try:
        subprocess.Popen([str(exe), str(ROOT / "app.py")] + sys.argv[1:], cwd=str(ROOT), env=env,
                         creationflags=0x08000000)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if relaunch_as_branded():
        sys.exit(0)
    import server
    server.main(sys.argv[1:])
