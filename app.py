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
    try:
        fresh = not target.exists() or target.stat().st_size != src.stat().st_size
        if fresh:
            ok_flag.unlink(missing_ok=True)
            shutil.copy2(src, target)
            for dll in base.glob("*.dll"):                     # python3xx.dll, vcruntime и т.п.
                d = scripts / dll.name
                if not d.exists() or d.stat().st_size != dll.stat().st_size:
                    shutil.copy2(dll, d)
        if not ok_flag.exists():
            env = {k: v for k, v in os.environ.items() if k != "__PYVENV_LAUNCHER__"}
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
