"""Local web UI for annotating wiki writes (agent actions).

For each write in ``~/.deja/audit.jsonl`` — event_create, wiki_write,
task_add, etc. — show the action, its reason, and the signals that went
into the cycle that produced it. Annotate each write as correct /
partial / incorrect with a note. Row-level autosave to browser
localStorage.

"Signals that went in" = observations whose timestamp falls in the
``[cycle_ts - window, cycle_ts]`` window (default 6 minutes, matching
the steady-state integrate interval). Approximate but close enough —
the true batch is whatever signals the scheduler flushed that cycle,
and we don't persist the batch anywhere. If this proves too noisy, we
can start writing the actual batch to disk.

Usage:
    ./venv/bin/python tools/annotate_wiki_writes.py
    ./venv/bin/python tools/annotate_wiki_writes.py --port 9877
    ./venv/bin/python tools/annotate_wiki_writes.py --window 10

Exports:
    JSONL / JSON / CSV — same contract as annotate_web.py, keyed by
    ``audit_id`` (synthesized hash of ts+cycle+target since audit
    entries have no natural primary key).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEJA = Path.home() / ".deja"
AUDIT_PATH = DEJA / "audit.jsonl"
OBS_PATH = DEJA / "observations.jsonl"


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Deja — Annotate Wiki Writes</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0e0e10;
    --panel: #1a1a1d;
    --panel-2: #22222a;
    --border: #2a2a2e;
    --fg: #eaeaea;
    --muted: #8a8a90;
    --accent: #4f8cff;
    --good: #3fb950;
    --bad: #f85149;
    --warn: #e3b341;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.45 -apple-system, ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--fg); }
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
  main { max-width: 1080px; margin: 0 auto; padding: 14px; }
  .write {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 12px; padding: 12px; display: flex; flex-direction: column; gap: 8px;
  }
  .write.v-correct { border-left: 3px solid var(--good); }
  .write.v-incorrect { border-left: 3px solid var(--bad); }
  .write.v-partial { border-left: 3px solid var(--warn); }
  .write .meta {
    display: flex; gap: 10px; font-size: 11px; color: var(--muted);
    font-family: ui-monospace, monospace; align-items: center; flex-wrap: wrap;
  }
  .write .meta .action {
    background: var(--border); padding: 2px 6px; border-radius: 3px; color: var(--fg);
    font-weight: 600; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px;
  }
  .write .target {
    font-family: ui-monospace, monospace; font-size: 13px; color: var(--accent);
    word-break: break-all;
  }
  .write .reason {
    background: var(--bg); padding: 8px 10px; border-radius: 4px;
    border-left: 2px solid var(--accent);
    white-space: pre-wrap; word-break: break-word;
  }
  .write .signals-box {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 10px; font-size: 12px;
  }
  .write .signals-box summary {
    cursor: pointer; color: var(--muted); font-size: 12px; user-select: none;
  }
  .write .signals-box summary:hover { color: var(--fg); }
  .write .signals-box .sig {
    background: var(--bg); padding: 6px 8px; border-radius: 4px; margin-top: 8px;
    font-family: ui-monospace, monospace; font-size: 11px;
    white-space: pre-wrap; word-break: break-word; color: var(--fg);
    max-height: 220px; overflow-y: auto;
  }
  .write .signals-box .sig .sig-head {
    color: var(--muted); font-size: 10px; margin-bottom: 4px;
  }
  .write .signals-box .sig .sig-head .src {
    background: var(--border); padding: 1px 5px; border-radius: 2px;
    color: var(--fg); margin-right: 6px;
  }
  .write .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .write .controls button {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font: inherit;
  }
  .write .controls button:hover { border-color: var(--accent); }
  .write .controls button.active.y { background: var(--good); color: #0a0a0a; border-color: var(--good); }
  .write .controls button.active.n { background: var(--bad); color: #fff; border-color: var(--bad); }
  .write .controls button.active.p { background: var(--warn); color: #0a0a0a; border-color: var(--warn); }
  .write .controls input[type=text] {
    flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 6px 10px; border-radius: 6px; font: inherit; min-width: 180px;
  }
  .write .saved { color: var(--muted); font-size: 11px; }
  .write .saved.show { color: var(--good); }
  .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
  .export-group { display: flex; gap: 6px; border-left: 1px solid var(--border); padding-left: 14px; margin-left: 6px; }
  .danger:hover { border-color: var(--bad) !important; color: var(--bad); }
  .obsidian-link { color: var(--accent); text-decoration: none; font-size: 11px; }
  .obsidian-link:hover { text-decoration: underline; }
</style>
</head>
<body>

<header>
  <h1>Annotate wiki writes</h1>

  <label>Action:
    <select id="filterAction"><option value="">all</option></select>
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
      <option value="50" selected>50</option>
      <option value="100">100</option>
      <option value="250">250</option>
      <option value="500">500</option>
      <option value="0">all</option>
    </select>
  </label>

  <label>Input grade:
    <select id="filterTier">
      <option value="">any</option>
      <option value="has_t1">has Tier 1</option>
      <option value="only_t3">only Tier 3 (ambient)</option>
      <option value="no_signals">no signals joined</option>
    </select>
  </label>

  <input type="text" id="filterMatch" placeholder="target / reason contains…" style="width:220px;">

  <div class="stats"><span id="stats"></span></div>

  <div class="export-group">
    <button id="btnExportJSONL">Export JSONL</button>
    <button id="btnExportJSON">JSON</button>
    <button id="btnExportCSV">CSV</button>
    <button id="btnClear" class="danger" title="Wipe all annotations">Clear</button>
  </div>
</header>

<main id="main">
  <div class="empty">Loading…</div>
</main>

<script>
(function() {
  const STORAGE_KEY = "deja.wiki-write-annotations.v1";

  function readAll() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function writeOne(id, rec) {
    const all = readAll();
    if (rec === null) delete all[id]; else all[id] = rec;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  }

  let writes = [];
  let annotations = readAll();

  async function loadWrites() {
    const res = await fetch("/api/writes");
    if (!res.ok) throw new Error("failed: " + res.status);
    writes = await res.json();
  }

  function populateActionFilter() {
    const actions = Array.from(new Set(writes.map(w => w.action).filter(Boolean))).sort();
    const el = document.getElementById("filterAction");
    for (const a of actions) {
      const opt = document.createElement("option");
      opt.value = a; opt.textContent = a;
      el.appendChild(opt);
    }
  }

  function filtered() {
    const act = document.getElementById("filterAction").value;
    const stat = document.getElementById("filterStatus").value;
    const limit = parseInt(document.getElementById("filterLimit").value, 10);
    const match = document.getElementById("filterMatch").value.toLowerCase();
    const tier = document.getElementById("filterTier").value;

    let rows = writes.slice().sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
    if (act) rows = rows.filter(r => r.action === act);
    if (match) rows = rows.filter(r =>
      ((r.target || "") + " " + (r.reason || "")).toLowerCase().includes(match)
    );
    if (stat === "unannotated") rows = rows.filter(r => !annotations[r.audit_id]);
    else if (stat === "annotated") rows = rows.filter(r => annotations[r.audit_id]);
    else if (stat === "incorrect") rows = rows.filter(r => annotations[r.audit_id]?.verdict === "incorrect");
    else if (stat === "partial") rows = rows.filter(r => annotations[r.audit_id]?.verdict === "partial");
    if (tier === "has_t1") rows = rows.filter(r => (r.tier_counts?.t1 || 0) > 0);
    else if (tier === "only_t3") rows = rows.filter(r => {
      const tc = r.tier_counts || {};
      return (tc.t1 || 0) === 0 && (tc.t2 || 0) === 0 && (tc.t3 || 0) > 0;
    });
    else if (tier === "no_signals") rows = rows.filter(r => (r.signals || []).length === 0);
    if (limit > 0) rows = rows.slice(0, limit);
    return rows;
  }

  function updateStats() {
    const c = { correct: 0, partial: 0, incorrect: 0 };
    for (const a of Object.values(annotations)) if (c[a.verdict] !== undefined) c[a.verdict]++;
    const t = c.correct + c.partial + c.incorrect;
    const bad = c.partial + c.incorrect;
    const pct = t ? Math.round(100 * bad / t) : 0;
    document.getElementById("stats").innerHTML =
      `<b>${t}</b> annotated · <b>${c.correct}</b> ok · <b>${c.incorrect}</b> bad · <b>${c.partial}</b> partial · <b>${pct}%</b> bad`;
  }

  function tierBadge(tier) {
    // Compact coloured chip matching integrate's tier meaning.
    const t = tier;
    const label = t === 1 ? "T1 voice"
                : t === 2 ? "T2 attention"
                : t === 3 ? "T3 ambient"
                : "T?";
    const bg = t === 1 ? "#2b7a3e"   // green — high-grade
             : t === 2 ? "#8a6a00"   // amber — medium
             : t === 3 ? "#5a2020"   // muted red — low-grade
             : "#444";
    return `<span style="background:${bg};color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;letter-spacing:0.3px;">${label}</span>`;
  }

  function renderSignal(sig) {
    const el = document.createElement("div");
    el.className = "sig";
    const head = document.createElement("div");
    head.className = "sig-head";
    head.innerHTML = `${tierBadge(sig.tier)} <span class="src">${sig.source || "?"}</span>${sig.sender || ""} · ${sig.timestamp || ""}`;
    const body = document.createElement("div");
    let text = sig.text || "";
    if (text.length > 500) text = text.slice(0, 500) + `\n… [+${sig.text.length - 500} more chars]`;
    body.textContent = text;
    el.appendChild(head);
    el.appendChild(body);
    return el;
  }

  function tierSummaryHTML(tc) {
    if (!tc) return "";
    const parts = [];
    if (tc.t1) parts.push(`<span style="color:#3fb950;font-weight:600">${tc.t1}×T1</span>`);
    if (tc.t2) parts.push(`<span style="color:#e3b341;font-weight:600">${tc.t2}×T2</span>`);
    if (tc.t3) parts.push(`<span style="color:#aa6060">${tc.t3}×T3</span>`);
    if (tc.unknown) parts.push(`<span style="color:var(--muted)">${tc.unknown}×?</span>`);
    return parts.join(" · ");
  }

  function renderWrite(w) {
    const a = annotations[w.audit_id] || {};
    const el = document.createElement("div");
    el.className = "write" + (a.verdict ? " v-" + a.verdict : "");
    el.dataset.id = w.audit_id;

    // Meta row
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `
      <span class="action">${w.action || "?"}</span>
      <span>${w.ts || ""}</span>
      <span>cycle=${w.cycle || "?"}</span>
      <span>trigger=${(w.trigger?.kind || "") + (w.trigger?.detail ? " · " + w.trigger.detail : "")}</span>
      <span style="margin-left:auto;opacity:0.5">${w.audit_id}</span>
    `;

    // Target
    const target = document.createElement("div");
    target.className = "target";
    target.textContent = w.target || "(no target)";

    // Reason
    const reason = document.createElement("div");
    reason.className = "reason";
    reason.textContent = w.reason || "(no reason given)";

    // Signals
    const sigBox = document.createElement("details");
    sigBox.className = "signals-box";
    const sigSummary = document.createElement("summary");
    const sigCount = (w.signals || []).length;
    const tierSum = tierSummaryHTML(w.tier_counts);
    const sourceLabel = w.signals_source === "exact"
      ? `<span style="color:var(--good)">●</span> exact`
      : `<span style="color:var(--warn)">●</span> window (${w.window_min}m)`;
    sigSummary.innerHTML = `${sourceLabel} · ${sigCount} signal${sigCount === 1 ? "" : "s"}${tierSum ? " — " + tierSum : ""}`;
    sigBox.appendChild(sigSummary);
    for (const s of (w.signals || [])) sigBox.appendChild(renderSignal(s));

    // Controls
    const controls = document.createElement("div");
    controls.className = "controls";

    function mkBtn(letter, verdict) {
      const b = document.createElement("button");
      b.textContent = letter.toUpperCase();
      b.className = letter + (a.verdict === verdict ? " active" : "");
      b.title = verdict;
      b.onclick = () => setVerdict(w.audit_id, verdict);
      return b;
    }
    const savedLabel = document.createElement("span");
    savedLabel.className = "saved";

    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.placeholder = "note (auto-saves)";
    noteInput.value = a.note || "";
    let noteTimer;
    noteInput.addEventListener("input", () => {
      clearTimeout(noteTimer);
      noteTimer = setTimeout(() => saveNote(w.audit_id, noteInput.value, savedLabel), 250);
    });

    const clearBtn = document.createElement("button");
    clearBtn.textContent = "×";
    clearBtn.className = "danger";
    clearBtn.title = "Remove annotation";
    clearBtn.onclick = () => clearAnnotation(w.audit_id);

    controls.appendChild(mkBtn("y", "correct"));
    controls.appendChild(mkBtn("n", "incorrect"));
    controls.appendChild(mkBtn("p", "partial"));
    controls.appendChild(noteInput);
    controls.appendChild(clearBtn);
    controls.appendChild(savedLabel);

    el.appendChild(meta);
    el.appendChild(target);
    el.appendChild(reason);
    el.appendChild(sigBox);
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
      for (const w of rows) main.appendChild(renderWrite(w));
    }
    updateStats();
  }

  function findWrite(id) { return writes.find(w => w.audit_id === id); }

  function makeRecord(w, verdict, note) {
    return {
      audit_id: w.audit_id,
      action: w.action, target: w.target,
      ts: w.ts, cycle: w.cycle,
      verdict, note: note || "",
      reason_preview: (w.reason || "").slice(0, 200),
      signal_count: (w.signals || []).length,
      annotated_at: new Date().toISOString(),
    };
  }

  function setVerdict(id, verdict) {
    const w = findWrite(id); if (!w) return;
    const rec = makeRecord(w, verdict, (annotations[id] || {}).note || "");
    annotations[id] = rec; writeOne(id, rec);
    const node = document.querySelector(`.write[data-id="${CSS.escape(id)}"]`);
    if (node) node.replaceWith(renderWrite(w));
    updateStats();
    flashSaved(id);
  }

  function saveNote(id, note, labelEl) {
    const w = findWrite(id); if (!w) return;
    const prev = annotations[id];
    if (!prev) return;
    annotations[id] = { ...prev, note, annotated_at: new Date().toISOString() };
    writeOne(id, annotations[id]);
    updateStats();
    if (labelEl) {
      labelEl.textContent = "saved"; labelEl.classList.add("show");
      setTimeout(() => labelEl.classList.remove("show"), 1000);
    }
  }

  function clearAnnotation(id) {
    delete annotations[id]; writeOne(id, null);
    const w = findWrite(id);
    const filter = document.getElementById("filterStatus").value;
    const node = document.querySelector(`.write[data-id="${CSS.escape(id)}"]`);
    if (filter === "annotated" || filter === "incorrect" || filter === "partial") node?.remove();
    else if (node && w) node.replaceWith(renderWrite(w));
    updateStats();
  }

  function flashSaved(id) {
    const n = document.querySelector(`.write[data-id="${CSS.escape(id)}"] .saved`);
    if (!n) return;
    n.textContent = "saved"; n.classList.add("show");
    setTimeout(() => n.classList.remove("show"), 1000);
  }

  // Exports
  function download(name, text, mime) {
    const blob = new Blob([text], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
  }
  function tsStr() { return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19); }
  document.getElementById("btnExportJSON").onclick = () => {
    download(`deja-wiki-write-annotations-${tsStr()}.json`, JSON.stringify(annotations, null, 2), "application/json");
  };
  document.getElementById("btnExportJSONL").onclick = () => {
    const lines = Object.values(annotations).map(r => JSON.stringify(r));
    download(`deja-wiki-write-annotations-${tsStr()}.jsonl`, lines.join("\n") + "\n", "application/jsonl");
  };
  document.getElementById("btnExportCSV").onclick = () => {
    const header = ["audit_id","action","target","ts","cycle","verdict","note","reason_preview","signal_count","annotated_at"];
    const esc = v => { const s = (v ?? "").toString(); return /[",\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s; };
    const rows = Object.values(annotations).map(r => header.map(k => esc(r[k])).join(","));
    download(`deja-wiki-write-annotations-${tsStr()}.csv`, header.join(",") + "\n" + rows.join("\n") + "\n", "text/csv");
  };
  document.getElementById("btnClear").onclick = () => {
    if (!confirm("Wipe ALL wiki-write annotations from this browser?")) return;
    localStorage.removeItem(STORAGE_KEY); annotations = {}; render();
  };

  ["filterAction","filterStatus","filterLimit","filterMatch","filterTier"].forEach(id => {
    document.getElementById(id).addEventListener("input", render);
    document.getElementById(id).addEventListener("change", render);
  });

  loadWrites()
    .then(() => { populateActionFilter(); render(); })
    .catch(err => {
      document.getElementById("main").innerHTML =
        `<div class="empty">Failed: ${err.message}</div>`;
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
            if rec.get("timestamp"):
                rows.append(rec)
        except Exception:
            continue
    return rows


def _load_writes(window_min: int) -> list[dict]:
    """Parse audit.jsonl, enrich each write with the signals in its
    preceding time window. Skips non-write entries (e.g. read-only
    audit rows, if any). Every attached signal is tier-classified via
    ``deja.signals.classify_tier`` so the UI can show the grade of
    input that led to each write."""
    if not AUDIT_PATH.exists():
        return []

    # Import the live tier classifier so the annotation view shows
    # exactly what integrate saw. Graceful fallback if the module can't
    # load — signals render without tier info.
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from deja.signals.tiering import classify_tier, reset_caches
        reset_caches()  # pick up any post-restart changes to self-page / inner-circle
    except Exception:
        classify_tier = None  # type: ignore

    def _classify(o: dict) -> dict:
        """Return a copy of the observation with a ``tier`` key added."""
        out = dict(o)
        try:
            if classify_tier is not None:
                out["tier"] = int(classify_tier(o))
        except Exception:
            pass
        return out

    obs_raw = _load_observations()
    obs = [_classify(o) for o in obs_raw]
    # Pre-index observations by timestamp for fast window slicing.
    obs_sorted = sorted(
        [(o.get("timestamp", ""), o) for o in obs if o.get("timestamp")],
        key=lambda x: x[0],
    )
    # Also index by id_key so writes that logged their exact signal ids
    # can skip the time-window heuristic entirely (precise joins).
    obs_by_id: dict[str, dict] = {o["id_key"]: o for o in obs if o.get("id_key")}

    def signals_before(ts_str: str) -> list[dict]:
        try:
            t_end = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return []
        t_start = t_end - timedelta(minutes=window_min)
        out = []
        for ts, o in obs_sorted:
            try:
                t = datetime.fromisoformat(ts)
                if t.tzinfo is None:
                    # observations.jsonl writes naive ISO; treat as UTC for comparison
                    from datetime import timezone
                    t = t.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if t_start <= t <= t_end:
                out.append(o)
        return out

    rows: list[dict] = []
    for line in AUDIT_PATH.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue

        action = rec.get("action")
        if not action:
            continue
        # Skip non-decision rows: health_check is startup plumbing,
        # cycle_no_op records a cycle that intentionally did nothing
        # (useful aggregate stat, not an annotation target),
        # voice_transcript is a raw signal not an agent decision.
        if action in {"health_check", "cycle_no_op", "voice_transcript"}:
            continue

        ts = rec.get("ts", "")
        audit_id = hashlib.sha1(
            f"{ts}|{rec.get('cycle','')}|{action}|{rec.get('target','')}".encode()
        ).hexdigest()[:16]

        # Prefer the exact signal ids recorded at cycle seed time when
        # present (audit.set_signals path). Fall back to the time-window
        # heuristic for rows logged before that was wired up.
        sids = rec.get("signal_ids") or []
        if sids:
            signals = [obs_by_id[sid] for sid in sids if sid in obs_by_id]
            source = "exact"
        else:
            signals = signals_before(ts)
            source = "window"

        # Tier distribution across the signals that fed this write —
        # the single most useful per-write summary for understanding
        # input grade. If no signals joined (e.g. startup rows), tier
        # counts are zeros.
        tier_counts = {1: 0, 2: 0, 3: 0, "unknown": 0}
        for s in signals:
            t = s.get("tier")
            if t in (1, 2, 3):
                tier_counts[t] += 1
            else:
                tier_counts["unknown"] += 1

        rows.append({
            "audit_id": audit_id,
            "ts": ts,
            "cycle": rec.get("cycle"),
            "action": action,
            "target": rec.get("target"),
            "reason": rec.get("reason"),
            "trigger": rec.get("trigger") or {},
            "window_min": window_min,
            "signals": signals,
            "signals_source": source,
            "tier_counts": {
                "t1": tier_counts[1],
                "t2": tier_counts[2],
                "t3": tier_counts[3],
                "unknown": tier_counts["unknown"],
            },
        })
    return rows


class Handler(BaseHTTPRequestHandler):
    window_min = 30  # overridden in main()

    def log_message(self, *args, **kwargs):  # noqa: ARG002
        return

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/writes":
            rows = _load_writes(self.window_min)
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
    p.add_argument("--port", type=int, default=9877)
    p.add_argument("--window", type=int, default=30, help="minutes of signals before each write (default 30)")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    if not AUDIT_PATH.exists():
        print(f"audit not found at {AUDIT_PATH}", file=sys.stderr)
        return 1

    Handler.window_min = args.window
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"

    n_writes = len(_load_writes(args.window))
    print(f"\n  Deja annotate-writes — {url}")
    print(f"  Audit: {AUDIT_PATH} ({n_writes} writes)")
    print(f"  Signal window: {args.window} min before each write")
    print(f"  Annotations are stored in your browser's localStorage.")
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
