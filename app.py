"""DocuStudio — генератор документальных видео для YouTube.
Запуск: start.bat или ярлык на рабочем столе. Окно программы открывается как отдельное приложение."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    import server
    server.main(sys.argv[1:])
