"""Blind A/B/C evaluator for the integrate shadow eval.

Walks every ``~/.deja/integrate_shadow/*.json`` record. For each cycle,
shows the signal batch that went in + three anonymized outputs (prod +
two shadows, labels A/B/C randomized per cycle). You pick the best.
After you've rated enough cycles, the Reveal button shows which label
mapped to which model and the aggregate win rates.

- All ratings autosave to browser localStorage — row-level, survives
  tab close.
- Per-cycle label → model mapping is persisted alongside the rating so
  the reveal works across reloads.
- Export JSONL button when you're done.

Usage:
    ./venv/bin/python tools/shadow_blind_eval.py
    ./venv/bin/python tools/shadow_blind_eval.py --port 9878
    ./venv/bin/python tools/shadow_blind_eval.py --since 2026-04-13
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SHADOW_DIR = Path.home() / ".deja" / "integrate_shadow"


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Deja — Blind Shadow Eval</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 13px/1.45 -apple-system, ui-sans-serif, system-ui, sans-serif;
    background: #0e0e10; color: #eaeaea;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    background: #1a1a1d; border-bottom: 1px solid #2a2a2e;
    padding: 10px 16px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  }
  header h1 { font-size: 15px; margin: 0 8px 0 0; }
  header button, header select, header input {
    background: #0e0e10; color: #eaeaea; border: 1px solid #2a2a2e;
    padding: 6px 10px; border-radius: 6px; font: inherit; cursor: pointer;
  }
  header button:hover:not(:disabled) { border-color: #4f8cff; }
  header button:disabled { opacity: 0.5; cursor: not-allowed; }
  header .stats { margin-left: auto; color: #8a8a90; font-variant-numeric: tabular-nums; }
  header .stats b { color: #eaeaea; }
  main { max-width: 1280px; margin: 0 auto; padding: 14px; }

  .cycle-meta {
    color: #8a8a90; font-family: ui-monospace, monospace; font-size: 11px;
    margin-bottom: 12px;
  }

  .signals-box {
    background: #1a1a1d; border: 1px solid #2a2a2e; border-radius: 8px;
    padding: 12px; margin-bottom: 14px; max-height: 260px; overflow-y: auto;
  }
  .signals-box h3 {
    font-size: 12px; color: #8a8a90; margin: 0 0 6px 0; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .signals-box pre {
    margin: 0; font-family: ui-monospace, monospace; font-size: 11px;
    color: #eaeaea; white-space: pre-wrap; word-break: break-word; line-height: 1.5;
  }

  .outputs { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-bottom: 14px; }
  .output-card {
    background: #1a1a1d; border: 1px solid #2a2a2e; border-radius: 8px;
    padding: 14px; display: flex; flex-direction: column; gap: 10px;
    min-height: 320px;
  }
  .output-card.selected { border-color: #3fb950; background: rgba(63,185,80,0.06); }
  .output-card .label {
    font-size: 22px; font-weight: 700; color: #4f8cff;
    font-family: ui-monospace, monospace;
  }
  .output-card .model-reveal {
    font-size: 11px; font-family: ui-monospace, monospace; color: #e3b341;
    background: rgba(227,179,65,0.1); padding: 3px 6px; border-radius: 3px;
    display: inline-block;
  }
  .output-card .reasoning {
    font-size: 12px; color: #c0c0c0; line-height: 1.5;
    white-space: pre-wrap; word-break: break-word;
  }
  .output-card .updates {
    font-family: ui-monospace, monospace; font-size: 11px; color: #eaeaea;
    background: #0e0e10; padding: 8px; border-radius: 4px; overflow-x: auto;
    white-space: pre-wrap; word-break: break-word;
  }
  .output-card .updates-label {
    font-size: 10px; color: #8a8a90; text-transform: uppercase; letter-spacing: 0.5px;
    margin-bottom: 4px; margin-top: 4px;
  }
  .output-card .empty { color: #555; font-size: 11px; font-style: italic; }
  .output-card .error { color: #f85149; font-size: 11px; }

  .ratings {
    display: flex; gap: 10px; align-items: center; padding: 14px;
    background: #1a1a1d; border: 1px solid #2a2a2e; border-radius: 8px;
  }
  .ratings button {
    background: #0e0e10; color: #eaeaea; border: 1px solid #2a2a2e;
    padding: 8px 16px; border-radius: 6px; cursor: pointer; font: inherit;
  }
  .ratings button:hover { border-color: #4f8cff; }
  .ratings button.active {
    background: #3fb950; color: #0a0a0a; border-color: #3fb950; font-weight: 600;
  }
  .ratings input[type=text] {
    flex: 1; background: #0e0e10; color: #eaeaea; border: 1px solid #2a2a2e;
    padding: 7px 10px; border-radius: 6px; font: inherit;
  }
  .nav {
    display: flex; gap: 10px; justify-content: space-between; margin-top: 14px;
  }
  .nav button {
    background: #0e0e10; color: #eaeaea; border: 1px solid #2a2a2e;
    padding: 8px 18px; border-radius: 6px; cursor: pointer; font: inherit;
  }
  .nav button:hover:not(:disabled) { border-color: #4f8cff; }
  .nav button:disabled { opacity: 0.4; cursor: not-allowed; }
  .nav button.primary { background: #2b4d8a; border-color: #4f8cff; }

  .reveal-pane {
    background: #1a1a1d; border: 1px solid #e3b341; border-radius: 8px;
    padding: 16px; margin-top: 18px;
  }
  .reveal-pane h2 { margin: 0 0 8px 0; font-size: 15px; color: #e3b341; }
  .reveal-pane table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  .reveal-pane th, .reveal-pane td {
    text-align: left; padding: 6px 10px; border-bottom: 1px solid #2a2a2e;
    font-family: ui-monospace, monospace; font-size: 12px;
  }
  .reveal-pane th { color: #8a8a90; font-weight: 600; }
</style>
</head>
<body>

<header>
  <h1>Blind Shadow Eval</h1>
  <button id="btnPrev">← prev</button>
  <span id="cycleIdx" style="font-family:ui-monospace,monospace;font-size:11px;color:#8a8a90;"></span>
  <button id="btnNext">next →</button>
  <button id="btnJumpNextUnrated">next unrated</button>

  <label>Filter:
    <select id="filterState">
      <option value="all">all</option>
      <option value="unrated" selected>unrated only</option>
      <option value="rated">rated only</option>
    </select>
  </label>

  <div class="stats"><span id="stats"></span></div>

  <button id="btnReveal">Reveal results</button>
  <button id="btnExport">Export JSONL</button>
  <button id="btnClear" style="color:#f85149;border-color:#4a2020;" title="Wipe all ratings">Clear</button>
</header>

<main>
  <div id="cycle-view">
    <div style="padding:60px;text-align:center;color:#8a8a90;">Loading…</div>
  </div>
  <div id="reveal-pane"></div>
</main>

<script>
(function() {
  const STORAGE_KEY = "deja.shadow-blind-eval.v1";

  // Storage shape:
  //   { <cycleFilename>: { verdict, note, rated_at, label_to_model: {A:..., B:..., C:...} } }
  function readAll() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); }
    catch(e) { return {}; }
  }
  function writeOne(id, rec) {
    const all = readAll();
    if (rec === null) delete all[id]; else all[id] = rec;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  }

  let cycles = [];  // raw list from server
  let annotations = readAll();
  let currentIndex = 0;
  let revealShown = false;

  async function loadCycles() {
    const res = await fetch("/api/cycles");
    if (!res.ok) throw new Error("load failed " + res.status);
    cycles = await res.json();
  }

  // Deterministic shuffle per cycle so reloading gives the same label→model
  // mapping. We store the mapping on first view; subsequent views reuse it.
  function labelMapping(cycle) {
    if (annotations[cycle.file]?.label_to_model) {
      return annotations[cycle.file].label_to_model;
    }
    // Derive a deterministic shuffle from the filename hash so the
    // order is stable even across reloads BEFORE a rating is saved.
    let h = 0;
    for (let i = 0; i < cycle.file.length; i++) {
      h = ((h << 5) - h + cycle.file.charCodeAt(i)) | 0;
    }
    const models = ["production", "shadow0", "shadow1"];
    // Simple Fisher-Yates seeded by h
    const rng = (() => { let s = h; return () => { s = (s*9301+49297) % 233280; return s / 233280; }; })();
    const shuffled = models.slice();
    for (let i = shuffled.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
    }
    return { A: shuffled[0], B: shuffled[1], C: shuffled[2] };
  }

  function getOutputForSlot(cycle, slot) {
    // slot is "A" | "B" | "C"; maps to one of production/shadow0/shadow1
    const mapping = labelMapping(cycle);
    const role = mapping[slot];
    if (role === "production") return { model: cycle.production?.model, out: cycle.production };
    if (role === "shadow0") return { model: cycle.shadows?.[0]?.model, out: cycle.shadows?.[0] };
    if (role === "shadow1") return { model: cycle.shadows?.[1]?.model, out: cycle.shadows?.[1] };
    return { model: null, out: null };
  }

  function renderOutput(cycle, slot) {
    const el = document.createElement("div");
    el.className = "output-card";
    const { model, out } = getOutputForSlot(cycle, slot);
    const a = annotations[cycle.file];
    const isSelected = a?.verdict === slot;
    if (isSelected) el.classList.add("selected");

    const labelRow = document.createElement("div");
    labelRow.style.display = "flex";
    labelRow.style.justifyContent = "space-between";
    labelRow.style.alignItems = "center";
    const lab = document.createElement("div");
    lab.className = "label";
    lab.textContent = slot;
    labelRow.appendChild(lab);

    if (revealShown && model) {
      const modelBadge = document.createElement("span");
      modelBadge.className = "model-reveal";
      modelBadge.textContent = model;
      labelRow.appendChild(modelBadge);
    }
    el.appendChild(labelRow);

    if (!out || out.error) {
      const err = document.createElement("div");
      err.className = "error";
      err.textContent = out?.error ? "error: " + out.error : "(no output — model unavailable)";
      el.appendChild(err);
      return el;
    }

    const reasoning = document.createElement("div");
    reasoning.className = "reasoning";
    reasoning.textContent = out.reasoning || "(no reasoning)";
    el.appendChild(reasoning);

    // Wiki updates
    const wikiUpdates = out.wiki_updates || [];
    if (wikiUpdates.length) {
      const lab2 = document.createElement("div");
      lab2.className = "updates-label";
      lab2.textContent = `wiki_updates (${wikiUpdates.length})`;
      el.appendChild(lab2);
      for (const u of wikiUpdates) {
        const card = document.createElement("div");
        card.className = "updates";
        let action = u.action || "?";
        let target = `${u.category || "?"}/${u.slug || "?"}`;
        let lines = `${action} ${target}`;
        if (u.reason) lines += `\nreason: ${u.reason}`;
        card.textContent = lines;
        el.appendChild(card);
      }
    } else {
      const lab2 = document.createElement("div");
      lab2.className = "empty";
      lab2.textContent = "no wiki_updates";
      el.appendChild(lab2);
    }

    // Goal actions
    const goals = out.goal_actions || [];
    if (goals.length) {
      const lab3 = document.createElement("div");
      lab3.className = "updates-label";
      lab3.textContent = `goal_actions (${goals.length})`;
      el.appendChild(lab3);
      for (const g of goals) {
        const card = document.createElement("div");
        card.className = "updates";
        card.textContent = `${g.type}: ${g.reason || ""}`;
        el.appendChild(card);
      }
    }

    // Tasks update summary
    const tu = out.tasks_update || {};
    const taskOps = [];
    for (const k of ["add_tasks","complete_tasks","archive_tasks","add_waiting","resolve_waiting","archive_waiting","add_reminders","resolve_reminders","archive_reminders"]) {
      if (tu[k] && tu[k].length) taskOps.push(`${k}(${tu[k].length})`);
    }
    if (taskOps.length) {
      const lab4 = document.createElement("div");
      lab4.className = "updates-label";
      lab4.textContent = "tasks_update";
      el.appendChild(lab4);
      const card = document.createElement("div");
      card.className = "updates";
      card.textContent = taskOps.join(", ");
      el.appendChild(card);
    }

    if (out.latency_ms) {
      const lat = document.createElement("div");
      lat.style.fontSize = "10px";
      lat.style.color = "#555";
      lat.style.fontFamily = "ui-monospace,monospace";
      lat.style.marginTop = "auto";
      lat.textContent = `${out.latency_ms} ms`;
      el.appendChild(lat);
    }

    return el;
  }

  function render() {
    const view = document.getElementById("cycle-view");
    const visible = getFilteredCycles();
    document.getElementById("cycleIdx").textContent =
      visible.length ? `${currentIndex + 1} / ${visible.length}` : "0 / 0";
    document.getElementById("btnPrev").disabled = currentIndex <= 0;
    document.getElementById("btnNext").disabled = currentIndex >= visible.length - 1;
    updateStats();

    if (!visible.length) {
      view.innerHTML = '<div style="padding:60px;text-align:center;color:#8a8a90;">No cycles match the filter.</div>';
      return;
    }

    const cycle = visible[currentIndex];
    view.innerHTML = "";

    const meta = document.createElement("div");
    meta.className = "cycle-meta";
    meta.textContent = `${cycle.timestamp || "?"} · ${cycle.file}`;
    view.appendChild(meta);

    // Signals that went in
    const sig = document.createElement("div");
    sig.className = "signals-box";
    sig.innerHTML = `<h3>Signals that went in</h3>`;
    const pre = document.createElement("pre");
    pre.textContent = cycle.signals_text || "(no signals_text)";
    sig.appendChild(pre);
    view.appendChild(sig);

    // Three outputs
    const outs = document.createElement("div");
    outs.className = "outputs";
    for (const slot of ["A", "B", "C"]) {
      const card = renderOutput(cycle, slot);
      card.addEventListener("click", (e) => {
        // Don't swallow button or input clicks if any exist inside
        if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return;
        selectVerdict(cycle, slot);
      });
      outs.appendChild(card);
    }
    view.appendChild(outs);

    // Rating controls
    const rate = document.createElement("div");
    rate.className = "ratings";
    const a = annotations[cycle.file] || {};
    const mkBtn = (letter, label) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (a.verdict === letter) b.classList.add("active");
      b.onclick = () => selectVerdict(cycle, letter);
      return b;
    };
    rate.appendChild(mkBtn("A", "A best"));
    rate.appendChild(mkBtn("B", "B best"));
    rate.appendChild(mkBtn("C", "C best"));
    rate.appendChild(mkBtn("tie", "tie"));
    rate.appendChild(mkBtn("none", "none good"));
    const note = document.createElement("input");
    note.type = "text";
    note.placeholder = "note (optional, auto-saves)";
    note.value = a.note || "";
    let noteTimer;
    note.addEventListener("input", () => {
      clearTimeout(noteTimer);
      noteTimer = setTimeout(() => {
        saveNote(cycle, note.value);
      }, 250);
    });
    rate.appendChild(note);
    view.appendChild(rate);

    // Nav
    const nav = document.createElement("div");
    nav.className = "nav";
    const prev = document.createElement("button");
    prev.textContent = "← prev";
    prev.disabled = currentIndex <= 0;
    prev.onclick = () => { currentIndex--; render(); };
    const next = document.createElement("button");
    next.textContent = "next →";
    next.className = "primary";
    next.disabled = currentIndex >= visible.length - 1;
    next.onclick = () => { currentIndex++; render(); };
    nav.appendChild(prev);
    nav.appendChild(next);
    view.appendChild(nav);
  }

  function getFilteredCycles() {
    const f = document.getElementById("filterState").value;
    if (f === "unrated") return cycles.filter(c => !annotations[c.file]?.verdict);
    if (f === "rated") return cycles.filter(c => annotations[c.file]?.verdict);
    return cycles;
  }

  function selectVerdict(cycle, letter) {
    // Persist the label→model mapping at rating time so the reveal works
    // later regardless of whether the deterministic shuffle changes.
    const mapping = labelMapping(cycle);
    const prev = annotations[cycle.file] || {};
    const rec = {
      verdict: letter,
      note: prev.note || "",
      rated_at: new Date().toISOString(),
      label_to_model: mapping,
      timestamp: cycle.timestamp,
    };
    annotations[cycle.file] = rec;
    writeOne(cycle.file, rec);
    render();
  }

  function saveNote(cycle, note) {
    const mapping = labelMapping(cycle);
    const prev = annotations[cycle.file] || {};
    const rec = { ...prev, note, label_to_model: mapping, timestamp: cycle.timestamp };
    annotations[cycle.file] = rec;
    writeOne(cycle.file, rec);
  }

  function updateStats() {
    const total = cycles.length;
    const rated = cycles.filter(c => annotations[c.file]?.verdict).length;
    document.getElementById("stats").innerHTML =
      `<b>${rated}</b> / <b>${total}</b> rated`;
  }

  // Reveal — compute win counts per model across rated cycles.
  function renderReveal() {
    const pane = document.getElementById("reveal-pane");
    revealShown = true;
    const rated = cycles.filter(c => annotations[c.file]?.verdict);
    if (!rated.length) {
      pane.innerHTML = '<div class="reveal-pane"><h2>No ratings yet</h2></div>';
      render();
      return;
    }

    const tally = {};  // model → {best: n, tie: n, none: n}
    let ties = 0, nones = 0;
    for (const c of rated) {
      const a = annotations[c.file];
      if (a.verdict === "tie") { ties++; continue; }
      if (a.verdict === "none") { nones++; continue; }
      const mapping = a.label_to_model || labelMapping(c);
      const role = mapping[a.verdict];
      let model = null;
      if (role === "production") model = c.production?.model;
      else if (role === "shadow0") model = c.shadows?.[0]?.model;
      else if (role === "shadow1") model = c.shadows?.[1]?.model;
      if (model) {
        tally[model] = (tally[model] || 0) + 1;
      }
    }

    let html = '<div class="reveal-pane"><h2>Results</h2>';
    html += `<p>${rated.length} cycles rated · ${ties} tie · ${nones} none good</p>`;
    html += '<table><tr><th>Model</th><th>Picked as best</th><th>Share</th></tr>';
    const sorted = Object.entries(tally).sort((a, b) => b[1] - a[1]);
    const picks = sorted.reduce((s, [, n]) => s + n, 0);
    for (const [model, n] of sorted) {
      const pct = picks ? Math.round(100 * n / picks) : 0;
      html += `<tr><td>${model}</td><td>${n}</td><td>${pct}%</td></tr>`;
    }
    html += '</table></div>';
    pane.innerHTML = html;

    render();  // re-render card with model badges now visible
  }

  function exportData() {
    const lines = [];
    for (const c of cycles) {
      const a = annotations[c.file];
      if (!a?.verdict) continue;
      const mapping = a.label_to_model || labelMapping(c);
      let picked_model = null;
      if (a.verdict === "A" || a.verdict === "B" || a.verdict === "C") {
        const role = mapping[a.verdict];
        if (role === "production") picked_model = c.production?.model;
        else if (role === "shadow0") picked_model = c.shadows?.[0]?.model;
        else if (role === "shadow1") picked_model = c.shadows?.[1]?.model;
      }
      lines.push(JSON.stringify({
        file: c.file,
        timestamp: c.timestamp,
        verdict: a.verdict,
        note: a.note || "",
        picked_model,
        label_to_model: mapping,
        rated_at: a.rated_at,
      }));
    }
    const blob = new Blob([lines.join("\n") + "\n"], { type: "application/jsonl" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `deja-blind-eval-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}.jsonl`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 200);
  }

  // Event wiring
  document.getElementById("btnPrev").onclick = () => { currentIndex--; render(); };
  document.getElementById("btnNext").onclick = () => { currentIndex++; render(); };
  document.getElementById("btnJumpNextUnrated").onclick = () => {
    const visible = getFilteredCycles();
    for (let i = currentIndex + 1; i < visible.length; i++) {
      if (!annotations[visible[i].file]?.verdict) { currentIndex = i; render(); return; }
    }
    // wrap around
    for (let i = 0; i <= currentIndex; i++) {
      if (!annotations[visible[i].file]?.verdict) { currentIndex = i; render(); return; }
    }
  };
  document.getElementById("filterState").onchange = () => { currentIndex = 0; render(); };
  document.getElementById("btnReveal").onclick = renderReveal;
  document.getElementById("btnExport").onclick = exportData;
  document.getElementById("btnClear").onclick = () => {
    if (!confirm("Wipe ALL ratings? Export first if you want to keep them.")) return;
    localStorage.removeItem(STORAGE_KEY);
    annotations = {};
    revealShown = false;
    document.getElementById("reveal-pane").innerHTML = "";
    render();
  };

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    const visible = getFilteredCycles();
    if (!visible.length) return;
    const cycle = visible[currentIndex];
    if (e.key === "a" || e.key === "A") selectVerdict(cycle, "A");
    else if (e.key === "b" || e.key === "B") selectVerdict(cycle, "B");
    else if (e.key === "c" || e.key === "C") selectVerdict(cycle, "C");
    else if (e.key === "t") selectVerdict(cycle, "tie");
    else if (e.key === "n") selectVerdict(cycle, "none");
    else if (e.key === "ArrowRight" || e.key === "j") {
      if (currentIndex < visible.length - 1) { currentIndex++; render(); }
    }
    else if (e.key === "ArrowLeft" || e.key === "k") {
      if (currentIndex > 0) { currentIndex--; render(); }
    }
  });

  loadCycles()
    .then(() => render())
    .catch(err => {
      document.getElementById("cycle-view").innerHTML =
        `<div style="padding:60px;text-align:center;color:#f85149;">Failed: ${err.message}</div>`;
    });
})();
</script>
</body>
</html>
"""


def _load_cycles(since: datetime | None) -> list[dict]:
    """Return all shadow records (newest-first), optionally filtered by date."""
    if not SHADOW_DIR.is_dir():
        return []
    rows: list[dict] = []
    for fp in sorted(SHADOW_DIR.glob("*.json")):
        try:
            rec = json.loads(fp.read_text())
        except Exception:
            continue
        ts = rec.get("timestamp", "")
        if since:
            try:
                if datetime.fromisoformat(ts) < since:
                    continue
            except ValueError:
                continue

        # Normalise old-schema (single-shadow) records to the new shape
        # so the UI only has one code path.
        shadows = rec.get("shadows")
        if not shadows:
            legacy = rec.get("shadow") or rec.get("flash")
            shadows = [legacy] if legacy else []

        # Skip cycles where fewer than 3 outputs exist — we need prod +
        # 2 shadows for a blind A/B/C comparison.
        if not rec.get("production") or len(shadows) < 2:
            continue

        rows.append({
            "file": fp.name,
            "timestamp": ts,
            "signals_text": rec.get("signals_text", ""),
            "production": rec.get("production"),
            "shadows": shadows,
        })
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return rows


class Handler(BaseHTTPRequestHandler):
    since_dt: datetime | None = None

    def log_message(self, *args, **kwargs):  # noqa: ARG002
        return

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/cycles":
            rows = _load_cycles(self.since_dt)
            body = json.dumps(rows).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9878)
    p.add_argument("--since", default=None, help="ISO date; only cycles >= this")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"bad --since: {args.since}", file=sys.stderr)
            return 1
    Handler.since_dt = since_dt

    count = len(_load_cycles(since_dt))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n  Deja blind shadow eval — {url}")
    print(f"  {count} cycles available for rating")
    print(f"  Keyboard: A/B/C to pick, T for tie, N for none, ←/→ or j/k to nav")
    print(f"  Ctrl-C to stop.\n")

    if not args.no_open:
        try: webbrowser.open(url)
        except Exception: pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
