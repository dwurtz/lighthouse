# Integrate model eval: Flash-Lite vs Flash

**Question:** Should we upgrade integrate from Gemini 2.5 Flash-Lite ($0.10/$0.40 per M tokens) to Gemini 2.5 Flash ($0.30/$2.50 per M)?

**Cost delta:** ~$6.50/month per user (Flash is 4.2× more expensive per cycle).

**Hypothesis:** Flash reduces the specific failure mode where integrate acts on `goals.md` ambient content without a supporting signal in the current batch (the "Cruz/Chime hallucination" pattern).

---

## The failure we're trying to measure

On 2026-04-12 at 11:32 and 11:37, integrate produced spurious wiki updates:

**11:32 cycle input (clipboard signals only):**
```
[clipboard] "- people: [slug-1, slug-2] on events creates a machine-readable entity graph..."
[clipboard] "That's literally hardcoded 'David Wurtz' in the source..."
[clipboard] "can we see whether our latest group-related-events..."
```

These are snippets of a developer conversation about the codebase. Zero information about Cruz, Maestra Lili, or a math workbook.

**What integrate wrote:**
> *"The user's observation about accepting a math workbook from Maestra Lili for Cruz directly corresponds to an existing task in the goals.md file. Therefore, I will complete that task and create a reminder."*

Integrate completed a task and emitted a wiki update for `david-accepts-math-workbook-for-cruz`. **Nothing in the signals supports this.** It reached into goals.md for something it could "act on" and manufactured evidence.

**11:37 cycle** did the same pattern on different clipboard snippets — wrote a "declined the Chime offer" event with no supporting signal.

These are **false positives** from integrate's standpoint: it took action when the correct answer was "these signals have nothing to do with any task."

---

## Eval design

### Shadow mode (live, real signals)

Every production integrate cycle now fires BOTH models in parallel with the same prompt, wiki context, and signals. Flash-Lite's output continues to drive the wiki (unchanged production). Flash's output gets logged to `~/.deja/integrate_shadow/<timestamp>.json` but never applied.

This captures the **real distribution of cycles** — most are no-op, some are complex, a few are ambiguous. A synthetic test set couldn't match that mix.

### Running period

Target: **48 hours of live cycles, minimum 200 cycles captured.** Current cadence is ~1 cycle every 5 minutes during active hours, quieter overnight. 48h should yield 400-600 cycles.

Enabled via `~/.deja/feature_flags.json`:
```json
{"integrate_shadow_eval": true}
```

### Metrics captured per cycle

Each `integrate_shadow/<ts>.json` file contains:

- `signals_text` — exact batch both models saw
- `flash_lite.{reasoning, wiki_updates, goal_actions, tasks_update, latency_ms}`
- `flash.{reasoning, wiki_updates, goal_actions, tasks_update, latency_ms}`
- `flash_error` — set if Flash failed/timed out (tracked separately)

### Classification script

`tools/integrate_shadow_compare.py` buckets every cycle:

| Bucket | Meaning |
|---|---|
| `both_no_op` | Both models said "no action." Expected for most cycles. |
| `agree` | Both acted and proposed the SAME wiki updates/goal actions. |
| `fl_extra` | Flash-Lite acted, Flash did nothing. **Candidate FL hallucination.** |
| `flash_extra` | Flash acted, Flash-Lite didn't. Candidate FL miss. |
| `disagree` | Both acted but on different things. |
| `flash_unavailable` | Flash errored or timed out. |

### Primary metric

**False-positive rate** = `fl_extra / total_cycles` when we manually verify those `fl_extra` cases and confirm the signals did not support the action.

Target for "worth upgrading": **Flash-Lite's false-positive rate is >3× Flash's** AND the absolute count is >5 verified hallucinations over the eval window.

### Secondary metrics

- **Median + p95 latency** — Flash is slower. If p95 > 10s the cycle cost becomes user-visible (integrate holds up other work).
- **Agreement rate on actioned cycles** — if both models agree on what to do >90% of the time, the model choice doesn't much matter.
- **Flash availability** — if Flash errors >5% of calls, the shadow data is too sparse to decide.

---

## The specific Cruz/Chime replication check

We already have the two fixtures from the 11:32 and 11:37 failures:

- `~/.deja/integration_fixtures/20260412-113205-combined.json` (Cruz hallucination)
- `~/.deja/integration_fixtures/20260412-113743-combined.json` (Chime hallucination)

Both through Flash-Lite and Flash at temp=0.2, **N=20 runs each**:

```bash
./venv/bin/python tools/integrate_model_replay.py \
    --fixture ~/.deja/integration_fixtures/20260412-113205-combined.json \
    --model flash-lite --runs 20

./venv/bin/python tools/integrate_model_replay.py \
    --fixture ~/.deja/integration_fixtures/20260412-113205-combined.json \
    --model flash --runs 20
```

*(Note: `integrate_model_replay.py` not yet built — write if we want this replication step. The existing one-off script in `tools/` can be templated.)*

**Scoring:** a run is a "false positive" if its output has any non-empty `wiki_updates`, `goal_actions`, or `tasks_update` (both fixtures should produce "no action" since the signals are developer-chat clipboard snippets).

**Decision thresholds:**
- Flash-Lite FP rate > 20% AND Flash FP rate < 5% → strong evidence, upgrade
- Both < 10% → the prompt updates we made between 11:32 and now already fixed it; don't upgrade
- Both > 20% → model choice isn't the issue; tighten prompt further

---

## Open questions the eval will answer

1. **How often does Flash-Lite hallucinate in production?** (Before I only had 2 anecdotes.)
2. **Would Flash have prevented those specific cases?** (Shadow data + replay give us evidence.)
3. **Is the latency penalty acceptable?** (Flash's p95 — under 10s is fine, under 5s is great.)
4. **Does Flash catch things Flash-Lite misses?** (`flash_extra` bucket — the opposite concern.)
5. **What's the actual disagreement rate on "should we act"?** (If 5% of cycles differ, we have a real question. If 0.5%, both are fine.)

---

## What the eval WON'T answer

- **Whether the issue is fixable with better prompting.** We already made several prompt tightenings after the 11:32 failure. A prompt-discipline fix is cheaper than a model upgrade. If Flash-Lite's FP rate during the eval is already low, the recent prompt changes may have solved it — in which case the data tells us to stay on Flash-Lite.
- **How Pro performs.** Pro ($1.25/$10) is ~3× Flash's cost. If neither Flash-Lite nor Flash is reliable, a Pro shadow would be the next step.
- **Long-tail rare failures.** A 48-hour window samples ~500 cycles; 1-in-10000 failures won't show up. We live with that.

---

## Review checkpoint

After 48 hours:

1. Run `./venv/bin/python tools/integrate_shadow_compare.py`
2. Run again with `--detailed` and manually review 10 cases from `fl_extra` and `flash_extra` buckets
3. Decide per the thresholds above
4. Either: update `INTEGRATE_MODEL` in `config.py` → rebuild → ship, OR: flip the feature flag off and leave Flash-Lite in place
5. Archive the shadow dir (`~/.deja/integrate_shadow/`) with the decision rationale

Data-first decision, not vibes.
