"""Local web UI for annotating observations.

Runs a localhost HTTP server on port 9876 (or any --port), opens a page
that loads observations from ``~/.deja/observations.jsonl`` and lets you
label each as correct / incorrect / partial with an optional note.

Row-level autosave to browser localStorage — every verdict / note change
persists instantly, so a tab crash or accidental close never loses work.
The server is stateless; the browser owns the annotation dataset.

Export buttons:
    - JSON: the raw annotations dict, keyed by id_key
    - JSONL: one line per annotated observation (matches the CLI tool's
      file format, so imports into ``~/.deja/observation_annotations.jsonl``)
    - CSV: spreadsheet-friendly

Usage:
    ./venv/bin/python tools/annotate_web.py
    ./venv/bin/python tools/annotate_web.py --port 9000
    ./venv/bin/python tools/annotate_web.py --no-open

The page reads observations, not the server's annotation file — so this
tool is independent of the CLI annotator. Export JSONL and concat into
``~/.deja/observation_annotations.jsonl`` if you want the CLI ``--stats``
mode to see them.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

OBS_PATH = Path.home() / ".deja" / "observations.jsonl"


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Deja — Annotate Observations</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0e0e10;
    --panel: #1a1a1d;
    --border: #2a2a2e;
    --fg: #eaeaea;
    --muted: #8a8a90;
    --accent: #4f8cff;
    --good: #3fb950;
    --bad: #f85149;
    --warn: #e3b341;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 13px/1.45 -apple-system, ui-sans-serif, system-ui, sans-serif;
    background: var(--bg); color: var(--fg);
  }
  header {
    position: sticky; top: 0; background: var(--panel);
    border-bottom: 1px solid var(--border); padding: 10px 16px; z-index: 10;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  }
  header h1 { font-size: 15px; margin: 0 8px 0 0; }
  header select, header input, header button {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 6px 10px; border-radius: 6px; font: inherit;
  }
  header button { cursor: pointer; }
  header button:hover { border-color: var(--accent); }
  header .stats { margin-left: auto; color: var(--muted); font-variant-numeric: tabular-nums; }
  header .stats b { color: var(--fg); }
  main { max-width: 920px; margin: 0 auto; padding: 14px; }
  .row {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 10px; padding: 12px; display: flex; flex-direction: column; gap: 8px;
    transition: border-color 0.15s;
  }
  .row.v-correct { border-left: 3px solid var(--good); }
  .row.v-incorrect { border-left: 3px solid var(--bad); }
  .row.v-partial { border-left: 3px solid var(--warn); }
  .row .meta {
    display: flex; gap: 10px; font-size: 11px; color: var(--muted);
    font-family: ui-monospace, monospace; align-items: center; flex-wrap: wrap;
  }
  .row .meta .src {
    background: var(--border); padding: 2px 6px; border-radius: 3px; color: var(--fg);
  }
  .row .text {
    white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace;
    font-size: 12px; background: var(--bg); padding: 8px; border-radius: 4px;
    max-height: 360px; overflow-y: auto;
  }
  .row .text.collapsed { max-height: 120px; position: relative; }
  .row .text.collapsed::after {
    content: ""; position: absolute; bottom: 0; left: 0; right: 0; height: 40px;
    background: linear-gradient(transparent, var(--bg));
    pointer-events: none;
  }
  .row .toggle {
    background: none; border: none; color: var(--accent); cursor: pointer;
    font-size: 12px; align-self: flex-start; padding: 0; text-decoration: underline;
  }
  .row .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .row .controls button {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font: inherit;
  }
  .row .controls button:hover { border-color: var(--accent); }
  .row .controls button.active.y { background: var(--good); color: #0a0a0a; border-color: var(--good); }
  .row .controls button.active.n { background: var(--bad); color: #fff; border-color: var(--bad); }
  .row .controls button.active.p { background: var(--warn); color: #0a0a0a; border-color: var(--warn); }
  .row .controls input[type=text] {
    flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 6px 10px; border-radius: 6px; font: inherit; min-width: 180px;
  }
  .row .saved { color: var(--muted); font-size: 11px; }
  .row .saved.show { color: var(--good); }
  .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
  .toolbar-spacer { flex: 1; }
  .export-group { display: flex; gap: 6px; border-left: 1px solid var(--border); padding-left: 14px; margin-left: 6px; }
  .danger:hover { border-color: var(--bad) !important; color: var(--bad); }
</style>
</head>
<body>

<header>
  <h1>Annotate observations</h1>

  <label>Source:
    <select id="filterSource"><option value="">all</option></select>
  </label>

  <label>Show:
    <select id="filterStatus">
      <option value="unannotated">unannotated</option>
      <option value="all">all</option>
      <option value="annotated">annotated only</option>
      <option value="incorrect">incorrect only</option>
      <option value="partial">partial only</option>
    </select>
  </label>

  <label>Limit:
    <select id="filterLimit">
      <option value="50">50</option>
      <option value="100" selected>100</option>
      <option value="250">250</option>
      <option value="500">500</option>
      <option value="0">all</option>
    </select>
  </label>

  <input type="text" id="filterMatch" placeholder="text contains…" style="width:180px;">

  <div class="stats"><span id="stats"></span></div>

  <div class="export-group">
    <button id="btnExportJSONL">Export JSONL</button>
    <button id="btnExportJSON">JSON</button>
    <button id="btnExportCSV">CSV</button>
    <button id="btnClear" class="danger" title="Wipe all annotations from browser storage">Clear</button>
  </div>
</header>

<main id="main">
  <div class="empty">Loading…</div>
</main>

<script>
(function() {
  const STORAGE_KEY = "deja.annotations.v1";

  // --------- Annotation storage (localStorage, row-level atomic) ---------
  function readAll() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (e) { return {}; }
  }
  function writeOne(id_key, record) {
    const all = readAll();
    if (record === null) {
      delete all[id_key];
    } else {
      all[id_key] = record;
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  }

  // --------- Fetch observations from local server ---------
  let observations = [];
  let annotations = readAll();

  async function loadObservations() {
    const res = await fetch("/api/observations");
    if (!res.ok) throw new Error("failed to load: " + res.status);
    observations = await res.json();
  }

  // --------- Filters ---------
  function populateSourceFilter() {
    const sources = Array.from(new Set(observations.map(o => o.source))).sort();
    const el = document.getElementById("filterSource");
    for (const s of sources) {
      const opt = document.createElement("option");
      opt.value = s; opt.textContent = s;
      el.appendChild(opt);
    }
  }

  function filtered() {
    const src = document.getElementById("filterSource").value;
    const stat = document.getElementById("filterStatus").value;
    const limitEl = document.getElementById("filterLimit").value;
    const limit = parseInt(limitEl, 10);
    const match = document.getElementById("filterMatch").value.toLowerCase();

    let rows = observations.slice().sort((a, b) =>
      (b.timestamp || "").localeCompare(a.timestamp || "")
    );
    if (src) rows = rows.filter(r => r.source === src);
    if (match) rows = rows.filter(r => (r.text || "").toLowerCase().includes(match));
    if (stat === "unannotated") rows = rows.filter(r => !annotations[r.id_key]);
    else if (stat === "annotated") rows = rows.filter(r => annotations[r.id_key]);
    else if (stat === "incorrect") rows = rows.filter(r => annotations[r.id_key]?.verdict === "incorrect");
    else if (stat === "partial") rows = rows.filter(r => annotations[r.id_key]?.verdict === "partial");
    if (limit > 0) rows = rows.slice(0, limit);
    return rows;
  }

  // --------- Rendering ---------
  function updateStats() {
    const counts = { correct: 0, partial: 0, incorrect: 0 };
    for (const a of Object.values(annotations)) {
      if (counts[a.verdict] !== undefined) counts[a.verdict]++;
    }
    const total = counts.correct + counts.partial + counts.incorrect;
    const bad = counts.partial + counts.incorrect;
    const pct = total ? Math.round(100 * bad / total) : 0;
    document.getElementById("stats").innerHTML =
      `<b>${total}</b> annotated · <b>${counts.correct}</b> correct · <b>${counts.incorrect}</b> incorrect · <b>${counts.partial}</b> partial · <b>${pct}%</b> bad`;
  }

  function renderRow(obs) {
    const a = annotations[obs.id_key] || {};
    const el = document.createElement("div");
    el.className = "row" + (a.verdict ? " v-" + a.verdict : "");
    el.dataset.id = obs.id_key;

    const textEl = document.createElement("div");
    textEl.className = "text collapsed";
    textEl.textContent = obs.text || "";

    const toggle = document.createElement("button");
    toggle.className = "toggle";
    toggle.textContent = "expand";
    toggle.onclick = () => {
      textEl.classList.toggle("collapsed");
      toggle.textContent = textEl.classList.contains("collapsed") ? "expand" : "collapse";
    };

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `
      <span class="src">${obs.source || "?"}</span>
      <span>${obs.sender || ""}</span>
      <span>${obs.timestamp || ""}</span>
      <span style="margin-left:auto;opacity:0.5">${obs.id_key || ""}</span>
    `;

    const controls = document.createElement("div");
    controls.className = "controls";

    function mkBtn(letter, verdict) {
      const b = document.createElement("button");
      b.textContent = letter.toUpperCase();
      b.className = letter + (a.verdict === verdict ? " active" : "");
      b.title = verdict;
      b.onclick = () => setVerdict(obs.id_key, verdict);
      return b;
    }
    const btnY = mkBtn("y", "correct");
    const btnN = mkBtn("n", "incorrect");
    const btnP = mkBtn("p", "partial");

    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.placeholder = "note (auto-saves)";
    noteInput.value = a.note || "";
    let noteTimer;
    noteInput.addEventListener("input", () => {
      clearTimeout(noteTimer);
      noteTimer = setTimeout(() => saveNote(obs.id_key, noteInput.value, savedLabel), 250);
    });

    const clearBtn = document.createElement("button");
    clearBtn.textContent = "×";
    clearBtn.className = "danger";
    clearBtn.title = "Remove annotation for this row";
    clearBtn.onclick = () => clearAnnotation(obs.id_key);

    const savedLabel = document.createElement("span");
    savedLabel.className = "saved";

    controls.appendChild(btnY);
    controls.appendChild(btnN);
    controls.appendChild(btnP);
    controls.appendChild(noteInput);
    controls.appendChild(clearBtn);
    controls.appendChild(savedLabel);

    el.appendChild(meta);
    el.appendChild(textEl);
    el.appendChild(toggle);
    el.appendChild(controls);
    return el;
  }

  function render() {
    const main = document.getElementById("main");
    main.innerHTML = "";
    const rows = filtered();
    if (!rows.length) {
      main.innerHTML = '<div class="empty">Nothing matches the current filter.</div>';
    } else {
      for (const obs of rows) main.appendChild(renderRow(obs));
    }
    updateStats();
  }

  // --------- Mutations ---------
  function makeRecord(obs, verdict, note) {
    return {
      id_key: obs.id_key,
      source: obs.source,
      sender: obs.sender,
      timestamp: obs.timestamp,
      verdict,
      note: note || "",
      text_preview: (obs.text || "").slice(0, 200),
      annotated_at: new Date().toISOString(),
    };
  }

  function findObs(id_key) {
    return observations.find(o => o.id_key === id_key);
  }

  function setVerdict(id_key, verdict) {
    const obs = findObs(id_key);
    if (!obs) return;
    const existing = annotations[id_key] || {};
    const rec = makeRecord(obs, verdict, existing.note || "");
    annotations[id_key] = rec;
    writeOne(id_key, rec);
    // Refresh just this row (preserve scroll position)
    const node = document.querySelector(`.row[data-id="${CSS.escape(id_key)}"]`);
    if (node) node.replaceWith(renderRow(obs));
    updateStats();
    flashSaved(id_key);
  }

  function saveNote(id_key, note, labelEl) {
    const obs = findObs(id_key);
    if (!obs) return;
    const existing = annotations[id_key];
    if (!existing) {
      // Noting without a verdict is fine — treat as partial until user picks one
      return;  // or bail out to avoid silent state
    }
    const rec = { ...existing, note, annotated_at: new Date().toISOString() };
    annotations[id_key] = rec;
    writeOne(id_key, rec);
    updateStats();
    if (labelEl) { labelEl.textContent = "saved"; labelEl.classList.add("show");
      setTimeout(() => labelEl.classList.remove("show"), 1200); }
  }

  function clearAnnotation(id_key) {
    delete annotations[id_key];
    writeOne(id_key, null);
    const obs = findObs(id_key);
    const node = document.querySelector(`.row[data-id="${CSS.escape(id_key)}"]`);
    const filter = document.getElementById("filterStatus").value;
    if (filter === "annotated" || filter === "incorrect" || filter === "partial") {
      node?.remove();
    } else if (node && obs) {
      node.replaceWith(renderRow(obs));
    }
    updateStats();
  }

  function flashSaved(id_key) {
    const node = document.querySelector(`.row[data-id="${CSS.escape(id_key)}"] .saved`);
    if (!node) return;
    node.textContent = "saved";
    node.classList.add("show");
    setTimeout(() => node.classList.remove("show"), 1000);
  }

  // --------- Exports ---------
  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
  }
  function ts() {
    return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  }
  document.getElementById("btnExportJSON").onclick = () => {
    download(`deja-annotations-${ts()}.json`, JSON.stringify(annotations, null, 2), "application/json");
  };
  document.getElementById("btnExportJSONL").onclick = () => {
    const lines = Object.values(annotations).map(r => JSON.stringify(r));
    download(`deja-annotations-${ts()}.jsonl`, lines.join("\n") + "\n", "application/jsonl");
  };
  document.getElementById("btnExportCSV").onclick = () => {
    const header = ["id_key","source","sender","timestamp","verdict","note","text_preview","annotated_at"];
    const esc = (v) => {
      const s = (v ?? "").toString();
      return /[",\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
    };
    const rows = Object.values(annotations).map(r => header.map(k => esc(r[k])).join(","));
    download(`deja-annotations-${ts()}.csv`, header.join(",") + "\n" + rows.join("\n") + "\n", "text/csv");
  };
  document.getElementById("btnClear").onclick = () => {
    if (!confirm("Wipe ALL annotations from this browser? Export first if you want to keep them.")) return;
    localStorage.removeItem(STORAGE_KEY);
    annotations = {};
    render();
  };

  // --------- Wire filters ---------
  ["filterSource","filterStatus","filterLimit","filterMatch"].forEach(id => {
    document.getElementById(id).addEventListener("input", render);
    document.getElementById(id).addEventListener("change", render);
  });

  // --------- Init ---------
  loadObservations()
    .then(() => { populateSourceFilter(); render(); })
    .catch(err => {
      document.getElementById("main").innerHTML =
        `<div class="empty">Failed to load observations: ${err.message}</div>`;
    });
})();
</script>

</body>
</html>
"""


def _load_observations() -> list[dict]:
    if not OBS_PATH.exists():
        return []
    rows: list[dict] = []
    for line in OBS_PATH.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("id_key"):
                rows.append(rec)
        except Exception:
            continue
    return rows


class Handler(BaseHTTPRequestHandler):
    # Quiet logs — only errors surface
    def log_message(self, format, *args):  # noqa: A002 — BaseHTTPRequestHandler signature
        if "200" not in (args[1] if len(args) > 1 else ""):
            sys.stderr.write(f"  {self.address_string()} - {format % args}\n")

    def do_GET(self):  # noqa: N802 — required name
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/observations":
            rows = _load_observations()
            body = json.dumps(rows).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9876)
    p.add_argument("--no-open", action="store_true", help="don't auto-open browser")
    args = p.parse_args()

    if not OBS_PATH.exists():
        print(f"observations not found at {OBS_PATH}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n  Deja annotate — {url}")
    print(f"  Observations: {OBS_PATH} ({len(_load_observations())} rows)")
    print(f"  Annotations are stored in your browser's localStorage.")
    print(f"  Use the Export buttons to save them to a file.\n")
    print(f"  Ctrl-C to stop.\n")

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
