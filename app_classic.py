"""Окно программы: генератор документальных видео для YouTube."""
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import config, runner, script_gen, tts  # noqa: E402

APP_TITLE = "DocuStudio (классический интерфейс)"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        from pipeline.config import VERSION
        self.title(f"{APP_TITLE} — версия {VERSION}")
        self.geometry("980x720")
        self.minsize(820, 600)
        self.cfg = config.load_config()
        self.niches = config.list_niches()
        self.logq = queue.Queue()
        self.progq = queue.Queue()
        self.t0, self.frac, self.stage, self.eta = None, None, "", None
        self.frac_start, self.frac_t0 = 0.0, 0.0
        self.worker = None
        self.stop_requested = False
        self.last_project = None

        style = ttk.Style(self)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_make = ttk.Frame(nb, padding=10)
        self.tab_set = ttk.Frame(nb, padding=10)
        nb.add(self.tab_make, text="  Создать видео  ")
        nb.add(self.tab_set, text="  Настройки и ключи  ")
        self._build_make()
        self._build_settings()
        self.after(150, self._drain_log)
        self._setup_clipboard()
        if not (self.cfg.get("yandex_api_key") or self.cfg.get("anthropic_key")):
            nb.select(self.tab_set)
            self.log("Сначала заполните ключи на вкладке «Настройки и ключи» и нажмите «Сохранить».")

    # ---------- вкладка «Создать» ----------
    def _build_make(self):
        f = self.tab_make
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Ниша:").grid(row=0, column=0, sticky="w", pady=4)
        self.niche_var = tk.StringVar(value=next(iter(self.niches), ""))
        ttk.Combobox(f, textvariable=self.niche_var, values=list(self.niches), state="readonly",
                     width=40).grid(row=0, column=1, sticky="w", pady=4)

        # язык видео берётся из строки «#! ЯЗЫК:» в файле сценария
        self.lang_names = {v["name"]: k for k, v in config.LANGS.items()}
        self.lang_var = tk.StringVar(value=config.lang(self.cfg.get("language", "ru"))["name"])

        ttk.Label(f, text="Сценарий пишет Claude в чате и присылает файлом .txt. "
                          "Нажмите «▶ Видео из сценария…» и выберите этот файл.",
                  foreground="#444").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))
        # скрытые поля для совместимости со старыми функциями
        self.topic_var = tk.StringVar()
        self.min_var = tk.IntVar(value=3)
        self.review_var = tk.BooleanVar(value=True)
        self.ideas = tk.Listbox(f)

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(btns, text="▶ Видео из сценария…", command=self.on_from_text).pack(side="left", padx=(0, 6))
        self.btn_build = ttk.Button(btns, text="Собрать видео из проекта…", command=self.on_build)
        self.btn_build.pack(side="left", padx=6)
        ttk.Button(btns, text="Открыть папку проекта", command=self.on_open).pack(side="left", padx=6)
        ttk.Button(btns, text="📁 Мои футажи", command=self.on_footage).pack(side="left", padx=6)
        ttk.Button(btns, text="■ Стоп", command=self.on_stop).pack(side="left", padx=6)

        self.progress = ttk.Progressbar(f, mode="indeterminate", maximum=100)
        self.progress.grid(row=6, column=0, columnspan=4, sticky="ew")
        self.status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        logf = ttk.Frame(f)
        logf.grid(row=8, column=0, columnspan=4, sticky="nsew", pady=(8, 0))
        f.rowconfigure(8, weight=1)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.logbox = tk.Text(logf, wrap="word", height=14, font=("Consolas", 10), state="disabled")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.logbox["yscrollcommand"] = sb.set

    # ---------- вкладка «Настройки» ----------
    def _build_settings(self):
        f = self.tab_set
        f.columnconfigure(1, weight=1)
        self.vars = {}
        self.secret_entries = []
        rows = [
            ("—", "YANDEX CLOUD (основной вариант, оплата картой РФ)", False, None),
            ("yandex_api_key", "API-ключ Yandex Cloud *", True, "нужен для озвучки и проверки кадров по смыслу"),
            ("yandex_folder_id", "ID каталога Yandex Cloud *", False, "console.yandex.cloud → каталог → ID"),
            ("yandex_voice", "Голос диктора (русский)", False, None),
            ("yandex_speed", "Скорость речи", False, "1.0 — обычная; 0.9 — медленнее"),
            ("pixabay_key", "Ключ Pixabay — бесплатные видео *", True, "pixabay.com/api/docs → после входа ключ на странице"),
            ("pexels_key", "Ключ Pexels (если есть)", True, "выдача новых ключей приостановлена"),
            ("—", "ДРУГИЕ СЕРВИСЫ (необязательно)", False, None),
            ("tts_provider", "Озвучка", False, None),
            ("anthropic_key", "Ключ Claude", True, "если выбрана нейросеть claude"),
            ("openai_key", "Ключ OpenAI", True, "если выбрана озвучка openai"),
            ("elevenlabs_key", "Ключ ElevenLabs", True, "если выбрана озвучка elevenlabs"),
            ("elevenlabs_voice_id", "ID голоса ElevenLabs", False, None),
            ("—", "МОНТАЖ", False, None),
            ("music_volume", "Громкость музыки (0–1)", False, "0.12 — фон, не мешает голосу"),
            ("shot_seconds", "Смена кадра, сек", False, "1.8 — динамичный монтаж (~90 кадров на 3 минуты)"),
        ]
        choices = {
            "yandex_voice": ([f"{v} — {t}" for v, t in tts.RU_VOICES], None),
            "llm_provider": (["yandex", "claude"], "yandex — работает из РФ"),
            "tts_provider": (["yandex", "edge", "openai", "elevenlabs"], "edge — бесплатно, без ключа"),
        }
        for r, (key, label, secret, hint) in enumerate(rows):
            if key == "—":
                ttk.Label(f, text=label, font=("Segoe UI", 9, "bold")).grid(row=r, column=0, columnspan=3,
                                                                          sticky="w", pady=(10, 2))
                continue
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            val0 = str(self.cfg.get(key, ""))
            if key == "yandex_voice":
                val0 = next((f"{v} — {t}" for v, t in tts.RU_VOICES if v == val0), val0)
            var = tk.StringVar(value=val0)
            self.vars[key] = var
            if key in choices:
                values, note = choices[key]
                ttk.Combobox(f, textvariable=var, values=values, state="readonly",
                             width=46 if key == "yandex_voice" else 20).grid(row=r, column=1, sticky="w", pady=3)
                if key == "yandex_voice":
                    ttk.Button(f, text="▶ Прослушать все голоса", command=self.on_voice_samples).grid(
                        row=r, column=2, sticky="w", padx=6)
                else:
                    ttk.Label(f, text=note, foreground="#666").grid(row=r, column=2, sticky="w", padx=6)
                continue
            cell = ttk.Frame(f)
            cell.grid(row=r, column=1, sticky="ew", pady=3)
            cell.columnconfigure(0, weight=1)
            ent = ttk.Entry(cell, textvariable=var, show="•" if secret else "")
            ent.grid(row=0, column=0, sticky="ew")
            if secret:
                self.secret_entries.append(ent)
            if key not in ("music_volume", "shot_seconds", "yandex_speed"):
                ttk.Button(cell, text="Вставить", width=9,
                           command=lambda v=var: self._paste_into(v)).grid(row=0, column=1, padx=(4, 0))
            if hint:
                ttk.Label(f, text=hint, foreground="#666").grid(row=r, column=2, sticky="w", padx=6)
        ttk.Button(f, text="Сохранить", command=self.on_save).grid(row=len(rows), column=1, sticky="w", pady=12)
        self.save_status = ttk.Label(f, text="")
        self.save_status.grid(row=len(rows), column=1, sticky="w", padx=(110, 0))
        ttk.Button(f, text="✔ Проверить всё", command=self.on_selfcheck).grid(
            row=len(rows), column=1, sticky="w", padx=(230, 0), pady=12)
        self.show_keys = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Показать ключи", variable=self.show_keys,
                        command=self._toggle_keys).grid(row=len(rows), column=2, sticky="w", padx=6)
        ttk.Label(f, text="Скопируйте ключ на сайте (Ctrl+C), затем нажмите «Вставить» у нужного поля "
                          "(или Ctrl+V / правая кнопка мыши → Вставить).",
                  foreground="#444").grid(row=len(rows) + 1, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Ключи хранятся только у вас в файле config.json рядом с программой. Никому его не отправляйте.",
                  foreground="#a00").grid(row=len(rows) + 2, column=0, columnspan=3, sticky="w")

    # ---------- вставка из буфера обмена ----------
    def _paste_into(self, var):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning(APP_TITLE, "Буфер обмена пуст. Сначала скопируйте ключ на сайте (Ctrl+C).")
            return
        var.set(text.strip().replace("\n", "").replace("\r", "").replace(" ", ""))

    def _toggle_keys(self):
        for e in self.secret_entries:
            e.configure(show="" if self.show_keys.get() else "•")

    def _setup_clipboard(self):
        """Ctrl+V / Ctrl+C / Ctrl+X / Ctrl+A при любой раскладке + меню правой кнопкой."""
        actions = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}   # коды клавиш V, C, X

        def on_ctrl_key(e):
            if e.keysym.lower() in ("v", "c", "x", "a"):
                return None                     # английская раскладка — работает штатно
            w = e.widget
            if e.keycode in actions:
                w.event_generate(actions[e.keycode])
                return "break"
            if e.keycode == 65:
                self._select_all(w)
                return "break"
            return None

        self.bind_all("<Control-KeyPress>", on_ctrl_key)
        for cls in ("TEntry", "Entry", "Text"):
            self.bind_class(cls, "<Button-3>", self._context_menu)

    @staticmethod
    def _select_all(w):
        try:
            w.select_range(0, "end")
            w.icursor("end")
        except Exception:
            try:
                w.tag_add("sel", "1.0", "end")
            except Exception:
                pass

    def _context_menu(self, e):
        w = e.widget
        w.focus_set()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Вставить", command=lambda: w.event_generate("<<Paste>>"))
        menu.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
        menu.add_command(label="Вырезать", command=lambda: w.event_generate("<<Cut>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: self._select_all(w))
        menu.tk_popup(e.x_root, e.y_root)

    def _check_yandex_fields(self):
        """Ловит частую ошибку: ID каталога (b1g…) вставлен в поле ключа и наоборот."""
        key = self.vars["yandex_api_key"].get().strip()
        folder = config.clean_folder_id(self.vars["yandex_folder_id"].get())
        self.vars["yandex_folder_id"].set(folder)          # убираем хвосты вроде /dashboard
        if key.startswith("b1g") and folder.startswith("AQV"):
            self.vars["yandex_api_key"].set(folder)
            self.vars["yandex_folder_id"].set(key)
            messagebox.showinfo(APP_TITLE, "Ключ Yandex и ID каталога были перепутаны местами — "
                                           "программа поменяла их сама.")
            return True
        if key.startswith("b1g"):
            messagebox.showerror(APP_TITLE, "В поле «API-ключ Yandex Cloud» вставлен ID каталога (начинается на b1g).\n\n"
                                            "Нужен СЕКРЕТНЫЙ ключ сервисного аккаунта — он обычно начинается на AQVN.\n"
                                            "ID каталога вставьте во второе поле.")
            return False
        if key.startswith("aje"):
            messagebox.showerror(APP_TITLE, "В поле ключа вставлен ИДЕНТИФИКАТОР ключа (начинается на aje).\n\n"
                                            "Нужен СЕКРЕТНЫЙ ключ — длинная строка, обычно начинается на AQVN.")
            return False
        if folder and not folder.startswith("b1g"):
            return messagebox.askyesno(APP_TITLE, "ID каталога обычно начинается на b1g, а у вас — иначе.\n"
                                                  "Сохранить всё равно?")
        return True

    def on_save(self, quiet=False):
        if not self._check_yandex_fields():
            return
        for k, v in self.vars.items():
            val = v.get().strip().replace("\n", "").replace("\r", "")
            if k == "yandex_voice":
                val = val.split(" — ")[0].strip()
            if k in ("music_volume", "shot_seconds"):
                try:
                    val = float(val.replace(",", "."))
                except ValueError:
                    messagebox.showerror(APP_TITLE, f"Поле «{k}» должно быть числом.")
                    return
            self.cfg[k] = val
        try:
            path = config.save_config(self.cfg)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить настройки:\n{e}\n\n"
                                            f"Распакуйте архив в обычную папку (например C:\\VideoGen) "
                                            f"и запускайте программу оттуда через start.bat.")
            return
        filled = [n for k, n in (("yandex_api_key", "ключ Yandex"), ("yandex_folder_id", "ID каталога"),
                                 ("pixabay_key", "ключ Pixabay")) if self.cfg.get(k)]
        missing = [n for k, n in (("yandex_api_key", "ключ Yandex"), ("yandex_folder_id", "ID каталога"),
                                  ("pixabay_key", "ключ Pixabay")) if not self.cfg.get(k)]
        self.save_status.configure(text="✔ Сохранено", foreground="#0a0")
        self.log(f"Настройки сохранены: {path}")
        msg = "Настройки сохранены.\n\nЗаполнено: " + (", ".join(filled) or "ничего")
        if missing:
            msg += "\nЕщё не заполнено: " + ", ".join(missing)
        if not quiet:
            messagebox.showinfo(APP_TITLE, msg + "\n\nНажмите «✔ Проверить всё», чтобы проверить ключи.")

    # ---------- лог ----------
    def log(self, msg):
        self.logq.put(str(msg))

    @staticmethod
    def _fmt(sec):
        sec = int(max(0, sec))
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    def set_progress(self, frac, stage):
        """Вызывается из рабочего потока: доля 0..1 и название этапа."""
        self.progq.put((max(0.0, min(1.0, frac)), stage))

    def _update_status(self):
        if self.t0 is None:
            return
        elapsed = time.time() - self.t0
        if self.frac is None:
            self.status_var.set(f"Идёт работа… прошло {self._fmt(elapsed)}")
            return
        pct = int(self.frac * 100)
        if self.frac >= 1.0:
            text = f"Готово за {self._fmt(elapsed)}"
        elif self.frac < 0.03 or elapsed < 10:
            text = f"{pct}% · {self.stage} · прошло {self._fmt(elapsed)} · осталось: считаю…"
        else:
            # оставшееся время по средней скорости, сглаженное, чтобы цифра не прыгала
            raw = (elapsed - self.frac_t0) / max(self.frac - self.frac_start, 1e-3) * (1 - self.frac)
            self.eta = raw if self.eta is None else 0.8 * self.eta + 0.2 * raw
            text = (f"{pct}% · {self.stage} · прошло {self._fmt(elapsed)} · "
                    f"осталось ~{self._fmt(self.eta)}")
        self.status_var.set(text)

    def _drain_log(self):
        try:
            while True:
                frac, stage = self.progq.get_nowait()
                if self.frac is None:
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    # часть работы могла быть сделана раньше (кеш) — скорость считаем от этой точки
                    self.frac_start, self.frac_t0 = frac, time.time() - self.t0
                self.frac, self.stage = frac, stage
                self.progress["value"] = frac * 100
        except queue.Empty:
            pass
        self._update_status()
        try:
            while True:
                msg = self.logq.get_nowait()
                self.logbox.configure(state="normal")
                self.logbox.insert("end", msg + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    # ---------- действия ----------
    def _niche(self):
        name = self.niche_var.get()
        if name not in self.niches:
            raise RuntimeError("Выберите нишу.")
        return config.load_niche(self.niches[name]), self.niches[name]

    def _run(self, fn):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Уже идёт работа. Дождитесь окончания или нажмите «Стоп».")
            return
        self.stop_requested = False
        self.cfg = config.load_config()
        self._apply_lang()
        self.t0, self.frac, self.stage, self.eta = time.time(), None, "", None
        self.frac_start, self.frac_t0 = 0.0, 0.0
        self.progress.configure(mode="indeterminate", value=0)

        result = {"error": None}

        def wrapper():
            try:
                fn()
            except Exception as e:
                result["error"] = str(e)
                self.log(f"ОШИБКА: {e}")
                with open(config.ROOT / "error_log.txt", "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc() + "\n")
            finally:
                def done():
                    self.progress.stop()
                    elapsed = self._fmt(time.time() - self.t0) if self.t0 else ""
                    finished = self.frac is not None and self.frac >= 1.0 and not result["error"]
                    if not finished and not (self.frac is None and not result["error"]):
                        self.status_var.set(f"Остановлено · прошло {elapsed}")
                    self.t0 = None
                    # уведомление: только для сборки видео (у неё есть прогресс) и для ошибок
                    if finished:
                        self.bell()
                        messagebox.showinfo(APP_TITLE, f"Видео готово ✓\n\nСборка заняла {elapsed}.\n"
                                                       f"Папка с видео открыта.")
                    elif result["error"] and not self.stop_requested:
                        self.bell()
                        messagebox.showerror(APP_TITLE, f"Сборка остановилась с ошибкой:\n\n"
                                                        f"{result['error'][:500]}\n\n"
                                                        f"Подробности — в файле error_log.txt в папке программы.")
                self.after(200, done)

        self.progress.start(12)
        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _apply_lang(self):
        self.cfg["language"] = self.lang_names.get(self.lang_var.get(), "ru")

    def on_ideas(self):
        self._apply_lang()

        def job():
            niche, _ = self._niche()
            topics = script_gen.suggest_topics(self.cfg, niche, log=self.log)

            def fill():
                self.ideas.delete(0, "end")
                for t in topics:
                    self.ideas.insert("end", t)
            self.after(0, fill)
            self.log(f"Предложено тем: {len(topics)}.")
        self._run(job)

    def _pick_idea(self, _e):
        sel = self.ideas.curselection()
        if sel:
            self.topic_var.set(self.ideas.get(sel[0]))

    def on_create(self):
        topic = self.topic_var.get().strip()
        if not topic:
            messagebox.showwarning(APP_TITLE, "Введите тему видео или нажмите «Подобрать темы».")
            return
        minutes = max(3, int(self.min_var.get() or 3))
        self._apply_lang()
        review = self.review_var.get()

        def job():
            niche, npath = self._niche()
            project = runner.new_project_dir(topic)
            self.last_project = project
            self.log(f"Проект: {project}")
            runner.step_script(self.cfg, niche, npath, topic, minutes, project, self.log)
            if review:
                self.log("Сценарий на проверке. Откройте папку проекта: script.txt — текст, "
                         "ПРОВЕРИТЬ_ФАКТЫ.txt — что сверить. Затем нажмите «Собрать видео из проекта…».")
                self.after(0, self.on_open)
                return
            runner.step_build(self.cfg, niche, project, self.log, lambda: self.stop_requested,
                              self.set_progress)
            self.after(0, self.on_open)
        self._run(job)

    def on_from_text(self):
        path = filedialog.askopenfilename(title="Файл сценария (.txt)", filetypes=[("Сценарий", "*.txt")])
        if not path:
            return

        def job():
            _, npath = self._niche()
            niche = config.load_niche(npath)
            project = runner.project_from_text(path, npath, self.log)
            self.last_project = project
            runner.step_build(self.cfg, niche, project, self.log, lambda: self.stop_requested,
                              self.set_progress)
            self.after(0, self.on_open)
        self._run(job)

    def on_build(self):
        start = str(self.last_project or config.OUTPUT_DIR)
        folder = filedialog.askdirectory(title="Папка проекта (внутри output)", initialdir=start)
        if not folder:
            return
        project = Path(folder)
        self.last_project = project

        def job():
            data = runner._load_json(project / "script.json", {})
            npath = data.get("niche_file")
            if npath and Path(npath).exists():
                niche = config.load_niche(npath)
            else:
                niche, _ = self._niche()
            runner.step_build(self.cfg, niche, project, self.log, lambda: self.stop_requested,
                              self.set_progress)
            self.after(0, self.on_open)
        self._run(job)

    def on_selfcheck(self):
        """Проверяет ключи и доступность сервисов. Сначала сохраняет введённые значения."""
        self.on_save(quiet=True)

        def job():
            from pipeline import selfcheck
            self.log("Проверка подключений...")
            results = selfcheck.run_all(self.cfg, self.log)
            ok = all(r[1] for r in results if r[3])
            lines = [("✔ " if good else "✘ ") + name + (f" — {msg}" if msg else "")
                     for name, good, msg, _ in results]
            for ln in lines:
                self.log("  " + ln)
            text = "\n".join(lines)
            self.after(0, lambda: (messagebox.showinfo if ok else messagebox.showwarning)(
                APP_TITLE, ("Всё готово к работе.\n\n" if ok else "Есть проблемы:\n\n") + text))
        self._run(job)

    def on_voice_samples(self):
        """Озвучивает одну фразу всеми голосами Yandex и открывает папку с файлами — выбрать на слух."""
        self.on_save(quiet=True)
        if not self.cfg.get("yandex_api_key"):
            messagebox.showwarning(APP_TITLE, "Сначала вставьте API-ключ Yandex Cloud и нажмите «Сохранить».")
            return
        sample = ("В декабре тысяча девятисотого года с маяка на крошечном острове исчезли три смотрителя. "
                  "Их так и не нашли. Ни тел, ни лодки, ни записки.")
        out_dir = config.ROOT / "voice_samples"

        def job():
            from pipeline import tts as _tts
            self.log("Озвучиваю пример всеми голосами...")
            made, errors = _tts.voice_samples(self.cfg, sample, out_dir, self.log)

            def done():
                if made:
                    if os.name == "nt":
                        os.startfile(str(out_dir))
                    messagebox.showinfo(APP_TITLE, f"Готово: {len(made)} файлов в папке voice_samples.\n"
                                                   "Название файла = голос. Выберите лучший в списке "
                                                   "«Голос диктора» и нажмите «Сохранить»."
                                        + ("\n\nНе сработали:\n" + "\n".join(errors) if errors else ""))
                else:
                    messagebox.showwarning(APP_TITLE, "Ни один голос не озвучил пример:\n\n" + "\n".join(errors[:5]))
            self.after(0, done)
        self._run(job)

    def on_footage(self):
        from pipeline import library
        library.FOOTAGE_DIR.mkdir(exist_ok=True)
        if os.name == "nt":
            os.startfile(str(library.FOOTAGE_DIR))
        else:
            subprocess.Popen(["xdg-open", str(library.FOOTAGE_DIR)])

    def on_stop(self):
        self.stop_requested = True
        self.log("Остановка после текущего шага... Уже сделанное сохранится, повторная сборка продолжит с этого места.")

    def on_open(self):
        target = self.last_project or config.OUTPUT_DIR
        os.makedirs(target, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))
        else:
            subprocess.Popen(["xdg-open", str(target)])


if __name__ == "__main__":
    App().mainloop()
