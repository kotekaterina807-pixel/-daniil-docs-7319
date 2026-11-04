"use strict";
/* DocuStudio — интерфейс. Вся работа идёт в программе на этом компьютере, страница только показывает её. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const params = new URLSearchParams(location.search);
let TOKEN = params.get("t") || "";
try { if (TOKEN) sessionStorage.setItem("ds-t", TOKEN); else TOKEN = sessionStorage.getItem("ds-t") || ""; } catch (e) {}

const S = { cfg: {}, niches: [], voices: [], projects: [], logNext: 0, doneId: null, file: null,
            lastJobKind: "", restarting: false, offline: 0 };

const PAGES = {
  home: ["Главная", "Документальные видео из вашего сценария"],
  projects: ["Проекты", "Все собранные видео: смотреть, пересобрать, взять описание для YouTube"],
  voices: ["Голоса", "Выбор голоса диктора на слух"],
  music: ["Музыка", "Фоновая музыка под голосом"],
  settings: ["Параметры", "Оформление, звук, монтаж, ключи и обновления"],
};

// ---------- запросы к программе ----------
async function api(path, body, raw) {
  const opt = { method: body === undefined ? "GET" : "POST", headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    if (raw) { opt.body = body; opt.headers["Content-Type"] = "application/octet-stream"; }
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`);
  return data;
}
const fileUrl = (path) => `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;

// ---------- уведомления ----------
function toast(text, kind = "", ms = 3200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, ms);
}
function modal(title, bodyHtml, actions = [{ text: "Закрыть" }]) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  const box = $("#modalActions");
  box.innerHTML = "";
  actions.forEach((a) => {
    const b = document.createElement("button");
    b.className = `btn ${a.cls || "ghost"}`;
    b.textContent = a.text;
    b.onclick = async () => { if (a.onClick) { const keep = await a.onClick(); if (keep === true) return; } closeModal(); };
    box.appendChild(b);
  });
  $("#modalBack").hidden = false;
}
function closeModal() {
  $$("#modalBody video").forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
  $("#modal").classList.remove("wide");
  $("#modalBack").hidden = true;
}
$("#modalBack").addEventListener("mousedown", (e) => { if (e.target.id === "modalBack") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function chime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [[880, 0], [1320, 0.16]].forEach(([f, t]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.5);
      o.connect(g).connect(ctx.destination); o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.55);
    });
  } catch (e) {}
}

// ---------- плавное нажатие: волна от места клика ----------
document.addEventListener("pointerdown", (e) => {
  const b = e.target.closest(".btn, .tile, .icon-btn");
  if (!b || b.disabled) return;
  const r = b.getBoundingClientRect(), size = Math.max(r.width, r.height);
  const s = document.createElement("span");
  s.className = "ripple";
  s.style.cssText = `width:${size}px;height:${size}px;left:${e.clientX - r.left - size / 2}px;top:${e.clientY - r.top - size / 2}px`;
  b.appendChild(s);
  setTimeout(() => s.remove(), 600);
});

// ---------- тема ----------
const mq = window.matchMedia("(prefers-color-scheme: dark)");
function resolveTheme(t) { return t === "system" ? (mq.matches ? "dark" : "light") : (t || "light"); }
function applyTheme(t) {
  document.documentElement.dataset.theme = resolveTheme(t);
  try { localStorage.setItem("ds-theme", resolveTheme(t)); } catch (e) {}
  $$("#themeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.v === (t || "light")));
}
mq.addEventListener?.("change", () => { if (S.cfg.theme === "system") applyTheme("system"); });
$("#themeBtn").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  S.cfg.theme = next; applyTheme(next); save({ theme: next }, true);
};
$$("#themeSeg button").forEach((b) => b.onclick = () => { S.cfg.theme = b.dataset.v; applyTheme(b.dataset.v); save({ theme: b.dataset.v }, true); });

// ---------- навигация ----------
function go(page) {
  stopAudio();
  if (page === "home") renderVoiceCard();
  $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  $("#pageTitle").textContent = PAGES[page][0];
  $("#pageSub").textContent = PAGES[page][1];
  if (page === "projects") loadProjects();
  if (page === "voices") loadVoices();
  if (page === "music") loadMusic();
  if (page === "settings") moveInk();
  $(".main").scrollTop = 0;
}
$$(".nav-item").forEach((n) => n.onclick = () => go(n.dataset.page));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) go(g.dataset.goto);
  const u = e.target.closest("[data-url]");
  if (u) api("/api/open_url", { url: u.dataset.url }).catch((err) => toast(err.message, "err"));
  const o = e.target.closest("[data-open]");
  if (o) api("/api/open", { what: o.dataset.open }).catch((err) => toast(err.message, "err"));
});

// вкладки параметров с плавной полоской
function moveInk() {
  const a = $("#setTabs .tab.active"), ink = $("#tabInk");
  if (a) { ink.style.left = a.offsetLeft + "px"; ink.style.width = a.offsetWidth + "px"; }
}
$$("#setTabs .tab").forEach((t) => t.onclick = () => {
  $$("#setTabs .tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${t.dataset.tab}`));
  moveInk();
});
window.addEventListener("resize", moveInk);

// ---------- сохранение параметров ----------
let saveTimer = null, pending = {};
function save(values, quiet) {
  Object.assign(pending, values);
  Object.assign(S.cfg, values);
  if ("tts_provider" in values || "elevenlabs_voice_id" in values) {
    setTimeout(renderVoiceCard, 0);
    if ("tts_provider" in values) VS.provider = null;
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const v = pending; pending = {};
    try {
      const r = await api("/api/save", { values: v });
      (r.notes || []).forEach((n) => toast(n, "err", 6000));
      if (!quiet) toast("Сохранено", "ok", 1600);
      if (r.notes && r.notes.length) await loadInit();
    } catch (e) { toast("Не сохранилось: " + e.message, "err"); }
  }, 350);
}
function rangeFill(el) {
  const p = ((el.value - el.min) / (el.max - el.min)) * 100;
  el.style.setProperty("--p", p + "%");
}
function bindSettings() {
  $$("[data-key]").forEach((el) => {
    const k = el.dataset.key, v = S.cfg[k];
    if (el.type === "checkbox") el.checked = !!v && v !== "false";
    else if (v !== undefined && v !== null) el.value = v;
    if (el.type === "range") rangeFill(el);
    if (el.dataset.bound) return;
    el.dataset.bound = 1;
    const ev = el.type === "range" ? "input" : "change";
    el.addEventListener(ev, () => {
      const val = el.type === "checkbox" ? el.checked : el.type === "range" ? parseFloat(el.value) : el.value;
      if (el.type === "range") rangeFill(el);
      if (k === "shot_seconds") $("#shotVal").textContent = `${parseFloat(el.value).toFixed(1)} с`;
      save({ [k]: val }, el.type === "range");
    });
  });
  $("#shotVal").textContent = `${parseFloat($("#shotSec").value || 1.8).toFixed(1)} с`;
}

// поля ключей: скрыть/показать, вставить
const EYE = '<svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const PASTE = '<svg viewBox="0 0 24 24"><rect x="8" y="3" width="8" height="4" rx="1"/><path d="M8 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/></svg>';
function renderKeys() {
  $$(".key-row").forEach((row) => {
    const k = row.dataset.k, secret = row.dataset.secret === "1";
    row.innerHTML = `<div class="key-label">${esc(row.dataset.label)}${row.dataset.hint ? `<small class="muted">${esc(row.dataset.hint)}</small>` : ""}</div>
      <div class="key-box"><input class="key-input" type="${secret ? "password" : "text"}" spellcheck="false" autocomplete="off">
      ${secret ? `<button class="btn ghost small" data-act="eye" title="Показать">${EYE}</button>` : ""}
      <button class="btn ghost small" data-act="paste" title="Вставить из буфера">${PASTE}Вставить</button></div>`;
    const inp = $("input", row);
    inp.value = S.cfg[k] || "";
    const commit = () => {
      const v = inp.value.replace(/\s+/g, "");
      inp.value = v;
      if (v !== (S.cfg[k] || "")) save({ [k]: v });
    };
    inp.addEventListener("change", commit);
    inp.addEventListener("blur", commit);
    $$("[data-act]", row).forEach((b) => b.onclick = async () => {
      if (b.dataset.act === "eye") { inp.type = inp.type === "password" ? "text" : "password"; return; }
      try { inp.value = (await navigator.clipboard.readText()).trim(); commit(); }
      catch (e) { inp.focus(); toast("Нажмите Ctrl+V в поле — браузер не дал прочитать буфер", "", 4000); }
    });
  });
}

// ---------- загрузка сценария ----------
const dz = $("#dropzone");
async function setFile(f) {
  if (!f) return;
  if (!/\.txt$/i.test(f.name)) { toast("Нужен файл сценария .txt", "err"); return; }
  S.file = { name: f.name, text: await f.text() };
  dz.classList.add("has-file");
  $("#dzText").innerHTML = `<b>${esc(f.name)}</b><span>${(f.size / 1024).toFixed(1)} КБ · нажмите, чтобы выбрать другой</span>`;
  $("#buildBtn").disabled = false;
}
$("#scriptFile").addEventListener("change", (e) => setFile(e.target.files[0]));
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("#dropOverlay").classList.add("on"); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dropOverlay").classList.remove("on"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; $("#dropOverlay").classList.remove("on");
  const f = e.dataTransfer.files[0];
  if (f && /\.zip$/i.test(f.name)) { updateFromZip(f); return; }
  if (f) { go("home"); setFile(f); }
});
$("#buildBtn").onclick = async () => {
  if (!S.file) return;
  try {
    await api("/api/build_text", { name: S.file.name, text: S.file.text, niche: $("#nicheSel").value });
    toast("Сборка началась", "ok");
    S.logNext = S.logNext; showJob(true);
  } catch (e) { toast(e.message, "err"); }
};
$("#stopBtn").onclick = () => api("/api/stop", {}).then(() => toast("Остановлю после текущего шага")).catch(() => {});
$("#logToggle").onclick = () => {
  const box = $("#logBox"); box.hidden = !box.hidden;
  $("#logToggle").textContent = box.hidden ? "Показать" : "Скрыть";
  box.scrollTop = box.scrollHeight;
};

// ---------- состояние работы ----------
const KIND = { build: "Сборка видео", selfcheck: "Проверка подключений", voices: "Озвучка примеров голосов" };
function showJob(on) { $("#jobCard").hidden = !on; $("#miniJob").hidden = !on; }
function renderChecks(list) {
  const html = list && list.length ? list.map((c) => `<div class="check ${c.ok ? "ok" : "bad"}"><span class="ic">${c.ok ? "✓" : "!"}</span>
      <div><div>${esc(c.name)}</div>${c.msg ? `<div class="msg">${esc(c.msg)}</div>` : ""}</div></div>`).join("")
    : '<p class="muted small">Нажмите «Проверить», чтобы убедиться, что ключи и сервисы работают.</p>';
  $("#checksHome").innerHTML = html; $("#checksKeys").innerHTML = list ? html : "";
}
async function poll() {
  try {
    const st = await api(`/api/state?since=${S.logNext}`);
    if (S.restarting) { location.reload(); return; }
    S.offline = 0;
    if (st.log.length) {
      const box = $("#logBox");
      const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.textContent += st.log.join("\n") + "\n";
      if (box.textContent.length > 300000) box.textContent = box.textContent.slice(-200000);
      if (atEnd) box.scrollTop = box.scrollHeight;
    }
    S.logNext = st.log_next;
    const j = st.job;
    S.jobRunning = j.running;
    if (j.running) {
      showJob(true);
      S.lastJobKind = j.kind;
      $("#jobTitle").textContent = KIND[j.kind] || "Работа";
      $("#miniJobText").textContent = j.frac != null ? `${KIND[j.kind] || "Работа"} · ${Math.round(j.frac * 100)}%` : (KIND[j.kind] || "Работа");
      $("#jobStage").textContent = j.stage || "Подготовка…";
      const bar = $("#jobBar"), mini = $("#miniBar");
      if (j.frac == null) { bar.classList.add("indet"); mini.classList.add("indet"); $("#jobPct").textContent = "…"; }
      else {
        bar.classList.remove("indet"); mini.classList.remove("indet");
        bar.style.width = mini.style.width = (j.frac * 100).toFixed(1) + "%";
      }
      S.timer = Object.assign(S.timer || {}, { elapsed: j.elapsed_sec || 0, eta: j.eta_sec, at: Date.now(), frac: j.frac });
      renderSteps(j.stage || "");
      tickTimer();
      $("#stopBtn").hidden = j.kind !== "build";
    } else {
      showJob(false);
    }
    if (st.selfcheck) renderChecks(st.selfcheck);
    if (S.doneId === null) S.doneId = j.done_id;
    else if (j.done_id !== S.doneId) { S.doneId = j.done_id; onJobDone(j); }
  } catch (e) {
    S.offline++;
    if (S.offline === 3 && !S.restarting) toast("Нет связи с программой. Если окно было закрыто — запустите DocuStudio снова.", "err", 6000);
  }
  setTimeout(poll, S.restarting ? 1000 : 800);
}
// таймер идёт каждую секунду плавно, между ответами программы
function fmt(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function human(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} с`;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h} ч ${m} мин` : `${m} мин ${String(s).padStart(2, "0")} с`;
}
// плавный таймер: цифры не прыгают, а мягко подтягиваются к новой оценке и идут каждую секунду
function tickTimer() {
  const t = S.timer; if (!t) return;
  const now = Date.now(), d = (now - t.at) / 1000;
  $("#jobElapsed").textContent = fmt(t.elapsed + d);
  if (t.frac != null) {
    t.pct = t.pct == null ? t.frac * 100 : t.pct + (t.frac * 100 - t.pct) * 0.15;
    $("#jobPct").textContent = Math.floor(t.pct) + "%";
  }
  if (t.eta == null) { $("#jobEta").textContent = "считаю…"; $("#jobDoneAt").textContent = "—"; return; }
  const target = Math.max(0, t.eta - d);
  const dt = t.last ? (now - t.last) / 1000 : 0; t.last = now;
  t.disp = t.disp == null ? target : Math.max(0, t.disp - dt);
  t.disp += (target - t.disp) * Math.min(1, dt / 6);          // ~6 секунд на сглаживание скачка
  $("#jobEta").textContent = "≈ " + human(t.disp);
  const at = new Date(now + t.disp * 1000);
  $("#jobDoneAt").textContent = `в ${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}
setInterval(() => { if (!$("#jobCard").hidden) tickTimer(); }, 250);

// этапы сборки — понятно, на каком шаге работа
const STEPS = [["Озвучка", /^Озвучка/], ["Подбор кадров", /^(Поиск|Подбор) кадров|^Кадры/], ["Монтаж кадров", /^Монтаж/],
               ["Склейка", /^Склейка/], ["Звук и файлы", /^(Сведение|Готово)/]];
function renderSteps(stage) {
  let cur = STEPS.findIndex(([, re]) => re.test(stage));
  if (cur < 0) cur = 0;
  const m = stage.match(/(\d+)\/(\d+)/);
  $("#jobStep").textContent = `Шаг ${cur + 1} из ${STEPS.length} — ${STEPS[cur][0]}${m ? `: ${m[1]} из ${m[2]}` : ""}`;
  $("#jobSteps").innerHTML = STEPS.map(([n], i) => `<span class="step ${i < cur ? "done" : i === cur ? "now" : ""}">${i < cur ? "✓ " : ""}${n}</span>`).join("");
}
$("#miniJob").addEventListener("click", () => { go("home"); setTimeout(() => $("#jobCard").scrollIntoView({ behavior: "smooth", block: "center" }), 60); });

function onJobDone(j) {
  if (j.kind === "build") {
    loadRecent();
    if (j.result && j.result.need_choices) {
      if (S.cfg.sound_done) chime();
      showChoose(j.result.project, j.result.sheets.length, j.result.lines);
      modal("Нужен выбор кадров", `<p>Кандидаты под все ${j.result.lines} строк сценария собраны на ${j.result.sheets.length} листах.</p>
        <p class="muted">Отправьте листы Claude в чат — он выберет кадры по смыслу. Его ответ вставьте сюда, и сборка продолжится.</p>`, [
        { text: "Позже" },
        { text: "Открыть листы", onClick: () => { api("/api/open", { what: "project", id: j.result.project }); return true; } },
        { text: "Вставить ответ Claude", cls: "primary", onClick: () => { setTimeout(() => pasteChoices(j.result.project), 50); } },
      ]);
      return;
    }
    if (j.error) {
      if (S.cfg.sound_done) chime();
      modal("Сборка остановилась", `<p>${esc(j.error)}</p><p class="muted small">Подробности — в журнале на главной и в файле error_log.txt. Пересборка продолжит с места остановки.</p>`);
      return;
    }
    if (S.cfg.sound_done) chime();
    const pid = j.result && j.result.project;
    modal("", `<div class="big-ok"><svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg></div>
      <h2>Видео готово</h2><p class="muted">Сборка заняла ${esc(j.elapsed)}. Рядом с видео — субтитры, описание для YouTube и 3 обложки.</p>`, [
      { text: "Закрыть" },
      { text: "Открыть папку", onClick: () => api("/api/open", { what: "project", id: pid }) },
      { text: "Смотреть", cls: "primary", onClick: async () => {
        const list = await api("/api/projects"); const p = list.find((x) => x.id === pid);
        if (p && p.ready) { setTimeout(() => openPlayer(p), 50); } } },
    ]);
  } else if (j.kind === "selfcheck") {
    const bad = (j.result && j.result.selfcheck || []).filter((c) => !c.ok && c.critical);
    toast(bad.length ? `Есть проблемы: ${bad.length}` : "Все подключения работают", bad.length ? "err" : "ok", 4000);
  }
}
const runCheck = () => api("/api/selfcheck", {}).then(() => toast("Проверяю подключения…")).catch((e) => toast(e.message, "err"));
$("#checkBtn").onclick = runCheck;
$("#checkBtn2").onclick = runCheck;

// ---------- выбор кадров через Claude ----------
function showChoose(pid, sheets, lines) {
  S.choose = pid;
  $("#chooseCard").hidden = !pid;
  if (pid) $("#chooseText").textContent = `Кандидаты собраны: ${lines || "все"} строк сценария, листов: ${sheets}.`;
}
$("#chooseOpen").onclick = () => S.choose && api("/api/open", { what: "project", id: S.choose }).catch((e) => toast(e.message, "err"));
$("#choosePaste").onclick = () => S.choose && pasteChoices(S.choose);
function pasteChoices(pid) {
  modal("Ответ Claude с выбором кадров", `<p class="muted small">Вставьте сюда ответ Claude целиком (Ctrl+V). Строки вида <code>12: 3 1</code> — номер строки сценария и номера кадров по порядку.</p>
    <textarea id="choiceText" placeholder="1: 2 5&#10;2: 1&#10;3: 4 7"></textarea>`, [
    { text: "Отмена" },
    { text: "Продолжить сборку", cls: "primary", onClick: async () => {
      try {
        const r = await api("/api/choices", { id: pid, text: $("#choiceText").value });
        if (r.missing && r.missing.length) toast(`Для строк ${r.missing.join(", ")} кадров нет — возьмутся соседние`, "", 6000);
        await api("/api/rebuild", { id: pid });
        showChoose(null); toast("Кадры приняты, сборка продолжается", "ok"); go("home");
      } catch (e) { toast(e.message, "err", 7000); return true; }
    } },
  ]);
  setTimeout(() => $("#choiceText") && $("#choiceText").focus(), 80);
}

// ---------- проекты ----------
const ICON = {
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  doc: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg>',
  redo: '<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>',
  text: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 10h16M4 14h10M4 18h7"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
};
const PLAY = '<svg viewBox="0 0 24 24"><path d="m8 5 11 7-11 7z" class="fill"/></svg>';
async function loadProjects() {
  try { S.projects = await api("/api/projects"); } catch (e) { S.projects = []; }
  renderProjects();
}
function thumbHtml(p) {
  return p.thumb ? `<img src="${fileUrl(`/api/project_file?id=${encodeURIComponent(p.id)}&name=${p.thumb}`)}" alt="" loading="lazy">` : "";
}
function renderProjects() {
  const q = ($("#projSearch").value || "").toLowerCase();
  const f = S.projFilter || "all";
  const list = S.projects.filter((p) => p.title.toLowerCase().includes(q) &&
    (f === "all" || (f === "ready" && p.ready) || (f === "work" && !p.ready)));
  const cnt = { all: S.projects.length, ready: S.projects.filter((p) => p.ready).length, work: S.projects.filter((p) => !p.ready).length };
  $$("#projFilter .chipbtn").forEach((b) => { b.classList.toggle("on", b.dataset.f === f); $(".n", b).textContent = cnt[b.dataset.f]; });
  const box = $("#projects");
  if (!list.length) {
    box.innerHTML = `<div class="empty"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="14" rx="2.5"/><path d="m10 8.5 5 2.5-5 2.5z" class="fill"/></svg>
      <div>${S.projects.length ? "В этом разделе пока пусто" : "Проектов пока нет — соберите первое видео на главной"}</div></div>`;
    return;
  }
  box.innerHTML = list.map((p, i) => `
    <div class="pcard" style="animation-delay:${Math.min(i, 12) * 35}ms" data-id="${esc(p.id)}">
      <div class="pthumb" data-act="video">${thumbHtml(p)}
        ${p.ready ? `<div class="play"><span>${PLAY}</span></div>` : ""}
        ${p.duration ? `<span class="badge">${esc(p.duration)}</span>` : ""}</div>
      <div class="pbody">
        <div class="ptitle">${esc(p.title)}</div>
        <div class="pmeta"><span class="chip ${p.ready ? "ok" : (p.building || p.need_choices) ? "work" : "wait"}">${p.ready ? "Готово" : p.building ? `В работе · ${p.progress}%` : p.need_choices ? "Ждёт выбора кадров" : "Не собрано"}</span>
          <span>${esc(p.created)}</span>${p.size_mb ? `<span>· ${p.size_mb} МБ</span>` : ""}</div>
        <div class="pactions">
          ${p.building ? '<button class="btn small" disabled>Собирается…</button>' : p.need_choices ? '<button class="btn small primary" data-act="choose">Выбор кадров</button>'
            : `<button class="btn small ${p.ready ? "primary" : ""}" data-act="${p.ready ? "video" : "rebuild"}">${p.ready ? "Смотреть" : "Собрать"}</button>`}
          <span class="spacer"></span>
          <button class="ibtn" data-act="desc" title="Описание для YouTube" ${p.has_description ? "" : "disabled"}>${ICON.text}</button>
          <button class="ibtn" data-act="project" title="Открыть папку">${ICON.folder}</button>
          <button class="ibtn" data-act="script" title="Открыть сценарий">${ICON.doc}</button>
          ${p.ready ? `<button class="ibtn" data-act="rebuild" title="Пересобрать">${ICON.redo}</button>` : ""}
          ${p.building ? "" : `<button class="ibtn danger" data-act="delete" title="Удалить">${ICON.trash}</button>`}
        </div>
      </div>
    </div>`).join("");
}
$("#projSearch").addEventListener("input", renderProjects);
$("#projFilter").addEventListener("click", (e) => { const b = e.target.closest(".chipbtn"); if (b) { S.projFilter = b.dataset.f; renderProjects(); } });
// просмотр готового видео прямо в программе
function openPlayer(p) {
  const src = fileUrl(`/api/project_file?id=${encodeURIComponent(p.id)}&name=${encodeURIComponent(p.video)}`);
  modal(p.title, `<video class="player" controls autoplay preload="metadata" src="${src}"></video>`, [
    { text: "Закрыть" },
    { text: "Папка", onClick: () => { api("/api/open", { what: "project", id: p.id }); return true; } },
    { text: "Открыть в проигрывателе", cls: "primary", onClick: () => api("/api/open", { what: "video", id: p.id }) },
  ]);
  $("#modal").classList.add("wide");
}
// пока идёт сборка — карточки обновляются сами
setInterval(() => { if (S.jobRunning && $("#page-projects").classList.contains("active")) loadProjects(); }, 3000);
$("#projects").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]"); if (!b || b.disabled) return;
  const id = b.closest(".pcard").dataset.id, act = b.dataset.act;
  const p = S.projects.find((x) => x.id === id);
  try {
    if (act === "video" && p && !p.ready) return;
    if (act === "video") openPlayer(p);
    else if (act === "choose") { showChoose(id, (p.sheets || []).length); pasteChoices(id); }
    else if (["project", "script"].includes(act)) await api("/api/open", { what: act, id });
    else if (act === "rebuild") {
      await api("/api/rebuild", { id }); toast("Сборка началась", "ok"); go("home");
    } else if (act === "desc") {
      const r = await api("/api/text_file", { id, name: "description.txt" });
      modal("Описание для YouTube", `<textarea id="descText">${esc(r.text)}</textarea>`, [
        { text: "Закрыть" },
        { text: "Скопировать", cls: "primary", onClick: async () => {
          await navigator.clipboard.writeText($("#descText").value).catch(() => { $("#descText").select(); document.execCommand("copy"); });
          toast("Описание скопировано", "ok"); return true; } },
      ]);
    } else if (act === "delete") {
      modal("Удалить проект?", `<p>«${esc(p ? p.title : id)}» — видео, озвучка и скачанные кадры будут удалены без возможности восстановить.</p>`, [
        { text: "Отмена" },
        { text: "Удалить", cls: "primary", onClick: async () => { await api("/api/delete_project", { id }); toast("Проект удалён"); loadProjects(); loadRecent(); } },
      ]);
    }
  } catch (err) { toast(err.message, "err"); }
});
async function loadRecent() {
  let list = [];
  try { list = await api("/api/projects"); } catch (e) {}
  S.projects = list;
  const pend = list.find((p) => p.need_choices && !p.building);
  if (pend && !S.jobRunning) showChoose(pend.id, (pend.sheets || []).length);
  else if (!pend) showChoose(null);
  const box = $("#recent");
  if (!list.length) { box.innerHTML = '<p class="muted small">Здесь появятся ваши видео.</p>'; return; }
  box.innerHTML = list.slice(0, 4).map((p) => `<div class="ritem" data-id="${esc(p.id)}">
      ${p.thumb ? thumbHtml(p) : '<div class="ph"></div>'}
      <div><div class="t">${esc(p.title)}</div><div class="s">${p.ready ? "Готово" : p.building ? "В работе" : "Не собрано"} · ${esc(p.created)}${p.duration ? " · " + esc(p.duration) : ""}</div></div></div>`).join("");
}
$("#recent").addEventListener("click", (e) => {
  const r = e.target.closest(".ritem"); if (!r) return;
  const p = S.projects.find((x) => x.id === r.dataset.id);
  if (p && p.ready) openPlayer(p);
  else go("projects");
});

// ---------- голоса ----------
const player = new Audio();
let playingBtn = null;
const STOPI = '<svg viewBox="0 0 24 24"><rect x="7" y="6" width="3.5" height="12" rx="1" class="fill"/><rect x="13.5" y="6" width="3.5" height="12" rx="1" class="fill"/></svg>';
let playingKey = null, lastPlayStart = 0;
function stopAudio() {
  player.pause();
  if (playingBtn) playingBtn.innerHTML = PLAY;
  playingBtn = null; playingKey = null;
}
// играет всегда только один звук: новый останавливает предыдущий
function playUrl(url, btn, key) {
  if (playingBtn) playingBtn.innerHTML = PLAY;
  player.pause();
  player.src = url;
  player.play().catch(() => toast("Не удалось воспроизвести", "err"));
  btn.innerHTML = STOPI; playingBtn = btn; playingKey = key || url; lastPlayStart = Date.now();
}
player.addEventListener("ended", () => { if (playingBtn) playingBtn.innerHTML = PLAY; playingBtn = null; playingKey = null; });
document.addEventListener("visibilitychange", () => { if (document.hidden) stopAudio(); });
const PROV = { elevenlabs: "ElevenLabs", lumean: "Lumean · ElevenLabs" };
const VS = { provider: null, list: [], defText: "", filter: "doc", role: {} };
function sampleText() {
  let t = "";
  try { t = localStorage.getItem("ds-sample") || ""; } catch (e) {}
  return t || VS.defText || DEF_TEXT;
}
// прослушать: если примера нет — программа озвучит его (1–3 секунды) и сразу включит
async function playVoice(provider, value, btn) {
  const key = provider + "|" + value;
  if (!player.paused && playingKey === key) { stopAudio(); return; }      // повторное нажатие — стоп
  if (!player.paused && playingBtn === btn) stopAudio();
  const clickedAt = Date.now();
  btn.classList.add("loading"); btn.disabled = true;
  try {
    const r = await api("/api/voice_sample", { provider, value, text: sampleText() });
    if (!r.ok) { toast(r.error, "err", 8000); return; }
    // пока этот пример готовился, уже заиграл другой — этот не включаем, он готов по нажатию
    if (lastPlayStart > clickedAt || !document.body.contains(btn)) return;
    playUrl(fileUrl(`/api/voice_audio?provider=${provider}&value=${encodeURIComponent(value)}&h=${r.h}`), btn, key);
    const row = btn.closest(".vrow");
    if (row) $$(`.role[data-value="${CSS.escape(value)}"]`, row).forEach((x) => { if (!$(".d", x)) x.insertAdjacentHTML("beforeend", '<span class="d"></span>'); });
  } catch (e) { toast(e.message, "err", 8000); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}
// ---------- голоса ElevenLabs ----------
const DEF_TEXT = "В декабре тысяча девятисотого года с маяка на крошечном острове исчезли три смотрителя. Их так и не нашли.";
const EL = { voices: [], current: "", hasKey: false };
function elRange(el, val, label, digits = 2) { el.value = val; rangeFill(el); $(label).textContent = parseFloat(el.value).toFixed(digits) + (label === "#elSpeedVal" ? "×" : ""); }
async function loadVoices() {
  $("#sampleText").value = sampleText();
  let d = { voices: [], current: "", has_key: false };
  try { d = await api("/api/eleven", {}); } catch (e) { toast(e.message, "err"); }
  EL.voices = d.voices; EL.current = d.current; EL.hasKey = d.has_key; EL.provider = d.provider;
  const lum = d.provider === "lumean";
  $("#libCard").hidden = !lum || !d.has_key; $("#elTipsCard").hidden = lum;
  $("#elImportBtn").hidden = lum; $("#elLibBtn").hidden = lum;
  $("#lumAddHint").hidden = !lum; $("#elAddHint").hidden = lum;
  $("#accTitle").textContent = lum ? "Аккаунт Lumean" : "Аккаунт ElevenLabs";
  const box = $("#elVoices");
  if (lum && d.has_key && !$("#libList").children.length) libSearch(0);
  if (!d.has_key) {
    box.innerHTML = `<div class="empty" style="padding:30px"><div>Сначала вставьте ключ ${lum ? "Lumean" : "ElevenLabs"}:<br><b>Параметры → Сервисы и ключи → Ключ ${lum ? "Lumean" : "ElevenLabs"}</b></div><button class="btn primary mt" data-goto="settings">Открыть параметры</button></div>`;
  } else if (!EL.voices.length) {
    box.innerHTML = `<div class="empty" style="padding:30px"><div>Голосов пока нет. ${lum ? "Найдите голос в библиотеке справа и нажмите «Добавить»." : "Добавьте голос по ID справа или нажмите «Загрузить все мои голоса»."}</div></div>`;
  } else {
    box.innerHTML = EL.voices.map((v) => {
      const cur = v.id === EL.current;
      return `<div class="vrow ${cur ? "sel" : ""}" data-id="${esc(v.id)}">
        <button class="playbtn" data-preview ${v.preview ? "" : "disabled"} title="Пример с сайта ElevenLabs">${PLAY}</button>
        <div class="name">${esc(v.name)}<small>${esc(v.desc || v.category || "")}</small></div>
        <button class="btn small ghost" data-mine title="Озвучить вашу фразу этим голосом (тратит ~${sampleText().length} символов)">${PLAY}Моя фраза</button>
        ${cur ? '<span class="chip ok">Выбран</span>' : '<button class="btn small ghost" data-pick>Выбрать</button>'}
        <button class="ibtn danger" data-del title="Убрать из списка">${ICON.trash}</button></div>`;
    }).join("");
  }
  elRange($("#elSpeed"), S.cfg.eleven_speed ?? 0.9, "#elSpeedVal");
  elRange($("#elStab"), S.cfg.eleven_stability ?? 0.45, "#elStabVal");
  if (d.has_key) loadUsage();
}
async function loadUsage() {
  const r = await api("/api/eleven_usage", {}).catch((e) => ({ ok: false, error: e.message }));
  if (!r.ok) { $("#elUsage").innerHTML = `<span style="color:var(--danger)">${esc(r.error)}</span>`; return; }
  const left = Math.max(0, (r.limit || 0) - (r.used || 0));
  const unit = r.unit || "символов";
  $("#elUsage").innerHTML = `Тариф: <b>${esc(r.tier)}</b> · осталось ${unit}: <b>${left.toLocaleString("ru-RU")}</b> из ${(r.limit || 0).toLocaleString("ru-RU")}<br>Это примерно <b>${Math.floor(left / 1100)} мин</b> озвучки (1 ${unit === "токенов" ? "токен" : "символ"} ≈ 1 буква текста; 3-минутное видео ≈ 3 300).`;
}
$("#elVoices").addEventListener("click", async (e) => {
  const row = e.target.closest(".vrow"); if (!row) return;
  const id = row.dataset.id, v = EL.voices.find((x) => x.id === id);
  const pv = e.target.closest("[data-preview]");
  if (pv && v && v.preview) {
    if (playingBtn === pv && !player.paused) { stopAudio(); return; }
    playUrl(v.preview, pv, "pv|" + id); return;
  }
  const mine = e.target.closest("[data-mine]");
  if (mine) { playVoice("eleven", id, mine); return; }
  if (e.target.closest("[data-pick]")) {
    Object.assign(S.cfg, { elevenlabs_voice_id: id }); EL.current = id;
    await api("/api/save", { values: { elevenlabs_voice_id: id } }).catch((err) => toast(err.message, "err"));
    toast(`Голос «${v ? v.name : id}» выбран для озвучки видео`, "ok"); loadVoices(); renderVoiceCard(); return;
  }
  if (e.target.closest("[data-del]")) {
    await api("/api/eleven_remove", { id }).catch((err) => toast(err.message, "err")); loadVoices(); renderVoiceCard();
  }
});
$("#elAddBtn").onclick = async () => {
  const val = $("#elAddId").value.trim(); if (!val) { $("#elAddId").focus(); return; }
  const b = $("#elAddBtn"); b.disabled = true;
  try {
    const r = await api("/api/eleven_add", { voice: val });
    toast(`Голос «${r.voice.name}» добавлен`, "ok"); $("#elAddId").value = "";
    const d = await api("/api/init"); S.cfg = d.cfg; loadVoices(); renderVoiceCard();
  } catch (e) { toast(e.message, "err", 8000); } finally { b.disabled = false; }
};
$("#elAddId").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#elAddBtn").click(); });
$("#elImportBtn").onclick = async () => {
  try { const r = await api("/api/eleven_import", {}); toast(r.added ? `Добавлено голосов: ${r.added}` : "Новых голосов в аккаунте нет", "ok"); loadVoices(); }
  catch (e) { toast(e.message, "err", 8000); }
};
// библиотека голосов ElevenLabs через Lumean
const LIB = { page: 0, items: [] };
async function libSearch(page) {
  LIB.page = page;
  const box = $("#libList");
  if (!page) box.innerHTML = '<p class="muted small">Ищу голоса…</p>';
  const r = await api("/api/lumean_library", { search: $("#libSearch").value.trim(), gender: $("#libGender").value,
    age: $("#libAge").value, use_cases: $("#libUse").value, language: "ru", page }).catch((e) => ({ ok: false, error: e.message }));
  if (!r.ok) { box.innerHTML = `<p class="small" style="color:var(--danger)">${esc(r.error)}</p>`; return; }
  LIB.items = page ? LIB.items.concat(r.voices) : r.voices;
  const have = new Set(EL.voices.map((v) => v.id));
  box.innerHTML = LIB.items.length ? LIB.items.map((v, i) => `<div class="vrow" data-i="${i}">
      <button class="playbtn" data-lprev ${v.preview ? "" : "disabled"} title="Пример с сайта">${PLAY}</button>
      <div class="name">${esc(v.name)}<small>${esc(v.desc)}</small></div>
      ${have.has(v.id) ? '<span class="chip ok">Добавлен</span>' : '<button class="btn small ghost" data-ladd>Добавить</button>'}</div>`).join("")
    : '<p class="muted small">Ничего не нашлось — измените поиск или фильтры.</p>';
  $("#libMore").hidden = !r.has_more;
}
$("#libFind").onclick = () => libSearch(0);
$("#libSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") libSearch(0); });
["#libGender", "#libAge", "#libUse"].forEach((s) => $(s).addEventListener("change", () => libSearch(0)));
$("#libMore").onclick = () => libSearch(LIB.page + 1);
$("#libList").addEventListener("click", async (e) => {
  const row = e.target.closest(".vrow"); if (!row) return;
  const v = LIB.items[+row.dataset.i];
  const pv = e.target.closest("[data-lprev]");
  if (pv) { if (playingBtn === pv && !player.paused) { stopAudio(); return; } playUrl(v.preview, pv, "lib|" + v.id); return; }
  if (e.target.closest("[data-ladd]")) {
    try {
      await api("/api/eleven_add", { voice_obj: { id: v.id, name: v.name, desc: v.desc, preview: v.preview } });
      toast(`Голос «${v.name}» добавлен в «Мои голоса»`, "ok");
      const d = await api("/api/init"); S.cfg = d.cfg; await loadVoices(); libSearch(0); renderVoiceCard();
    } catch (err) { toast(err.message, "err"); }
  }
});
$("#ccLoginBtn").onclick = () => api("/api/claude_login", {}).then((r) => toast(r.message || "Открываю Claude Code…", r.warn ? "err" : undefined, 9000)).catch((e) => toast(e.message, "err", 9000));
$("#elLibBtn").onclick = () => api("/api/open_url", { url: "https://elevenlabs.io/app/voice-library" }).catch((e) => toast(e.message, "err"));
$("#elUsageBtn").onclick = loadUsage;
$("#elSpeed").addEventListener("input", (e) => elRange(e.target, e.target.value, "#elSpeedVal"));
$("#elSpeed").addEventListener("change", (e) => save({ eleven_speed: parseFloat(e.target.value) }));
$("#elSpeedReset").onclick = () => { elRange($("#elSpeed"), 0.9, "#elSpeedVal"); save({ eleven_speed: 0.9 }); };
$("#elStab").addEventListener("input", (e) => elRange(e.target, e.target.value, "#elStabVal"));
$("#elStab").addEventListener("change", (e) => save({ eleven_stability: parseFloat(e.target.value) }));
$("#sampleText").addEventListener("change", (e) => { try { localStorage.setItem("ds-sample", e.target.value.trim()); } catch (err) {} });
$("#sampleReset").onclick = () => { try { localStorage.removeItem("ds-sample"); } catch (e) {} $("#sampleText").value = sampleText(); };

// карточка «Голос диктора» на главной — меняется вместе с настройками
async function renderVoiceCard() {
  const prov = S.cfg.tts_provider === "elevenlabs" ? "elevenlabs" : "lumean";
  const btn = $("#homePlay");
  btn.innerHTML = PLAY;
  $("#homeProv").textContent = PROV[prov] || prov;
  if (prov === "elevenlabs" || prov === "lumean") {
    let d = { voices: [], current: "" };
    try { d = await api("/api/eleven", {}); } catch (e) {}
    const v = d.voices.find((x) => x.id === d.current);
    $("#homeVoice").textContent = v ? v.name : (d.current ? d.current : "голос не выбран — вкладка «Голоса»");
    btn.disabled = !d.current;
    btn.onclick = d.current ? () => playVoice("eleven", d.current, btn) : null;
    return;
  }
  $("#homeVoice").textContent = "голос не выбран"; btn.disabled = true; btn.onclick = null;
}

// ---------- музыка ----------
async function loadMusic() {
  let list = [];
  try { list = await api("/api/music"); } catch (e) {}
  const v = $("#musicVol"); v.value = S.cfg.music_volume ?? 0.12; rangeFill(v); $("#volVal").textContent = Math.round(v.value * 100) + "%";
  $("#music").innerHTML = list.length ? list.map((m) => `<div class="vrow" data-rel="${esc(m.rel)}">
      <button class="playbtn" data-play>${PLAY}</button>
      <div class="name">${esc(m.name)}<small>${m.folder ? "ниша: " + esc(m.folder) + " · " : ""}${m.size_mb} МБ</small></div></div>`).join("")
    : '<p class="muted small mt">В папке music пока нет треков. Видео соберётся и без музыки.</p>';
}
$("#music").addEventListener("click", (e) => {
  const b = e.target.closest("[data-play]"); if (!b) return;
  if (playingBtn === b && !player.paused) { stopAudio(); return; }
  playUrl(fileUrl(`/api/music_audio?rel=${encodeURIComponent(b.closest(".vrow").dataset.rel)}`), b);
});
$("#musicVol").addEventListener("input", (e) => { rangeFill(e.target); $("#volVal").textContent = Math.round(e.target.value * 100) + "%"; });
$("#musicVol").addEventListener("change", (e) => save({ music_volume: parseFloat(e.target.value) }));

// ---------- обновления и ярлык ----------
async function updateFromZip(f) {
  try {
    toast("Устанавливаю обновление…", "", 5000);
    const r = await api("/api/update_zip", await f.arrayBuffer(), true);
    toast(`Обновлено до версии ${r.version}. Перезапуск…`, "ok", 5000);
    S.restarting = true;
  } catch (e) { toast("Обновление не установлено: " + e.message, "err", 7000); }
}
$("#updFile").addEventListener("change", (e) => { if (e.target.files[0]) updateFromZip(e.target.files[0]); e.target.value = ""; });
// обновление одной кнопкой: проверка на GitHub → установка → перезапуск
async function installOnline() {
  $("#updStatus").textContent = "Скачиваю и устанавливаю…";
  $("#updSideText").textContent = "Устанавливаю…";
  try {
    const u = await api("/api/update_online", {});
    toast(`Обновлено до версии ${u.version}. Перезапуск…`, "ok", 6000);
    S.restarting = true;
  } catch (e) {
    toast("Не обновилось: " + e.message, "err", 8000);
    $("#updStatus").textContent = "Не обновилось: " + e.message;
    $("#updSideText").textContent = "Обновить программу";
  }
}
async function checkUpdate(manual) {
  if (S.jobRunning) { if (manual) toast("Дождитесь окончания сборки, потом обновляйте", "err"); return; }
  if (manual) { $("#updStatus").textContent = "Проверяю…"; $("#updSideText").textContent = "Проверяю…"; }
  let r;
  try { r = await api("/api/check_update", {}); }
  catch (e) { r = { ok: false, error: e.message }; }
  $("#updSideText").textContent = "Обновить программу";
  if (!r.ok) {
    if (manual) { toast("Не удалось проверить: " + r.error, "err", 6000); $("#updStatus").textContent = "Не удалось проверить: " + r.error; }
    return;
  }
  $("#updDot").hidden = !r.newer;
  if (!r.newer) {
    $("#updStatus").textContent = `У вас последняя версия (${r.current})`;
    if (manual) toast(`У вас последняя версия (${r.current})`, "ok");
    return;
  }
  $("#updStatus").textContent = `Доступна версия ${r.latest}`;
  $("#updSideText").textContent = "Доступно обновление";
  modal("Доступно обновление", `<p>Установлена версия ${esc(r.current)}. Доступна <b>${esc(r.latest)}</b>.</p>
    <p class="muted small">Ключи, проекты, футажи и музыка сохранятся. Программа перезапустится сама.</p>`, [
    { text: "Позже" },
    { text: "Обновить", cls: "primary", onClick: installOnline },
  ]);
}
$("#updNow").onclick = () => checkUpdate(true);
$("#updSide").onclick = () => checkUpdate(true);
$("#updCheck").onclick = () => checkUpdate(true);
$("#shortcutBtn").onclick = () => api("/api/shortcut", {}).then((r) => toast(r.ok ? "Ярлык DocuStudio на рабочем столе готов" : "Не удалось создать ярлык", r.ok ? "ok" : "err")).catch((e) => toast(e.message, "err"));

// при запуске программа сама проверяет, есть ли новая версия на GitHub
function autoUpdateCheck() { if (S.cfg.update_repo) checkUpdate(false); }

// окно закрывается — программа завершится сама, если ничего не собирается
window.addEventListener("pagehide", () => { try { navigator.sendBeacon(`/api/bye?t=${encodeURIComponent(TOKEN)}`); } catch (e) {} });

// ---------- старт ----------
async function loadInit() {
  const d = await api("/api/init");
  S.cfg = d.cfg; S.niches = d.niches; S.voices = d.voices;
  $("#ver").textContent = "версия " + d.version;
  $("#verText").textContent = d.version;
  $("#aboutVer").textContent = "Версия " + d.version;
  const opts = d.niches.map((n) => `<option value="${esc(n.id)}">${esc(n.name)}</option>`).join("");
  $("#nicheSel").innerHTML = opts; $("#defNiche").innerHTML = opts;
  $("#nicheSel").value = d.cfg.default_niche || (d.niches[0] && d.niches[0].id);
  applyTheme(d.cfg.theme || "light");
  bindSettings();
  renderKeys();
}
(async function start() {
  try { await loadInit(); }
  catch (e) { toast("Программа не отвечает. Закройте окно и запустите DocuStudio снова.", "err", 10000); }
  loadRecent();
  renderVoiceCard();
  poll();
  setTimeout(autoUpdateCheck, 2500);
  if (!(S.cfg.lumean_key || S.cfg.elevenlabs_key) || !S.cfg.pixabay_key) {
    setTimeout(() => toast("Сначала вставьте ключи: Параметры → Сервисы и ключи", "", 6000), 600);
  }
})();
