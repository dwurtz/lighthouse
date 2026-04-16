# Post-mortem: the Graphiti + Kuzu experiment

**Date:** 2026-04-16
**Author:** David Wurtz + Claude (written over a long debugging session that
surfaced the strategic picture only at the end)
**Decision:** Revert Deja's primary memory layer from Graphiti + Kuzu back
to wiki-Deja's integrate pipeline. Preserve today's observation-layer wins.
**Restore point:** commit `da70859` (full graphiti pipeline, queryable and
partially working) — we can always come back if circumstances change.

---

## What we were trying to do

Deja's memory layer was a markdown wiki — one file per person, project,
event — maintained by an LLM `integrate` step that ran every 5-15 minutes
on batched signals. Retrieval was BM25 + vector over the markdown corpus.
It worked, but it had real weaknesses:

- The integrate LLM was expensive and occasionally wrote bad merges
  (e.g., collapsing two unrelated Robs into one person file)
- No temporal invalidation — contradictions had to be resolved by
  rewriting files, which is lossy
- Relational queries ("who works at HealthspanMD?") relied on the LLM
  reading through multiple pages
- No formal schema — everything was prose, with all the flexibility
  and noise of prose

Graphiti offered a structured alternative: a temporal knowledge graph
with typed entities, typed edges, automatic entity resolution across
time, and hybrid search (BM25 + vector + graph traversal). We'd seen
Zep (Graphiti's commercial parent) publish strong research, and
Graphiti-on-Kuzu promised a single-file embedded backend — perfect
for a local-first single-user personal assistant.

We spent ~12 hours of focused effort validating it as a replacement.

---

## What we tried

1. **Prototype ingest** of the full wiki (~758 episodes) into Kuzu.
   Took ~2 hours, produced 3985 nodes and 7950 edges at ~$5 cost on
   OpenAI. Clean completion.
2. **Deduplication** — identified and merged 27 parallel-race duplicates
   from the concurrent ingest. Clean result.
3. **Eval queries** — 15 realistic queries against the graph. 12/15
   PASS, 2 MIXED, 1 FAIL. Strong baseline.
4. **Live shadow ingest** — wired the Deja observation cycle to feed
   signals into Graphiti in real-time alongside the existing wiki
   pipeline. This is where things fell apart.
5. **Subprocess isolation** — moved graphiti ingest into a dedicated
   worker process to isolate deadlocks.
6. **FalkorDB migration** — swapped Kuzu for FalkorDB (Redis module)
   when Kuzu's driver kept silently crashing.
7. **Preprocessing layer** — added an LLM step to compress raw OCR
   (4K chars) into structured signals (~400 chars) before Graphiti.
8. **Hermes + MCP** — wired an agent (Hermes) to query the graph via
   a custom MCP server exposing `ask_deja`, `search_memory`, and
   `query_graph` tools.

Everything was committed and pushed incrementally. The final "full
graphiti pipeline" state lives at commit `da70859`.

---

## Why it didn't work (technical)

### 1. The Kuzu driver is not production-ready
Kuzu itself was archived in October 2025 (the project is unmaintained
upstream). The Graphiti-Kuzu driver is a late addition with known
bugs: it silently crashed the worker process every 30-60 seconds under
live load with no Python traceback (C extension issue), and
occasionally triggered asyncio deadlocks where OpenAI calls would
complete but Kuzu writes never committed. A community fork called
LadybugDB exists but isn't officially supported by Graphiti.

### 2. FalkorDB works but has its own papercuts
Switching backends resolved the silent crashes. But we then hit:
- Graphiti issue #1319: FalkorDB driver's default `group_id` (`\_`)
  fails Graphiti's own group_id validator. Workaround required.
- Bandwidth cost of running a Redis-module daemon alongside the
  single-user local app, plus bundling that daemon into the .app
  bundle for distribution.
- Lost the appeal that drew us to Kuzu in the first place: zero-ops
  single-file embedded storage.

### 3. Entity resolution is imperfect and LLM-driven
On the deduped graph we still had 5 "Rob"-prefixed Person nodes for 2
real humans (Rob Hurst at HealthspanMD + Coach Rob the gymnastics
coach). The LLM dedup step errs conservative — which prevents wrong
merges but produces fragmented-entity noise. Our `aliases` and
`context` schema fields helped but didn't fully solve it. At the
other extreme, when it DID merge, it sometimes cross-polluted
attributes (Miles's Person node ended up with Rob Hurst's `context`
string from a neighboring extraction).

### 4. Relational queries are the wrong shape for pure semantic search
The canonical test — "Who works at HealthspanMD?" — failed on every
one of Graphiti's 6 search recipes. The embedding similarity matched
the *pattern* "X works at Y" rather than the *entity* "HealthspanMD",
so the top results were mostly people at Humanleap, Anthropic, and
Stripe. Only direct Cypher produced the right answer. We built a
`dual_retrieve` helper that always runs both paths. Useful, but a
sign we were fighting the tool.

### 5. Cost scaled per-signal, not per-insight
A 4000-char screenshot produced ~80 OpenAI calls inside Graphiti's
add_episode (entity extraction × N entities, dedup search, attribute
extraction, edge extraction, summary regeneration, embeddings). At
~$0.03/screenshot × 300 meaningful screenshots per day = $270/mo on
screenshots alone. Preprocessing cut this ~10× but the underlying
pattern — per-signal cost times heavy multiplier — was structural.

Wiki-Deja's integrate batched 5-15 signals into ONE LLM call at
~$0.02/cycle, maybe 30-50 cycles per day. Roughly ~$0.60/day. An
order of magnitude cheaper AND produced a coherent narrative instead
of fragments.

### 6. We lost session-level synthesis
This was the killer realization. Wiki-Deja's integrate wasn't just
"write to markdown" — it was clustering multiple signals into one
coherent update per LLM call. That's session-level reasoning. When
we moved to Graphiti's per-signal add_episode, we atomized the
narrative into disconnected facts. To get back to session-level
output in the graph world, we'd have needed to build a reflection
layer on top of Graphiti — effectively reinventing integrate.

---

## Why it didn't work (strategic)

### 1. We were solving a different problem than Graphiti
Graphiti is optimized for **agent memory**: keeping an LLM's context
window accurate across multi-turn sessions and many users. Its value
propositions — temporal fact invalidation, group_id multi-tenancy,
structured retrieval under latency — are exactly what Zep (the
commercial parent) needs.

Deja is solving **personal memory**: a coherent narrative about one
human's life. Personal memory wants:
- Rich prose per entity (not structured facts in a graph)
- Session-level arcs (not per-event atoms)
- Human readability (you can open alice.md and read)
- Single-user single-file simplicity
- Cheap enough to run on battery

Looking similar ("remember things over time, query them later") is
not the same as being the same problem. We conflated them.

### 2. The architectural elegance seduced us
Graphs are a more "correct" abstraction for relationships than
markdown + wikilinks. Temporal edges with valid_at/invalid_at are
genuinely powerful. Hybrid search is clever. It's easy to look at
this machinery and conclude it must be better than a wiki.

But Deja's use case doesn't actually benefit from most of it:
- Temporal fact invalidation? Wiki handles contradictions fine by
  rewriting files, and most personal facts don't *need* bitemporal
  precision. "David's wife is Dominique" doesn't need a valid_at
  range.
- Multi-tenancy? Single-user app.
- Cross-session LLM context? Deja isn't an LLM — it's an observer
  and a memory.
- Graph traversal? Most queries are "tell me about X" (semantic
  recall), not relational joins.

We built infrastructure for problems we didn't have.

### 3. Opaque memory is a regression for a personal app
Wiki-Deja's memory is a directory of markdown files. David can open
any of them, read what Deja remembers, edit it, revert via git, or
delete it. Total transparency, total user control.

Graphiti's memory is a Kuzu/Falkor database. To see what Deja
remembers, you run a query. To edit, you don't — entity attributes
are LLM-regenerated. To revert, you don't.

For a personal AI assistant where trust and legibility matter, this
is a regression. The markdown wiki is a feature, not an
implementation detail.

### 4. The event/project/people structure was already a graph
wiki-Deja's directories (people/, projects/, events/) with wikilinks
between them ARE a graph. events/YYYY-MM-DD/ files ARE arc chapters.
goals.md's "Waiting for" section IS a typed-edge-like structure. We
had the conceptual model. We just didn't see it as a graph because
the storage was markdown.

The right move was to invest in the wiki-as-graph — better retrieval,
maybe a render-time graph view, sharper entity conventions — not
replace the substrate wholesale.

---

## Honest pros of the graph approach

We want to remember what was genuinely good about what we tried:

- **Typed schema discipline** was valuable. Forcing ourselves to
  enumerate entity types (Person, Project, Event, Task, WaitingFor,
  etc.) and edge types (WORKS_AT, COMMITTED_TO, OWES) clarified what
  Deja actually remembers. This discipline transfers back to integrate
  prompts.
- **Hybrid search** (BM25 + vector + graph BFS) really is better than
  any single approach for some query types. Our 12/15 eval PASS rate
  was genuine; when Graphiti worked it produced rich retrieval.
- **Temporal edges** are a powerful idea worth remembering. If we
  ever need precise "what was true on date X", the pattern is in our
  head now.
- **MCP + Hermes architecture** is a real win that's portable across
  memory substrates. "Deja is the memory layer, Hermes is the
  reasoning layer" clarified our thinking about what Deja should do
  (observe, triage, remember, serve) and not do (reason, plan, act).
- **Preprocessing** (cleaning OCR before it hits the memory layer)
  turned out to be valuable regardless of substrate. We keep it.

---

## What we're keeping

Everything we built in the observation layer applies to wiki-Deja too:

- **Event-driven screen capture** — replaces the 6s fixed timer with
  app-focus, typing-pause, and window-change triggers. Reduces
  captures ~14K/day → ~1K/day. Makes integrate's job cleaner because
  each capture is a meaningful moment.
- **Active-window OCR crop** — focused_frame_norm sidecar + deja-ocr
  --region. Strips cross-app chrome from every OCR.
- **Screenshot preprocessing** — gpt-4.1-mini step that categorizes,
  extracts, and SKIPs based on "does this matter to David as a
  human?" reasoning. Feeds integrate clean, structured input.
- **Engaged-thread Tier 1 rule** — `[ENGAGED]` prefix on incoming
  emails in threads David has replied to. Purely additive to the
  existing tier logic; works in wiki-Deja.
- **Email `format=full` sender parse** — bug fix, unrelated to memory
  architecture.
- **Voice-cmd stale-start fix** — bug fix, unrelated to memory
  architecture.
- **Hermes + MCP server** — reasoning agent on top of whatever memory
  substrate. Gets redirected at wiki retrieval tools.

---

## When we'd revisit graphs

These would change the calculus:

- **Multi-user Deja.** Graphiti's group_id model fits naturally when
  different users share a service. Wiki-per-file doesn't scale to
  many users as cleanly.
- **Mature Kuzu replacement.** If LadybugDB becomes production-ready
  or Graphiti's Kuzu driver gets resurrected, the embedded single-file
  appeal comes back.
- **Retrieval bottleneck on the wiki.** If BM25 + vector over markdown
  starts missing relational queries at scale, the graph approach for
  *retrieval only* (not primary storage) might be worth revisiting —
  e.g., build an ephemeral graph index over the wiki for faster
  traversal, without making the graph the source of truth.
- **Sub-second fact invalidation requirement.** If Deja ever needs to
  know "this fact was true at 14:32 but wrong by 14:45" with precision,
  bitemporal edges become load-bearing. Personal memory rarely needs
  this.

---

## Key moments from the day

For flavor and for future-David's amusement:

- **07:54** — Prototype ingest completes in 123 min. 98.9% success.
  We're feeling good.
- **11:30** — Live shadow ingest works, commits the first episode in
  29 seconds. We're feeling better.
- **14:40** — First worker deadlock noticed. "The worker has been
  alive for 5 min but no OpenAI calls for 3:44 min, no Kuzu writes
  for 4:55 min. Process alive, doing nothing."
- **17:15** — Five worker restarts in 90 seconds. "No traceback in
  stderr. Silent SIGKILL or C-level crash. 0 successful log lines."
- **20:55** — We swap Kuzu for FalkorDB. First ingest succeeds in
  19.5s. "IT WORKED."
- **07:14 next morning** — Worker survived the night. 0 successful
  commits overnight because OpenAI quota ran out. Credits added,
  pipeline resumes.
- **07:40** — User: "hows it look?" Me: "Honestly — not healthy."
- **08:10** — Dashboard shows graph with 41 nodes. Facts include
  *"OpenAI billing details cover credits management"* extracted from
  an OCR of our debugging session. Contamination.
- **08:45** — User: "or maybe we batch into sessions still, so we get
  the narrative arc, but we store things in graphiti"
- **09:30** — User: "or what we're realizing is that the wiki based
  deja design is actually ideal"
- **09:45** — Reverted to wiki-Deja.

---

## Lessons

1. **Architectural elegance isn't architectural fit.** The right
   abstraction for a problem is the one that matches your actual
   workload, not the one that's theoretically more general.
2. **Investment in a working system beats migration to a new system
   with unknown failure modes.** Wiki-Deja had known issues; Graphiti
   had unknown issues. In hindsight, investing a day in improving
   integrate (session boundary detection, preprocessing, engaged-thread
   rule) would have produced more durable value than migrating
   substrates.
3. **Read the repo issues before adopting a dependency.** Graphiti
   issues #450 and #1319 would have warned us. Kuzu being archived
   would have warned us. We were moving fast.
4. **Opaque memory is a user-facing regression for a personal app.**
   Legibility is a feature.
5. **If you already have a clustering mechanism (integrate LLM
   batching), preserve it.** We thought 5-minute batching was a cost
   overhead to eliminate. It was actually the session synthesis
   layer, and removing it atomized our narratives.

---

## What happens now

- Wiki-Deja integrate runs as primary memory.
- Today's observation-layer improvements amplify it.
- Graphiti code (`graphiti_ingest.py`, `graphiti_worker.py`,
  `graphiti_schema.py`, `deja_mcp_server.py`) stays in the tree as
  dead code, in case we come back.
- Restore point at commit `da70859` if we need to revisit.
- No regrets. We know something now that wasn't knowable in advance.

---

*— Written after midnight, at the end of a long day, with the
relief of having chosen the right direction even if it took the
wrong one first.*
