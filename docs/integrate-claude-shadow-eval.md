# Integrate Claude Shadow Eval

Experiment to decide whether Claude should replace Gemini as Deja's integrator.

## Why

Gemini Flash drives integrate today. It's fast and cheap but has produced two recent quality failures we could observe:

1. **SHOP confabulation** — ambiguous iMessage "this/blow off top" + CAR/Avis screen context → Gemini invented SHOP from stale wiki fragments instead of using the visible screen signal.
2. **Ghost pages from OCR** — `David Joad` / `Mike Wur2` created as people pages from garbled calendar-dropdown OCR text.

In parallel investigation, Claude (running in cos) spontaneously flagged the `Mike Wur2` artifact as OCR noise *without being asked* and stayed silent rather than pinging David. That's the quality delta we want to quantify.

## Tradeoffs going in

| | Gemini Flash | Claude Sonnet 4.6 (local CLI) |
|---|---|---|
| Latency per cycle | ~3s | ~20-30s |
| Cost / user | trivial (proxy) | covered by Max sub |
| Noise filtering | misses OCR artifacts | reasoned about them unprompted |
| Pronoun resolution | uses stale wiki | (hypothesis) uses live context |
| JSON-contract adherence | tight | tight (prompt says so, but untested) |
| Rate limits | proxy-bound | Max sub limits |
| Single-vendor dependency | no | yes |

## Phase 0 — scaffolding (shipped in `6786a4b`)

Opt-in flag `integrate_claude_shadow: true` in `~/.deja/config.yaml`. When on, every integrate cycle fires a parallel `claude -p` subprocess with the same prompt Gemini sees. Claude's JSON output lands in `~/.deja/integrate_shadow/<ts>.json` under `shadows[].model == "claude-local"`. Production wiki writes still come from Gemini — zero risk to the live wiki.

## Phase 1 — shadow data collection (you are here)

**Enable:**
```bash
echo "integrate_claude_shadow: true" >> ~/.deja/config.yaml
cd ~/projects/deja && make dev
```

**Run normally for a day.** Regular email, messaging, browsing activity. No special behavior required.

**Inspect:** `ls ~/.deja/integrate_shadow/` — you should see a file per integrate cycle. Each should include both the Gemini `production` block and a `claude-local` entry under `shadows`.

## Phase 2 — evaluation

**Tooling:** `tools/integrate_shadow_diff.py` (shipped with this doc). Commands:

```bash
# Last 6 hours, side-by-side + aggregate
uv run python tools/integrate_shadow_diff.py --since 6

# Just the stats, no per-cycle detail
uv run python tools/integrate_shadow_diff.py --since 24 --aggregate-only

# Deep-dive a specific cycle
uv run python tools/integrate_shadow_diff.py ~/.deja/integrate_shadow/20260418-090000.json
```

**What to look at — per cycle:**

- **Slugs written by one side but not the other.** Each is a judgment call. Audit the actual wiki page + the signal context. Did Gemini invent ghost pages Claude correctly filtered? Did Claude miss real signal?
- **Narrative divergence.** Same signals, different prose. Which is more accurate / specific / useful?
- **Latency** — Claude is expected slower. Sanity-check it's under ~45s.

**What to look at — aggregate (after ~20-50 cycles of real data):**

- **Agreement rate** — fraction of cycles where Gemini and Claude produced the same slug set. Expectation: 60-80%.
- **Claude-only writes** — candidates for "Claude sees things Gemini misses" OR "Claude hallucinates." Sample 5, read context, categorize.
- **Gemini-only writes** — candidates for "Claude filters noise Gemini writes." Sample 5, same process.
- **Average latency** — Gemini p50 vs Claude p50. If Claude > 45s consistently we can't ship it without making cycles longer.
- **Max token usage** — open your Max dashboard. Extrapolate: one cycle × 288 cycles/day × 30 days = monthly burn. Make sure that fits your plan.

**The Go / No-Go gate:**

GO to Phase 3 if at least all of:
- [ ] Claude agrees with or improves on Gemini for ≥80% of audited disagreements
- [ ] Fewer than 5% of cycles show Claude hallucinating an entity
- [ ] p50 latency under 30s, p95 under 60s
- [ ] Max usage dashboard shows headroom

NO-GO → revert the flag, keep Gemini. The scaffolding stays in place for a future run.

## Phase 3 — cutover (if Phase 2 says go)

Add an `integrate_mode: claude` config key that switches the production path to use `invoke_claude_shadow` instead of `GeminiClient._generate`. Gemini becomes the shadow. Run for another week in this inverted configuration to confirm stability before deleting the Gemini integrate path.

## Phase 4 — unification (separate future work)

Delete cos as a separate subprocess. Have Claude-integrate make notification decisions inline during the same cycle via MCP. One Claude pass per cycle, no double-invocation.

## Known limitations of the shadow eval

- **Claude gets no MCP access in shadow.** It's a pure prompt-to-JSON comparison. Phase 3 can optionally add MCP, which might widen the quality gap further in Claude's favor.
- **Claude's JSON contract adherence is untested.** If parse failures are frequent, the shadow errors in aggregate will tell us — if the rate is > 5%, we need prompt adjustments before cutover.
- **Sample size matters.** 10 cycles of shadow data is not enough to judge. Aim for 100+ (~2 days) before Phase 2 conclusions.

## Revert plan

```bash
# turn off
sed -i '' '/integrate_claude_shadow/d' ~/.deja/config.yaml
cd ~/projects/deja && make dev
```

The shadow files stay on disk for later reference; they're not load-bearing for any live code.
