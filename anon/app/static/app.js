/* Anonymisierung – local review app (no external libraries, works offline). */
"use strict";
(() => {
  const TOKEN = document.querySelector('meta[name="anon-token"]').content;
  const view = document.getElementById("view");
  const pop = document.getElementById("popover");
  const seltool = document.getElementById("seltool");
  const CAT = { party: "Person/Firma", representative: "Vertreter", place: "Ort", plot: "Grundstück", id: "Datum/ID", manual: "Manuell" };
  const CAT_ORDER = ["party", "representative", "place", "plot", "id", "manual"];
  const ROLE = { party: "Partei", affected: "betroffen", expert_or_doctor: "Arzt/Gutachter/Zeuge", representative: "Vertreter" };
  const KIND = { person: "Person", org: "Firma", representative: "Vertreter", place: "Ort", address: "Adresse", plot: "Grundstück", birthdate: "Geburtsdatum", id: "Kennung" };
  const S = { status: null, caseId: null, data: null, mode: "markup", hidden: new Set(), tab: "questions", focus: null, timers: [], sel: null, showDecided: false };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const ICON_LOCK = '<svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true" style="vertical-align:-2px"><path fill="currentColor" d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5zm-3 8V7a3 3 0 0 1 6 0v3z"/></svg>';

  // ---------------------------------------------------------------- utilities
  async function api(path, { method = "GET", body, form } = {}) {
    const opts = { method, headers: { "X-Anon-Token": TOKEN } };
    if (form) opts.body = form;
    else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    const ct = r.headers.get("content-type") || "";
    if (!r.ok) {
      let msg = `Fehler ${r.status}`;
      if (ct.includes("json")) {
        const j = await r.json();
        msg = typeof j.detail === "string" ? j.detail : (j.problems || []).join("\n") || msg;
      }
      throw new Error(msg);
    }
    return ct.includes("json") ? r.json() : r;
  }

  function filenameFrom(r, fallback) {
    const cd = r.headers.get("content-disposition") || "";
    let m = /filename\*=(?:UTF-8|utf-8)''([^;]+)/.exec(cd);
    if (m) return decodeURIComponent(m[1]);
    m = /filename="?([^";]+)"?/.exec(cd);
    return m ? m[1] : fallback;
  }

  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 3000);
  }

  async function download(path, fallback) {
    try {
      const r = await api(path);
      saveBlob(await r.blob(), filenameFrom(r, fallback));
    } catch (e) { toast(e.message, "err"); }
  }

  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), kind === "err" ? 7000 : 3500);
  }

  function modal({ title, body, actions = [], wide = false, onClose }) {
    closePopover(); hideSeltool();
    const el = document.createElement("div");
    el.className = "backdrop";
    el.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-h"><h3>${esc(title)}</h3><button class="menu-btn x" aria-label="Schliessen">✕</button></div>
      <div class="modal-b">${body}</div>${actions.length ? '<div class="modal-f"></div>' : ""}</div>`;
    const close = () => { el.remove(); document.removeEventListener("keydown", onKey, true); if (onClose) onClose(); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    el.addEventListener("mousedown", (e) => { if (e.target === el) close(); });
    $(".x", el).onclick = close;
    const foot = $(".modal-f", el);
    for (const a of actions) {
      const b = document.createElement("button");
      b.className = "btn " + (a.kind || "");
      b.textContent = a.label;
      b.onclick = () => a.onClick(close, el, b);
      foot.appendChild(b);
    }
    $("#modal-root").appendChild(el);
    setTimeout(() => { const f = $("[autofocus], input:not([type=checkbox]):not([type=radio]), textarea", el) || $(".btn.primary", el); if (f) f.focus(); }, 30);
    return { el, close };
  }

  function confirmBox(title, text, okLabel, kind = "danger") {
    return new Promise((resolve) => {
      let done = false;
      modal({
        title, body: `<p>${text}</p>`,
        actions: [
          { label: "Abbrechen", onClick: (close) => { close(); } },
          { label: okLabel, kind, onClick: (close) => { done = true; close(); resolve(true); } },
        ],
        onClose: () => { if (!done) resolve(false); },
      });
    });
  }

  const clearTimers = () => { S.timers.forEach(clearTimeout); S.timers = []; };
  const later = (fn, ms) => S.timers.push(setTimeout(fn, ms));
  const fmtDate = (iso) => { try { return new Date(iso).toLocaleString("de-CH", { dateStyle: "medium", timeStyle: "short" }); } catch { return iso; } };

  // ---------------------------------------------------------------- status
  async function loadStatus() {
    try { S.status = await api("/api/status"); } catch { return; }
    const st = S.status, llm = st.llm;
    const pills = [`<span class="pill ${st.ner ? "on" : ""}" title="${st.ner ? "Namenserkennung (NER) aktiv" : "Kein NER-Modell: Regeln und Verzeichnisse"}"><span class="dot"></span>NER ${st.ner ? "aktiv" : "aus"}</span>`];
    if (!llm.enabled) pills.push('<span class="pill" title="In den Einstellungen aktivieren"><span class="dot"></span>Sprachmodell aus</span>');
    else if (llm.reachable && llm.model_found !== false) pills.push(`<span class="pill on" title="${esc(llm.base_url)}"><span class="dot"></span>${esc(llm.model)}</span>`);
    else if (llm.reachable) pills.push(`<span class="pill err" title="Modell nicht installiert"><span class="dot"></span>${esc(llm.model)} fehlt</span>`);
    else pills.push(`<span class="pill err" title="${esc(llm.error || "")}"><span class="dot"></span>${esc(llm.model)} nicht erreichbar</span>`);
    $("#engines").innerHTML = pills.join("");
    const box = $("#use-llm");
    if (box) syncLlmBox();
  }

  function llmUsable() { const l = S.status && S.status.llm; return !!(l && l.enabled && l.reachable && l.model_found !== false); }
  function syncLlmBox() {
    const box = $("#use-llm"), label = $("#use-llm-label");
    if (!box) return;
    const l = S.status && S.status.llm;
    box.disabled = !llmUsable();
    box.checked = llmUsable() && box.dataset.touched !== "1" ? true : box.checked && llmUsable();
    if (!l || !l.enabled) label.innerHTML = 'Lokales Sprachmodell <span class="muted">(aus – <a href="#" id="open-settings">einschalten</a>)</span>';
    else if (!llmUsable()) label.innerHTML = `Lokales Sprachmodell <span class="muted">(${esc(l.model)} nicht erreichbar)</span>`;
    else label.innerHTML = `Lokales Sprachmodell verwenden <span class="muted">(${esc(l.model)}, Hinweise nach Richtlinie 2.4)</span>`;
    const link = $("#open-settings");
    if (link) link.onclick = (e) => { e.preventDefault(); openSettings(); };
  }

  // ---------------------------------------------------------------- routing
  async function route() {
    clearTimers(); closePopover(); hideSeltool();
    const m = /^#\/case\/([0-9a-f]{32})/.exec(location.hash);
    if (m) return openCase(m[1]);
    S.caseId = null; S.data = null;
    return renderHome();
  }

  // ---------------------------------------------------------------- home
  function statusBadge(c) {
    if (c.status === "queued") return '<span class="badge run">Wartet</span>';
    if (c.status === "running") return `<span class="badge run">Analyse ${Math.round((c.progress || 0) * 100)} %</span>`;
    if (c.status === "error") return `<span class="badge err" title="${esc(c.error || "")}">Fehler</span>`;
    if (c.status === "exported") return '<span class="badge ok">Exportiert</span>';
    if (c.open) return `<span class="badge warn">${c.open} offene Frage${c.open === 1 ? "" : "n"}</span>`;
    return '<span class="badge">Bereit zur Prüfung</span>';
  }

  async function renderHome() {
    view.innerHTML = `
      <div class="home">
        <section class="card">
          <div class="drop" id="drop" tabindex="0" role="button" aria-label="Word-Datei auswählen">
            <svg width="46" height="46" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zm4 18H6V4h7v5h5zM8 15h3v4h2v-4h3l-4-4z"/></svg>
            <h1>Entscheidung hierher ziehen</h1>
            <p>oder klicken, um eine Word-Datei (.docx) auszuwählen</p>
            <input type="file" id="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" hidden>
          </div>
          <div class="upload-opts">
            <label class="switch"><input type="checkbox" id="use-llm"><span id="use-llm-label">Lokales Sprachmodell</span></label>
            <span class="tiny">${ICON_LOCK} Die Datei bleibt auf diesem Computer.</span>
          </div>
        </section>
        <section class="card">
          <div class="card-h"><h2>Fälle</h2><button class="btn sm ghost" id="word-btn" title="Prüfkopie, die in Word bearbeitet wurde">In Word geprüft? Fertigstellen …</button></div>
          <div id="cases"><div class="empty-state">Wird geladen …</div></div>
        </section>
      </div>`;
    const drop = $("#drop"), file = $("#file");
    drop.onclick = () => file.click();
    drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } };
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
    file.onchange = () => { if (file.files[0]) upload(file.files[0]); };
    $("#use-llm").onchange = (e) => { e.target.dataset.touched = "1"; };
    $("#word-btn").onclick = openWordFinalize;
    syncLlmBox();
    refreshCases();
  }

  async function refreshCases() {
    const box = $("#cases");
    if (!box) return;
    let cases;
    try { cases = await api("/api/cases"); } catch (e) { box.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; return; }
    if (!cases.length) {
      box.innerHTML = '<div class="empty-state">Noch keine Fälle. Laden Sie oben eine Entscheidung hoch.</div>';
    } else {
      box.innerHTML = `<table class="cases"><thead><tr><th>Dokument</th><th>Hochgeladen</th><th>Status</th><th></th></tr></thead><tbody>
        ${cases.map((c) => `<tr class="clickable" data-id="${c.id}"><td class="name">${esc(c.name)}</td><td class="muted">${fmtDate(c.created)}</td>
          <td>${statusBadge(c)}</td><td><div class="row-actions"><button class="btn sm" data-open="${c.id}">Öffnen</button>
          <button class="btn sm danger" data-del="${c.id}" title="Fall und alle Daten löschen">Löschen</button></div></td></tr>`).join("")}
        </tbody></table>`;
      box.onclick = async (e) => {
        const del = e.target.closest("[data-del]");
        if (del) {
          e.stopPropagation();
          if (await confirmBox("Fall löschen?", "Original, Entscheidungen und Exporte dieses Falls werden von diesem Computer gelöscht.", "Löschen")) {
            try { await api(`/api/cases/${del.dataset.del}`, { method: "DELETE" }); toast("Fall gelöscht", "ok"); refreshCases(); } catch (err) { toast(err.message, "err"); }
          }
          return;
        }
        const row = e.target.closest("[data-id]");
        if (row) location.hash = `#/case/${row.dataset.id}`;
      };
    }
    if (cases.some((c) => c.status === "queued" || c.status === "running")) later(refreshCases, 2000);
  }

  async function upload(file) {
    if (!/\.docx$/i.test(file.name)) { toast("Bitte eine Word-Datei (.docx) wählen.", "err"); return; }
    const form = new FormData();
    form.append("file", file);
    form.append("use_llm", $("#use-llm") && $("#use-llm").checked ? "true" : "false");
    try {
      const c = await api("/api/cases", { method: "POST", form });
      location.hash = `#/case/${c.id}`;
    } catch (e) { toast(e.message, "err"); }
  }

  // ---------------------------------------------------------------- progress
  async function openCase(id) {
    S.caseId = id;
    let s;
    try { s = await api(`/api/cases/${id}`); } catch (e) { toast(e.message, "err"); location.hash = "#/"; return; }
    if (S.caseId !== id) return;
    if (s.status === "ready" || s.status === "exported") return loadReview(id, false);
    if (s.status === "error") {
      view.innerHTML = `<div class="card progress-card"><div class="result-icon err">!</div><h2>${esc(s.name)}</h2>
        <p class="muted">${esc(s.error || "Die Analyse ist fehlgeschlagen.")}</p>
        <div style="display:flex;gap:8px;justify-content:center;margin-top:16px"><a class="btn" href="#/">Zurück</a>
        <button class="btn danger" id="del">Fall löschen</button></div></div>`;
      $("#del").onclick = async () => { await api(`/api/cases/${id}`, { method: "DELETE" }); location.hash = "#/"; };
      return;
    }
    let card = $("#progress-card");
    if (!card) {
      view.innerHTML = `<div class="card progress-card" id="progress-card"><h2>${esc(s.name)}</h2>
        <p class="muted" id="stage"></p><div class="bar" id="bar"><i style="width:0"></i></div>
        <p class="tiny">${ICON_LOCK} Analyse läuft lokal. Sie können diese Seite verlassen; der Fall erscheint danach in der Liste.</p>
        <a class="btn sm ghost" href="#/">Zur Fallliste</a></div>`;
      card = $("#progress-card");
    }
    $("#stage").textContent = s.stage || "Wartet …";
    $("#bar").classList.toggle("indet", s.status === "queued");
    $("#bar > i").style.width = `${Math.max(3, Math.round((s.progress || 0) * 100))}%`;
    later(() => openCase(id), 700);
  }

  // ---------------------------------------------------------------- review
  async function loadReview(id, keep = true) {
    try {
      const data = await api(`/api/cases/${id}/review`);
      if (S.caseId !== id) return;
      setData(data, keep);
    } catch (e) { toast(e.message, "err"); }
  }

  function setData(data, keep = true) {
    const scroller = $(".doc-scroll");
    const scroll = keep && scroller ? scroller.scrollTop : 0;
    const sideScroll = keep && $(".side-body") ? $(".side-body").scrollTop : 0;
    S.data = data;
    S.byKey = new Map(data.mentions.map((m) => [m.key, m]));
    S.manualByKey = new Map(data.manual.map((m) => [m.key, m]));
    S.entById = new Map(data.entities.map((e) => [e.id, e]));
    renderReview();
    if (keep) { $(".doc-scroll").scrollTop = scroll; $(".side-body").scrollTop = sideScroll; }
  }

  function state(m) {
    if (!m) return "manual";
    const d = m.decision;
    if (m.kind === "change") return d === "reject" || d === "dismiss" ? "reject" : "accept";
    if (d === "apply" || d === "accept") return "apply";
    if (d === "dismiss" || d === "reject") return "dismiss";
    return "open";
  }

  function categoryCounts() {
    const counts = {};
    for (const m of S.data.mentions) counts[m.category] = (counts[m.category] || 0) + 1;
    if (S.data.manual.length) counts.manual = S.data.manual.length;
    return counts;
  }

  function renderReview() {
    const d = S.data;
    const counts = categoryCounts();
    view.innerHTML = `
      <div class="review">
        <div class="toolbar">
          <a class="btn ghost sm" href="#/" title="Zur Fallliste">← Fälle</a>
          <div class="title" title="${esc(d.case.name)}">${esc(d.case.name)}</div>
          <div class="seg" role="group" aria-label="Ansicht">
            <button data-mode="markup" class="${S.mode === "markup" ? "on" : ""}" title="Original mit Vorschlägen">Prüfansicht</button>
            <button data-mode="result" class="${S.mode === "result" ? "on" : ""}" title="So wird die Entscheidung veröffentlicht">Ergebnis</button>
          </div>
          <div class="chips">${CAT_ORDER.filter((c) => counts[c]).map((c) => `<span class="chip ${S.hidden.has(c) ? "off" : ""}" data-cat="${c}" title="Ein-/ausblenden"><span class="swatch sw-${c}"></span>${CAT[c]} <b>${counts[c]}</b></span>`).join("")}</div>
          <div class="spacer"></div>
          <span class="tiny saved" title="Jede Entscheidung wird sofort lokal gespeichert">✓ gespeichert</span>
          <button class="btn" id="next-q" title="Nächste offene Frage (N)">Nächste Frage <span id="q-count"></span> <kbd>N</kbd></button>
          <button class="btn primary" id="export-btn">Exportieren</button>
        </div>
        <div class="workspace">
          <div class="doc-scroll" id="doc-scroll"><div class="paper mode-${S.mode}" id="paper">
            ${d.sections.map((s) => `<section class="doc-section kind-${s.kind}"><div class="sec-title">${esc(s.title)}</div><div class="sec-body">${s.html}</div></section>`).join("")}
          </div></div>
          <aside class="sidebar">
            <div class="tabs" role="tablist">
              <button data-tab="questions">Fragen<span class="count" id="tab-q"></span></button>
              <button data-tab="entities">Entitäten<span class="count">${d.entities.length}</span></button>
              <button data-tab="hints">Hinweise<span class="count">${d.passages.filter((p) => p.status !== "done").length}</span></button>
              <button data-tab="info">Info</button>
            </div>
            <div class="side-body" id="side"></div>
          </aside>
        </div>
      </div>`;
    const paper = $("#paper");
    for (const c of S.hidden) paper.classList.add("hide-" + c);
    $$(".seg button").forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
    $$(".chip").forEach((ch) => (ch.onclick = () => {
      const c = ch.dataset.cat;
      if (S.hidden.has(c)) S.hidden.delete(c); else S.hidden.add(c);
      ch.classList.toggle("off"); paper.classList.toggle("hide-" + c);
    }));
    $("#next-q").onclick = nextQuestion;
    $("#export-btn").onclick = doExport;
    $$(".tabs button").forEach((b) => (b.onclick = () => { S.tab = b.dataset.tab; renderSidebar(); }));
    paper.addEventListener("click", onPaperClick);
    paper.addEventListener("mouseup", onSelect);
    $("#doc-scroll").addEventListener("scroll", () => { closePopover(); hideSeltool(); }, { passive: true });
    applyStates();
    renderSidebar();
  }

  function setMode(mode) {
    S.mode = mode;
    $("#paper").classList.toggle("mode-result", mode === "result");
    $("#paper").classList.toggle("mode-markup", mode === "markup");
    $$(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    closePopover(); hideSeltool();
  }

  function applyStates() {
    const paper = $("#paper");
    for (const mk of $$("mark.m", paper)) {
      const st = state(S.byKey.get(mk.dataset.key));
      mk.classList.remove("d-accept", "d-reject", "d-open", "d-apply", "d-dismiss", "d-manual");
      mk.classList.add("d-" + st);
      mk.setAttribute("tabindex", "-1");
    }
    $$("p.has-passage", paper).forEach((p) => p.classList.remove("has-passage"));
    for (const p of S.data.passages) {
      if (p.status === "done") continue;
      const el = paper.querySelector(`p[data-pid="${CSS.escape(p.pid)}"]`);
      if (el) { el.classList.add("has-passage"); el.title = "Hinweis Richtlinie 2.4: siehe Tab „Hinweise“"; }
    }
    const open = S.data.mentions.filter((m) => state(m) === "open").length;
    S.data.open_questions = open;
    $("#q-count").textContent = open ? `(${open})` : "";
    $("#next-q").disabled = !open;
    const tq = $("#tab-q");
    tq.textContent = open; tq.classList.toggle("hot", open > 0);
    const ex = $("#export-btn");
    ex.title = open ? `Zuerst ${open} offene Frage(n) entscheiden` : "Anonymisierte Fassung erstellen";
  }

  // ------------------------------------------------ decisions
  async function decide(keys, decision) {
    try {
      const r = await api(`/api/cases/${S.caseId}/decisions`, { method: "POST", body: { keys, decision } });
      for (const [k, v] of Object.entries(r.decisions)) { const m = S.byKey.get(k); if (m) m.decision = v; }
      applyStates();
      if (S.tab === "questions") renderSidebar();
    } catch (e) { toast(e.message, "err"); }
  }

  async function entityAction(id, action, value) {
    try {
      const data = await api(`/api/cases/${S.caseId}/entities/${id}`, { method: "POST", body: { action, value } });
      setData(data);
      return true;
    } catch (e) { toast(e.message, "err"); return false; }
  }

  function normalisedFor(m, wanted) {
    if (m.kind === "change") return wanted === "yes" ? "accept" : "reject";
    return wanted === "yes" ? "apply" : "dismiss";
  }

  // ------------------------------------------------ marks: focus, popover
  function marks() { return $$("#paper mark.m"); }

  function focusMark(mk, scroll = false) {
    $$("#paper mark.focus").forEach((x) => x.classList.remove("focus"));
    mk.classList.add("focus");
    S.focus = mk.dataset.key;
    if (scroll) mk.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function flash(el) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }

  function goToKey(key) {
    const mk = $(`#paper mark[data-key="${CSS.escape(key)}"]`);
    if (!mk) return;
    if (S.mode === "result") setMode("markup");
    focusMark(mk, true);
    setTimeout(() => openPopover(mk), 350);
  }

  function goToPid(pid) {
    const p = $(`#paper p[data-pid="${CSS.escape(pid)}"]`);
    if (!p) { toast("Stelle nicht im Dokument gefunden (z. B. Dokumenteigenschaften).", "err"); return; }
    p.scrollIntoView({ block: "center", behavior: "smooth" });
    flash(p);
  }

  function nextQuestion() {
    const all = marks().filter((mk) => state(S.byKey.get(mk.dataset.key)) === "open");
    if (!all.length) { toast("Keine offenen Fragen mehr.", "ok"); return; }
    const cur = all.findIndex((mk) => mk.dataset.key === S.focus);
    const next = all[(cur + 1) % all.length];
    if (S.mode === "result") setMode("markup");
    focusMark(next, true);
    setTimeout(() => openPopover(next), 350);
  }

  function onPaperClick(e) {
    if (S.mode === "result") return;
    const mk = e.target.closest("mark.m");
    if (!mk) { closePopover(); return; }
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
    e.stopPropagation();
    focusMark(mk);
    openPopover(mk);
  }

  function closePopover() { pop.hidden = true; pop.innerHTML = ""; }

  function placeFloating(el, rect, width) {
    el.hidden = false;
    const h = el.offsetHeight;
    width = el.offsetWidth || width;
    let left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 12);
    let top = rect.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(64, rect.top - h - 8);
    el.style.left = `${left}px`; el.style.top = `${top}px`;
  }

  function openPopover(mk) {
    hideSeltool();
    const key = mk.dataset.key;
    if (key.startsWith("manual:")) {
      const me = S.manualByKey.get(key);
      const idx = Number(key.split(":")[1]);
      pop.innerHTML = `<div class="ph"><span class="swatch sw-manual"></span>Manuelle Änderung<button class="menu-btn x" aria-label="Schliessen">✕</button></div>
        <div class="pv"><span class="from">${esc(me.text)}</span><span>→</span><span class="to">${esc(me.replacement) || "<em>entfernt</em>"}</span></div>
        <div class="main-acts"><button class="btn" data-act="edit">Bearbeiten</button><button class="btn danger" data-act="del">Rückgängig</button></div>`;
      pop.onclick = async (e) => {
        if (e.target.closest(".x")) return closePopover();
        const act = e.target.closest("[data-act]");
        if (!act) return;
        if (act.dataset.act === "del" || act.dataset.act === "edit") {
          try {
            const data = await api(`/api/cases/${S.caseId}/manual/${idx}`, { method: "DELETE" });
            setData(data);
            if (act.dataset.act === "edit") openManualModal({ pid: me.part + "|" + me.para, start: me.start, end: me.end, text: me.text }, me.replacement);
          } catch (err) { toast(err.message, "err"); }
        }
      };
      placeFloating(pop, mk.getBoundingClientRect(), 360);
      return;
    }
    const m = S.byKey.get(key);
    if (!m) return;
    const e = S.entById.get(m.entity);
    const st = state(m);
    const cat = m.category;
    const yes = m.kind === "change" ? "accept" : "apply";
    const no = m.kind === "change" ? "reject" : "dismiss";
    const label = e ? e.label : "";
    pop.innerHTML = `
      <div class="ph"><span class="swatch sw-${cat}"></span>${CAT[cat] || cat}${m.kind === "question" ? " · unsicher" : ""}<button class="menu-btn x" aria-label="Schliessen">✕</button></div>
      <div class="pv"><span class="from">${esc(m.text.trim())}</span><span>→</span><span class="to">${m.replacement ? esc(m.replacement) : "<em>entfernen</em>"}</span></div>
      ${m.reason ? `<div class="why">${esc(m.reason)}</div>` : ""}
      <div class="main-acts">
        <button class="btn ${st === "accept" || st === "apply" ? "primary" : ""}" data-act="${yes}">Anonymisieren <kbd>A</kbd></button>
        <button class="btn ${st === "reject" || st === "dismiss" ? "primary" : ""}" data-act="${no}">Original lassen <kbd>X</kbd></button>
      </div>
      ${e && e.count > 1 ? `<div class="sub">Alle ${e.count} Vorkommen von „${esc(label)}“:
        <div class="acts"><button class="btn sm" data-all="yes">anonymisieren</button><button class="btn sm" data-all="no">Original lassen</button></div></div>` : ""}
      ${e && e.forms.length ? `<div class="sub"><div class="acts">
        <button class="btn sm ghost" data-ent="replacement">Ersetzung ändern (${esc(e.replacement)}) …</button>
        <button class="btn sm ghost" data-ent="keep" title="Diese Entität nirgends anonymisieren">Nie anonymisieren</button></div></div>` : ""}`;
    pop.onclick = async (ev) => {
      if (ev.target.closest(".x")) return closePopover();
      const act = ev.target.closest("[data-act]");
      if (act) { await decide([m.key], act.dataset.act); closePopover(); afterDecision(m); return; }
      const all = ev.target.closest("[data-all]");
      if (all) { closePopover(); await entityAction(m.entity, "decide_all", all.dataset.all === "yes" ? "accept" : "reject"); return; }
      const ent = ev.target.closest("[data-ent]");
      if (ent && ent.dataset.ent === "keep") { closePopover(); if (await entityAction(m.entity, "keep")) toast(`„${label}“ wird nicht anonymisiert (rückgängig im Tab „Entitäten“).`); }
      if (ent && ent.dataset.ent === "replacement") { closePopover(); askReplacement(e); }
    };
    placeFloating(pop, mk.getBoundingClientRect(), 360);
  }

  function afterDecision(m) {
    if (m.kind === "question" && S.data.open_questions > 0) setTimeout(nextQuestion, 120);
  }

  function askReplacement(e) {
    const { close } = modal({
      title: `Ersetzung für „${e.label}“`,
      body: `<label class="field"><span>Ersetzung <small>(gilt für alle ${e.count} Vorkommen)</small></span>
        <input type="text" id="rep-in" maxlength="40" value="${esc(e.replacement)}"></label>
        <p class="tiny">Buchstaben werden sonst automatisch nach der ersten Erwähnung vergeben. Eine hier gesetzte Ersetzung bleibt fest.</p>`,
      actions: [
        { label: "Abbrechen", onClick: (c) => c() },
        { label: "Übernehmen", kind: "primary", onClick: async (c) => { const v = $("#rep-in").value.trim(); if (!v) return; c(); await entityAction(e.id, "replacement", v); } },
      ],
    });
    $("#rep-in").addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); $(".modal-f .primary").click(); } });
    return close;
  }

  // ------------------------------------------------ selection -> new entity / manual edit
  function nodeLen(node) {
    if (node.nodeType === 3) return node.length;
    if (node.nodeType !== 1) return 0;
    if (node.dataset.skip) return 0;
    if (node.dataset.len !== undefined) return Number(node.dataset.len);
    let n = 0;
    for (const c of node.childNodes) n += nodeLen(c);
    return n;
  }

  function offsetOf(p, container, offset) {
    let total = 0, found = null;
    (function walk(node) {
      if (found !== null) return;
      if (node !== p && node.nodeType === 1 && (node.dataset.skip || node.dataset.len !== undefined)) {
        // replacement labels and tabs/footnote marks: a boundary inside counts as their start
        if (node.contains(container)) { found = total; return; }
        total += nodeLen(node); return;
      }
      if (node === container) {
        if (node.nodeType === 3) { found = total + offset; return; }
        let n = 0;
        for (let i = 0; i < offset && i < node.childNodes.length; i++) n += nodeLen(node.childNodes[i]);
        found = total + n; return;
      }
      if (node.nodeType === 3) { total += node.length; return; }
      if (node.nodeType !== 1) return;
      for (const c of node.childNodes) { walk(c); if (found !== null) return; }
    })(p);
    return found;
  }

  function onSelect() {
    if (S.mode === "result") return;
    setTimeout(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) { hideSeltool(); return; }
      const r = sel.getRangeAt(0);
      const para = (n) => (n.nodeType === 1 ? n : n.parentElement).closest("p[data-pid]");
      const pa = para(r.startContainer), pb = para(r.endContainer);
      if (!pa || pa !== pb) { hideSeltool(); if (pa && pb) toast("Bitte nur innerhalb eines Absatzes markieren."); return; }
      const start = offsetOf(pa, r.startContainer, r.startOffset);
      const end = offsetOf(pa, r.endContainer, r.endOffset);
      if (start === null || end === null || end <= start) { hideSeltool(); return; }
      const frag = r.cloneContents();
      frag.querySelectorAll("[data-skip]").forEach((n) => n.remove());
      S.sel = { pid: pa.dataset.pid, start, end, text: frag.textContent };
      closePopover();
      seltool.innerHTML = `<span>Anonymisieren als</span>
        <button data-type="person">Person</button><button data-type="org">Firma</button><button data-type="representative">Vertreter</button>
        <button data-type="place">Ort</button><button data-type="plot">Grundstück</button><button data-type="id">Datum/ID</button>
        <i class="sep"></i><button data-type="manual" title="Freie Ersetzung, z. B. „[…]“ (Richtlinie 2.4)">Textstelle ersetzen …</button>`;
      placeFloating(seltool, r.getBoundingClientRect(), 680);
      const rect = r.getBoundingClientRect();
      const top = rect.top - seltool.offsetHeight - 8;
      if (top > 60) seltool.style.top = `${top}px`;
    }, 0);
  }

  function hideSeltool() { seltool.hidden = true; }

  seltool.addEventListener("mousedown", (e) => e.preventDefault());
  seltool.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-type]");
    if (!b || !S.sel) return;
    const sel = S.sel;
    hideSeltool();
    window.getSelection().removeAllRanges();
    if (b.dataset.type === "manual") { openManualModal(sel); return; }
    try {
      const data = await api(`/api/cases/${S.caseId}/entities`, { method: "POST", body: { pid: sel.pid, start: sel.start, end: sel.end, type: b.dataset.type } });
      setData(data);
      const text = sel.text.trim();
      const ent = data.entities.find((x) => x.forms.some((f) => text.includes(f) || f.includes(text)));
      toast(ent ? `„${ent.label}“ wird überall anonymisiert (${ent.count} Vorkommen, Ersetzung ${ent.replacement}).` : "Hinzugefügt.", "ok");
    } catch (err) { toast(err.message, "err"); }
  });

  function openManualModal(sel, value = "[…]", proposal = "") {
    modal({
      title: "Textstelle ersetzen",
      wide: true,
      body: `<p class="muted">Freie Ersetzung für Passagen, die eine Person indirekt erkennbar machen (Richtlinie 2.4). Grössere Auslassungen mit „[…]“ kennzeichnen.</p>
        <label class="field"><span>Original</span><div class="callout info" style="font-family:var(--doc);font-size:15px">${esc(sel.text)}</div></label>
        <label class="field"><span>Ersetzen durch</span><textarea id="man-in">${esc(value)}</textarea></label>
        ${proposal ? `<button class="btn sm" id="use-proposal" title="Vorschlag des lokalen Sprachmodells übernehmen – bitte prüfen">Vorschlag des Sprachmodells einsetzen</button>
          <p class="tiny">Vorschlag: ${esc(proposal)}</p>` : ""}`,
      actions: [
        { label: "Abbrechen", onClick: (c) => c() },
        { label: "Ersetzen", kind: "primary", onClick: async (c) => {
          const replacement = $("#man-in").value;
          try {
            const data = await api(`/api/cases/${S.caseId}/manual`, { method: "POST", body: { pid: sel.pid, start: sel.start, end: sel.end, replacement } });
            c(); setData(data); toast("Textstelle ersetzt.", "ok");
          } catch (err) { toast(err.message, "err"); }
        } },
      ],
    });
    const up = $("#use-proposal");
    if (up) up.onclick = () => { $("#man-in").value = proposal; $("#man-in").focus(); };
  }

  // ------------------------------------------------ sidebar
  function renderSidebar() {
    $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
    const side = $("#side");
    if (!side) return;
    const fn = { questions: sideQuestions, entities: sideEntities, hints: sideHints, info: sideInfo }[S.tab];
    side.innerHTML = fn();
    bindSide(side);
  }

  function questionCard(m, done) {
    const st = state(m);
    return `<div class="item clickable ${done ? "done" : ""}" data-go="${esc(m.key)}">
      <div class="line"><span class="swatch sw-${m.category}"></span><span class="orig-t">${esc(m.text.trim())}</span><span class="arrow">→</span><b>${m.replacement ? esc(m.replacement) : "<em>entfernen</em>"}</b></div>
      ${m.context ? `<div class="ctx">${esc(m.context[0])}<b>${esc(m.text)}</b>${esc(m.context[1])}</div>` : ""}
      ${m.reason ? `<div class="why">${esc(m.reason)}</div>` : ""}
      <div class="acts">
        <button class="btn sm ${st === "apply" ? "primary" : ""}" data-dec="apply" data-key="${esc(m.key)}">Anonymisieren</button>
        <button class="btn sm ${st === "dismiss" ? "primary" : ""}" data-dec="dismiss" data-key="${esc(m.key)}">Original lassen</button>
      </div></div>`;
  }

  function sideQuestions() {
    const order = new Map(marks().map((mk, i) => [mk.dataset.key, i]));
    const qs = S.data.mentions.filter((m) => m.kind === "question")
      .sort((a, b) => (order.get(a.key) ?? 1e9) - (order.get(b.key) ?? 1e9));
    const open = qs.filter((m) => state(m) === "open"), decided = qs.filter((m) => state(m) !== "open");
    let html = "";
    if (!qs.length) html += '<div class="callout ok">Keine unsicheren Stellen. Prüfen Sie die Vorschläge im Text und exportieren Sie.</div>';
    else if (!open.length) html += '<div class="callout ok">Alle Fragen entschieden. Sie können exportieren.</div>';
    else html += `<div class="callout warn">${open.length} Stelle${open.length === 1 ? "" : "n"} braucht Ihre Entscheidung. Tipp: <span class="kbd">N</span> springt zur nächsten, <span class="kbd">A</span>/<span class="kbd">X</span> entscheidet.</div>`;
    html += open.map((m) => questionCard(m, false)).join("");
    if (decided.length) {
      html += `<div class="group-h"><button class="btn sm ghost" id="toggle-decided">${S.showDecided ? "▾" : "▸"} Entschieden (${decided.length})</button></div>`;
      if (S.showDecided) html += decided.map((m) => questionCard(m, true)).join("");
    }
    const changes = S.data.mentions.filter((m) => m.kind === "change");
    const rejected = changes.filter((m) => state(m) === "reject").length;
    html += `<p class="tiny" style="margin-top:14px">Dazu ${changes.length} sichere Vorschläge im Text (${rejected} davon abgelehnt). Ein Klick auf eine Markierung im Text öffnet die Details.</p>`;
    return html;
  }

  function sideEntities() {
    const groups = {};
    for (const e of S.data.entities) (groups[e.category] = groups[e.category] || []).push(e);
    let html = '<p class="tiny">Text im Dokument markieren, um etwas Übersehenes zu anonymisieren. Klick auf die Ersetzung ändert sie für alle Vorkommen.</p>';
    for (const c of CAT_ORDER) {
      const list = groups[c];
      if (!list) continue;
      html += `<div class="group-h"><span class="swatch sw-${c}"></span>${CAT[c]} (${list.length})</div>`;
      html += list.map((e) => {
        const kept = e.decision === "keep";
        const role = e.kind === "person" || e.kind === "org" ? (ROLE[e.role] || "") : (KIND[e.kind] || "");
        return `<div class="ent ${kept ? "kept" : ""}" data-ent="${e.id}">
          <span class="swatch sw-${c}"></span>
          <span class="label" data-show="${e.id}" title="${esc(e.forms.join(" | ") || e.label)}">${esc(e.label)}${e.forms.length > 1 ? `<small>+${e.forms.length - 1}</small>` : ""}${role ? `<small>${esc(role)}</small>` : ""}</span>
          ${kept ? '<span class="badge">beibehalten</span>' : `<span class="rep-pill" data-rep="${e.id}" title="Ersetzung ändern">${esc(e.replacement) || "–"}</span>`}
          <span class="cnt" title="Vorkommen">×${e.count}</span>
          <button class="menu-btn" data-menu="${e.id}" aria-label="Aktionen">⋯</button></div>`;
      }).join("");
    }
    return html;
  }

  function sideHints() {
    const ps = S.data.passages;
    if (!ps.length) {
      const llm = S.data.engines && S.data.engines.llm;
      return `<div class="callout info">${llm === "on" ? "Das Sprachmodell hat keine Passagen gefunden, die eine Person indirekt erkennbar machen." : "Hinweise nach Richtlinie 2.4 kommen vom lokalen Sprachmodell. Für diesen Fall war es nicht aktiv."}</div>
        <p class="tiny">Sie können jede Textstelle selbst markieren und mit „Textstelle ersetzen …“ neutralisieren oder durch „[…]“ ersetzen.</p>`;
    }
    return '<p class="tiny">Vorschläge des lokalen Sprachmodells (Richtlinie 2.4). Sie werden nie automatisch angewendet.</p>' +
      ps.map((p) => `<div class="item ${p.status === "done" ? "done" : ""}">
        <div class="orig-t" style="font-weight:400">„${esc(p.quote)}“</div>
        <div class="why">${esc(p.reason)}</div>
        ${p.proposal ? `<div class="why"><b>Vorschlag:</b> ${esc(p.proposal)}</div>` : ""}
        <div class="acts"><button class="btn sm" data-pgo="${esc(p.pid)}">Zur Stelle</button>
        <button class="btn sm" data-prep="${esc(p.key)}">Ersetzen …</button>
        <button class="btn sm ghost" data-pdone="${esc(p.key)}" data-status="${p.status === "done" ? "open" : "done"}">${p.status === "done" ? "Wieder öffnen" : "Erledigt"}</button></div></div>`).join("");
  }

  function sideInfo() {
    const d = S.data, eng = d.engines || {};
    const counts = categoryCounts();
    const ner = { on: "aktiv", cached: "aktiv", unavailable: "nicht installiert", off: "aus" }[eng.ner] || "–";
    const llm = eng.llm === "on" ? esc(eng.model || "aktiv") : eng.llm === "error" ? "Fehler (teilweise ohne)" : "nicht verwendet";
    return `<dl class="dl">
        <dt>Namenserkennung</dt><dd>${ner}</dd><dt>Sprachmodell</dt><dd>${llm}</dd>
        ${CAT_ORDER.filter((c) => counts[c]).map((c) => `<dt>${CAT[c]}</dt><dd>${counts[c]} Stellen</dd>`).join("")}
      </dl>
      ${d.kept.length ? `<div class="group-h">Nicht anonymisiert (Gericht)</div><p style="margin:0 4px">${d.kept.map(esc).join(", ")}</p>` : ""}
      ${d.notes.length ? `<div class="group-h">Hinweise</div>${d.notes.map((n) => `<div class="callout warn">${esc(n)}</div>`).join("")}` : ""}
      <div class="group-h">Dateien</div>
      <div class="acts" style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn sm" id="dl-entities" title="Für die Bearbeitung in Excel / Archiv">Entitäten (Excel)</button>
        <button class="btn sm danger" id="del-case">Fall löschen</button></div>
      <p class="tiny" style="margin-top:12px">Tastatur: <span class="kbd">N</span> nächste Frage · <span class="kbd">J</span>/<span class="kbd">K</span> nächste/vorige Markierung · <span class="kbd">A</span> anonymisieren · <span class="kbd">X</span> Original lassen · <span class="kbd">E</span> Ergebnis-Ansicht · <span class="kbd">Esc</span> schliessen</p>`;
  }

  function bindSide(side) {
    side.onclick = async (e) => {
      const dec = e.target.closest("[data-dec]");
      if (dec) { e.stopPropagation(); await decide([dec.dataset.key], dec.dataset.dec); return; }
      const go = e.target.closest("[data-go]");
      if (go) { goToKey(go.dataset.go); return; }
      const tog = e.target.closest("#toggle-decided");
      if (tog) { S.showDecided = !S.showDecided; renderSidebar(); return; }
      const rep = e.target.closest("[data-rep]");
      if (rep) { askReplacement(S.entById.get(rep.dataset.rep)); return; }
      const show = e.target.closest("[data-show]");
      if (show) { cycleEntity(show.dataset.show); return; }
      const menu = e.target.closest("[data-menu]");
      if (menu) { entityMenu(S.entById.get(menu.dataset.menu), menu.getBoundingClientRect()); return; }
      const pgo = e.target.closest("[data-pgo]");
      if (pgo) { goToPid(pgo.dataset.pgo); return; }
      const prep = e.target.closest("[data-prep]");
      if (prep) {
        const p = S.data.passages.find((x) => x.key === prep.dataset.prep);
        openManualModal({ pid: p.pid, start: p.start, end: p.end, text: p.quote }, "[…]", p.proposal);
        return;
      }
      const pdone = e.target.closest("[data-pdone]");
      if (pdone) {
        try {
          await api(`/api/cases/${S.caseId}/passages`, { method: "POST", body: { key: pdone.dataset.pdone, status: pdone.dataset.status } });
          const p = S.data.passages.find((x) => x.key === pdone.dataset.pdone); p.status = pdone.dataset.status;
          applyStates(); renderSidebar();
          $(".tabs [data-tab=hints] .count").textContent = S.data.passages.filter((x) => x.status !== "done").length;
        } catch (err) { toast(err.message, "err"); }
        return;
      }
      if (e.target.closest("#dl-entities")) { download(`/api/cases/${S.caseId}/download/entities`, "entities.xlsx"); return; }
      if (e.target.closest("#del-case")) {
        if (await confirmBox("Fall löschen?", "Original, Entscheidungen und Exporte dieses Falls werden von diesem Computer gelöscht.", "Löschen")) {
          await api(`/api/cases/${S.caseId}`, { method: "DELETE" }); location.hash = "#/";
        }
      }
    };
  }

  function cycleEntity(id) {
    const keys = S.data.mentions.filter((m) => m.entity === id).map((m) => m.key);
    const els = marks().filter((mk) => keys.includes(mk.dataset.key));
    if (!els.length) { toast("Keine Vorkommen (Entität wird beibehalten)."); return; }
    const cur = els.findIndex((mk) => mk.dataset.key === S.focus);
    const next = els[(cur + 1) % els.length];
    if (S.mode === "result") setMode("markup");
    focusMark(next, true); flash(next);
  }

  function entityMenu(e, rect) {
    closeMenus();
    const menu = document.createElement("div");
    menu.className = "menu";
    const kept = e.decision === "keep";
    const same = S.data.entities.filter((x) => x.category === e.category && x.id !== e.id && x.decision !== "keep" && x.forms.length);
    menu.innerHTML = `
      ${e.forms.length ? `<button data-a="replacement">Ersetzung ändern …</button>` : ""}
      ${kept ? '<button data-a="anonymise">Wieder anonymisieren</button>' : (e.forms.length ? '<button data-a="keep">Nie anonymisieren</button>' : "")}
      ${!kept ? '<hr><button data-a="all-yes">Alle Vorkommen anonymisieren</button><button data-a="all-no">Bei allen Vorkommen Original lassen</button>' : ""}
      ${same.length && !kept && e.forms.length ? `<hr><div class="tiny" style="padding:4px 10px">Zusammenführen mit (gleiche Person/Firma):</div>${same.slice(0, 12).map((x) => `<button data-merge="${x.id}">${esc(x.label)} → ${esc(x.replacement)}</button>`).join("")}` : ""}`;
    document.body.appendChild(menu);
    const left = Math.min(rect.left - 180, window.innerWidth - 250);
    menu.style.left = `${Math.max(8, left)}px`;
    menu.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
    menu.onclick = async (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      closeMenus();
      if (b.dataset.merge) { if (await entityAction(e.id, "merge", b.dataset.merge)) toast("Zusammengeführt.", "ok"); return; }
      const a = b.dataset.a;
      if (a === "replacement") askReplacement(e);
      else if (a === "keep") { if (await entityAction(e.id, "keep")) toast(`„${e.label}“ wird nicht anonymisiert.`); }
      else if (a === "anonymise") await entityAction(e.id, "anonymise");
      else if (a === "all-yes") await entityAction(e.id, "decide_all", "accept");
      else if (a === "all-no") await entityAction(e.id, "decide_all", "reject");
    };
    setTimeout(() => document.addEventListener("mousedown", closeMenusOutside), 0);
  }
  function closeMenus() { $$(".menu").forEach((m) => m.remove()); document.removeEventListener("mousedown", closeMenusOutside); }
  function closeMenusOutside(e) { if (!e.target.closest(".menu")) closeMenus(); }

  // ------------------------------------------------ export
  async function doExport() {
    if (S.data.open_questions > 0) {
      toast(`Bitte zuerst ${S.data.open_questions} offene Frage(n) entscheiden.`, "err");
      S.tab = "questions"; renderSidebar(); nextQuestion();
      return;
    }
    const busy = modal({ title: "Anonymisierte Fassung erstellen", body: '<div class="spinner"></div><p class="muted" style="text-align:center">Änderungen anwenden, Metadaten entfernen und auf verbliebene Originalwerte prüfen …</p>' });
    let r;
    try { r = await api(`/api/cases/${S.caseId}/export`, { method: "POST" }); }
    catch (e) { busy.close(); toast(e.message, "err"); return; }
    busy.close();
    if (r.ok) exportOk(r); else exportFail(r);
  }

  function exportOk(r) {
    const media = (r.unchecked || []).length;
    const { el } = modal({
      title: "Bereit zur Veröffentlichung",
      body: `<div class="result-icon ok">✓</div>
        <p style="text-align:center;margin-top:0"><b>Leak-Check bestanden.</b><br><span class="muted">Kein Originalwert mehr in der Datei; Metadaten, Kommentare und Prüfspuren entfernt.</span></p>
        <div class="big-action"><button class="btn primary" id="dl-clean">Anonymisierte Fassung herunterladen</button></div>
        ${media ? `<div class="callout warn">${media} Bild/Objekt-Datei(en) können nicht automatisch geprüft werden. Bitte in Word kurz ansehen (z. B. eingescannte Unterschriften, Logos).</div>` : ""}
        <details><summary class="tiny">Prüfprotokoll</summary><pre class="tiny" style="white-space:pre-wrap">${esc((r.report || []).join("\n"))}</pre></details>
        <div class="callout info" style="margin-top:12px">Nach dem Herunterladen können Sie den Fall abschliessen. Dabei werden Original und Zuordnungen von diesem Computer gelöscht (Anonymisierung ist dann unumkehrbar).</div>`,
      actions: [
        { label: "Prüfkopie mit Änderungen (Word)", onClick: () => download(`/api/cases/${S.caseId}/download/review`, "review.docx") },
        { label: "Fall abschliessen …", kind: "danger", onClick: async (close) => {
          if (await confirmBox("Fall abschliessen?", "Original, Zuordnungen und Exporte dieses Falls werden gelöscht. Haben Sie die anonymisierte Fassung gespeichert?", "Abschliessen und löschen")) {
            await api(`/api/cases/${S.caseId}`, { method: "DELETE" }); close(); location.hash = "#/"; toast("Fall abgeschlossen und gelöscht.", "ok");
          }
        } },
      ],
    });
    $("#dl-clean", el).onclick = () => download(`/api/cases/${S.caseId}/download/clean`, "anonymisiert.docx");
  }

  function exportFail(r) {
    const hits = r.hits || [];
    const statusText = { leak: "Originalwert gefunden", open_review: "offene Frage", partial_review: "unvollständig geprüft" };
    const dlg = modal({
      title: "Export gestoppt",
      wide: true,
      body: `<div class="result-icon err">!</div>
        <p style="text-align:center;margin-top:0">Die bereinigte Datei würde noch Originalwerte enthalten. Es wurde nichts gespeichert.</p>
        <ul class="problems">${(r.problems || []).filter((p) => !/^(LEAK|OFFENER|TEILWEISE)/.test(p)).map((p) => `<li>${esc(p)}</li>`).join("")}
        ${hits.map((h) => `<li><span><span class="badge err">${esc(statusText[h.status] || h.status)}</span> ${esc(h.category)} ${esc(h.entity)}</span>
          ${h.pid ? `<button class="btn sm" data-hit="${esc(h.pid)}">Zur Stelle</button>` : '<span class="tiny">in Dateieigenschaften/verborgen</span>'}</li>`).join("")}</ul>
        <p class="tiny">Typische Ursache: eine Stelle, die nicht angepasst wurde, z. B. ein Name in anderer Schreibweise. Markieren Sie die Stelle im Text und wählen Sie „Anonymisieren als …“.</p>`,
      actions: [{ label: "Schliessen", kind: "primary", onClick: (c) => c() }],
    });
    dlg.el.addEventListener("click", (e) => {
      const b = e.target.closest("[data-hit]");
      if (b) { dlg.close(); goToPid(b.dataset.hit); }
    });
  }

  // ------------------------------------------------ settings
  async function openSettings() {
    let s;
    try { s = await api("/api/settings"); } catch (e) { toast(e.message, "err"); return; }
    const p = s.policy;
    const radio = (name, value, label) => `<label><input type="radio" name="${name}" value="${value}" ${String(p[name]) === String(value) ? "checked" : ""}> ${label}</label>`;
    const { close, el } = modal({
      title: "Einstellungen",
      wide: true,
      body: `
        <h4 style="margin:4px 0 6px">Lokales Sprachmodell</h4>
        <label class="switch"><input type="checkbox" id="set-llm" ${s.llm.enabled ? "checked" : ""}> Sprachmodell verwenden (Rollen, Namensvarianten, Hinweise nach Richtlinie 2.4)</label>
        <div class="form-grid">
          <label class="field"><span>Server-Adresse <small>(Ollama, vLLM)</small></span><input type="url" id="set-url" value="${esc(s.llm.base_url)}" placeholder="http://localhost:11434/v1"></label>
          <label class="field"><span>Modell</span><div class="inline"><input type="text" id="set-model" list="model-list" value="${esc(s.llm.model)}" placeholder="qwen3:8b"><button class="btn sm" id="load-models" type="button">Modelle laden</button></div><datalist id="model-list"></datalist></label>
        </div>
        <p class="tiny" id="llm-msg">Erlaubte Server (in der Konfigurationsdatei festgelegt): ${s.allowed_hosts.map(esc).join(", ")}. Andere Adressen werden abgelehnt.</p>
        <h4 style="margin:18px 0 6px">Erkennung</h4>
        <label class="switch"><input type="checkbox" id="set-ner" ${s.use_ner && s.ner_available ? "checked" : ""} ${s.ner_available ? "" : "disabled"}> Namenserkennung (NER-Modell)</label>
        ${s.ner_available ? "" : '<p class="tiny">Kein NER-Modell installiert (Befehl <code>anon setup-models</code>). Regeln und Verzeichnisse arbeiten trotzdem.</p>'}
        <h4 style="margin:18px 0 6px">Richtlinien-Optionen</h4>
        <label class="field"><span>Behandelnde Ärzte, Gutachter, Zeugen</span><div class="radio-row">${radio("third_parties", "letters", "Buchstaben")}${radio("third_parties", "keep", "nicht anonymisieren")}</div></label>
        <label class="field"><span>Parteivertreter</span><div class="radio-row">${radio("representatives", "stars", "***")}${radio("representatives", "letters", "Buchstaben (bei mehreren Vertretern)")}</div></label>
        <label class="field"><span>Geburtsdaten</span><div class="radio-row">${radio("birth_dates", "stars", "***")}${radio("birth_dates", "generalise", "„vor dem Jahr …“")}</div></label>
        <label class="switch"><input type="checkbox" id="set-activity" ${p.company_keep_activity ? "checked" : ""}> Tätigkeit bei Firmen nennen („A Bank AG“ statt „A AG“)</label>
        <h4 style="margin:18px 0 6px">Datenschutz</h4>
        <label class="field"><span>Fälle automatisch löschen nach</span><div class="inline" style="max-width:220px"><input type="number" id="set-ret" min="1" max="365" value="${s.retention_days}"><span>Tagen</span></div></label>`,
      actions: [
        { label: "Abbrechen", onClick: (c) => c() },
        { label: "Speichern", kind: "primary", onClick: async (c) => {
          const val = (n) => { const r = $(`input[name=${n}]:checked`, el); return r ? r.value : undefined; };
          const body = {
            llm: { enabled: $("#set-llm").checked, base_url: $("#set-url").value.trim(), model: $("#set-model").value.trim() },
            use_ner: s.ner_available ? $("#set-ner").checked : s.use_ner,
            policy: { third_parties: val("third_parties"), representatives: val("representatives"), birth_dates: val("birth_dates"), company_keep_activity: $("#set-activity").checked },
            retention_days: Number($("#set-ret").value || 14),
          };
          try {
            await api("/api/settings", { method: "PUT", body });
            c(); toast("Einstellungen gespeichert.", "ok");
            await loadStatus();
            if (S.caseId && S.data) loadReview(S.caseId);
          } catch (e) { $("#llm-msg").innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`; }
        } },
      ],
    });
    $("#load-models", el).onclick = async () => {
      const msg = $("#llm-msg", el);
      msg.textContent = "Verbinde …";
      try {
        const r = await api("/api/llm/models", { method: "POST", body: { base_url: $("#set-url", el).value.trim(), model: $("#set-model", el).value.trim() } });
        if (!r.ok) { msg.innerHTML = `<span style="color:var(--danger)">${esc(r.error)}</span>`; return; }
        $("#model-list", el).innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
        const cur = $("#set-model", el).value.trim();
        msg.innerHTML = `<span style="color:var(--ok)">Verbunden. ${r.models.length} Modell(e): ${r.models.slice(0, 8).map(esc).join(", ")}${r.models.length > 8 ? " …" : ""}</span>` +
          (cur && r.models.length && !r.models.includes(cur) ? ` <span style="color:var(--warn)">„${esc(cur)}“ ist nicht installiert.</span>` : "");
      } catch (e) { msg.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`; }
    };
    void close;
  }

  // ------------------------------------------------ Word path
  function openWordFinalize() {
    modal({
      title: "In Word geprüfte Prüfkopie fertigstellen",
      body: `<p class="muted">Für Prüfkopien, die in Word bearbeitet wurden (Änderungen angenommen/abgelehnt, Kommentare erledigt).</p>
        <label class="field"><span>Prüfkopie (.docx)</span><input type="file" id="wf-docx" accept=".docx"></label>
        <label class="field"><span>Entitäten-Datei (.xlsx)</span><input type="file" id="wf-xlsx" accept=".xlsx"></label>
        <div id="wf-msg"></div>`,
      actions: [
        { label: "Abbrechen", onClick: (c) => c() },
        { label: "Fertigstellen", kind: "primary", onClick: async (c, el, btn) => {
          const d = $("#wf-docx", el).files[0], x = $("#wf-xlsx", el).files[0];
          if (!d || !x) { $("#wf-msg", el).innerHTML = '<div class="callout warn">Bitte beide Dateien wählen.</div>'; return; }
          const form = new FormData(); form.append("review", d); form.append("entities", x);
          btn.disabled = true;
          try {
            const r = await api("/api/word/finalize", { method: "POST", form });
            saveBlob(await r.blob(), filenameFrom(r, "anonymisiert.docx"));
            c(); toast("Bereinigte Fassung gespeichert. Leak-Check bestanden.", "ok");
          } catch (e) {
            $("#wf-msg", el).innerHTML = `<div class="callout err"><b>Nicht fertiggestellt:</b><br>${esc(e.message).replace(/\n/g, "<br>")}</div>`;
          } finally { btn.disabled = false; }
        } },
      ],
    });
  }

  // ------------------------------------------------ keyboard
  document.addEventListener("keydown", (e) => {
    if ($(".backdrop") || e.target.closest("input, textarea, select") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!S.data || !$("#paper")) return;
    const k = e.key.toLowerCase();
    if (k === "escape") { closePopover(); hideSeltool(); closeMenus(); return; }
    if (k === "n") { e.preventDefault(); nextQuestion(); return; }
    if (k === "e") { setMode(S.mode === "result" ? "markup" : "result"); return; }
    if (k === "j" || k === "k") {
      const all = marks(); if (!all.length) return;
      const cur = all.findIndex((mk) => mk.dataset.key === S.focus);
      const next = all[(cur + (k === "j" ? 1 : -1) + all.length) % all.length];
      if (S.mode === "result") setMode("markup");
      focusMark(next, true); closePopover(); return;
    }
    if ((k === "a" || k === "x") && S.focus && !S.focus.startsWith("manual:")) {
      const m = S.byKey.get(S.focus); if (!m) return;
      e.preventDefault();
      decide([m.key], normalisedFor(m, k === "a" ? "yes" : "no")).then(() => { closePopover(); afterDecision(m); });
    }
  });
  document.addEventListener("mousedown", (e) => {
    if (!pop.hidden && !e.target.closest("#popover") && !e.target.closest("mark.m")) closePopover();
    if (!seltool.hidden && !e.target.closest("#seltool")) hideSeltool();
  });

  // ------------------------------------------------ start
  $("#settings-btn").onclick = openSettings;
  window.addEventListener("hashchange", route);
  loadStatus().then(route);
  setInterval(loadStatus, 20000);
})();
