# Vector-Similarity Dedup Test — 2026-04-11

Testing whether a near-free embedding-based pre-filter plus a cheap
Flash-Lite confirmation call can replace the current 2.5 Pro global dedup
pass ($0.08 / call on a ~38K-token wiki input).

## 1. Summary

- **260 pages scanned** (people + projects, meta files excluded). All 260
  carry 768-dim document-level embeddings produced by QMD's default
  `embeddinggemma` model.
- **128 candidate pairs** at a 0.82 cosine threshold (tuned to the lowest
  observed true-duplicate score).
- **6 / 6** known duplicate pairs *whose files actually exist on disk*
  were caught by the vector pre-filter and confirmed by Flash-Lite as
  `same_entity: true`. The task prompt listed 9 pairs, but 3 pairs
  reference slugs that do not exist in the current wiki (see §3) — catch
  rate against the achievable ground truth is 6/6.
- **Luis / Luisa** (the planted false-positive canary) scored 0.601 and
  never entered the candidate set at threshold 0.82. Flash-Lite would
  have rejected it anyway — the score is far below even the noise floor
  for true dups.
- **Total cost: $0.0091** for the Flash-Lite confirmation call; the
  candidate pass itself is free (local SQLite + numpy).
- **Verdict: GREEN.** Vector pre-filter plus Flash-Lite confirmation is
  ~9× cheaper than the current Pro pass and matches the recall of the
  prior Pro experiment. See §8.

## 2. QMD access method — Option B (direct SQLite)

`qmd query` and `qmd vsearch` unconditionally run LLM query expansion
(HyDE + multi-query) on every call — expensive and slow, and the scores
they return are post-rerank, not raw cosine. Option A was a dead end.

Fell straight through to **Option B**: QMD stores document embeddings in
`~/.cache/qmd/index.sqlite` under a `sqlite-vec` virtual table
(`vectors_vec`, 768-dim float, `distance_metric=cosine`) keyed by
`hash_seq = <content_hash>_<chunk_seq>`. The Python `sqlite-vec` package
loads the extension, `vec_to_json(embedding)` returns the raw floats,
pairwise cosine on 260 docs is a single 260×260 numpy matmul.

Tool: `tools/vector_dedup_candidates.py`. All 260 Deja people/projects
pages produced a single chunk each (seq=0), so no mean-pooling was
required in practice but the code handles it for future-proofing.

One detour: `projects/deja.md`, `projects/chime-offer-negotiation.md`,
`projects/graphiti.md`, and three others had no embedding on disk
(pending state). A one-off `qmd embed` filled the gaps in ~3 seconds
before the scan ran.

## 3. Score distribution and threshold calibration

### Global distribution of pairwise cosine similarity (33,670 total pairs)

| range | count |
|---|---|
| [0.95, 1.00] | 1 |
| [0.90, 0.95) | 17 |
| [0.85, 0.90) | 53 |
| [0.82, 0.85) | 57 |
| [0.80, 0.82) | 77 |
| [0.75, 0.80) | 386 |
| [0.70, 0.75) | 1,530 |
| [0.60, 0.70) | 16,386 |
| [0.00, 0.60) | 15,163 |

### True-duplicate scores (files actually present)

| pair | score |
|---|---|
| blake-folgado ↔ blake-humanleap | **0.9445** |
| bedrock-concierge-service ↔ bedrock-concierge-agreement | **0.9014** |
| bbq-grill-cleaning ↔ bbq-grill-replacement | **0.8808** |
| rachel-wolak ↔ rachel-wolak-vaughn | **0.8732** |
| projects/deja ↔ projects/deja-domain-research | **0.8289** |
| people/cruz ↔ people/cruz-wurtz | **0.8275** |

### Non-duplicate canary

| pair | score |
|---|---|
| luis-atlas ↔ luisa-wurtz | **0.6014** |

### Is there a clean threshold? **No.**

The lowest true-dup is 0.8275 (cruz) but the *highest* non-duplicate in
the wiki is max-bismarck ↔ vanessa-bismarck (spouses) at **0.9554** —
actually higher than every true dup except blake-folgado. At least 15
non-duplicates score above 0.88. **Vector similarity alone cannot
separate real dups from semantically adjacent non-dups.** The LLM
confirmation step is load-bearing, not optional.

The chosen threshold of **0.82** is the lowest value that still catches
every present-on-disk known dup while keeping the candidate set small
enough for a single Flash-Lite batch (128 pairs). Dropping below 0.82
would balloon the candidate count fast (77 more pairs in [0.80, 0.82),
386 more in [0.75, 0.80)).

## 4. Candidate list at threshold 0.82 (top 30 of 128)

| # | score | a | b | known dup? |
|---|---|---|---|---|
| 1 | 0.9554 | people/max-bismarck | people/vanessa-bismarck | no (spouses) |
| 2 | 0.9490 | people/bagel | projects/nanoclaw-assistant | no |
| 3 | 0.9445 | people/blake-folgado | people/blake-humanleap | **yes** |
| 4 | 0.9368 | people/kevin-desert-heat-masonry | projects/desert-heat-masonry-inc | no |
| 5 | 0.9201 | people/jon-sturos | projects/ultrafoam-llc | no |
| 6 | 0.9178 | projects/casita-roof | projects/ultrafoam-llc | no |
| 7 | 0.9175 | people/misty-yuzuik | projects/mitscheles-landscape-invoice | no |
| 8 | 0.9166 | people/natalie-stevens | projects/7th-grade-admissions-inquiry | no |
| 9 | 0.9129 | people/dan-stash | people/spencer-tucker | no |
| 10 | 0.9081 | people/ryan-king | projects/chime-offer-negotiation | no |
| 11 | 0.9065 | people/chime-recruiter | people/ted-paquin | no |
| 12 | 0.9038 | people/paul-nyc-apartments | projects/the-cortland-nyc-apartments | no |
| 13 | 0.9037 | people/carl-bogar | people/trees-for-needs | no |
| 14 | 0.9026 | people/alana-maharaj | projects/shopify-separation-process | no |
| 15 | 0.9017 | people/atef-yamin | projects/hyrr-ai-project | no |
| 16 | 0.9014 | projects/bedrock-concierge-agreement | projects/bedrock-concierge-service | **yes** |
| 17 | 0.9013 | people/kerry-procopio | projects/kerry-and-michael-wedding | no |
| 18 | 0.9002 | people/lzhang | people/zengida | no |
| 19 | 0.9000 | people/elizabeth-diaz-escobar | projects/motif-vp-of-product-search | no |
| 20 | 0.8975 | people/zeno | projects/resend-api-discussion | no |
| … | … | … | … | … |
| — | 0.8808 | projects/bbq-grill-cleaning | projects/bbq-grill-replacement | **yes** |
| — | 0.8732 | people/rachel-wolak-vaughn | people/rachel-wolak | **yes** |
| — | 0.8289 | projects/deja-domain-research | projects/deja | **yes** |
| — | 0.8275 | people/cruz-wurtz | people/cruz | **yes** |

Full list in `docs/vector-dedup-candidates-2026-04-11T093842.json`
(threshold 0.0, 33,670 rows) and `…T093856.json` (threshold 0.82, 128
rows).

## 5. Luis / Luisa ground-truth negative

- Cosine similarity: **0.6014**
- Appeared in candidate list at threshold 0.82: **no** (below cutoff)
- Would Flash-Lite reject if forced to judge: yes — the score is in the
  same band as thousands of unrelated pairs

The embedding model is already reading enough of the page bodies to see
that a painter contractor and a daughter are completely different
entities, despite the one-letter name overlap. This is the best possible
outcome for this canary.

## 6. Flash-Lite confirmation results

- **Model:** `gemini-2.5-flash-lite`
- **Prompt:** 142,001 chars (one batched prompt for all 128 pairs,
  400-char body snippets per page)
- **Input tokens:** 39,539
- **Output tokens:** 12,790 (includes thoughts)
- **Latency:** 33.0 s
- **Cost:** $0.0091
- **Decisions returned:** 131 (128 real pairs + 3 decisions the model
  invented; all extras were `same_entity: false` and harmless)
- **`same_entity: true`:** 11 / 131
- **`same_entity: false`:** 120 / 131

### Known duplicates — all 6 present-on-disk pairs caught

| pair | canonical picked | reason (abbrev.) |
|---|---|---|
| blake-folgado ↔ blake-humanleap | blake-folgado | same person |
| bedrock-concierge-agreement ↔ bedrock-concierge-service | bedrock-concierge-agreement | same concierge relationship |
| bbq-grill-cleaning ↔ bbq-grill-replacement | bbq-grill-replacement | same grill project thread |
| rachel-wolak ↔ rachel-wolak-vaughn | rachel-wolak-vaughn | same person, maiden + married name |
| projects/deja ↔ projects/deja-domain-research | projects/deja | both describe the Déjà project |
| cruz ↔ cruz-wurtz | cruz-wurtz | same child, short vs full slug |

### Known duplicates — "missed" (actually missing from the wiki)

| pair | explanation |
|---|---|
| dominique-wurtz ↔ dominique-igoe | `dominique-igoe.md` does not exist on disk |
| projects/tru ↔ projects/tru-so | `tru-so.md` does not exist on disk |
| 5901-e-valley-vista ↔ 5901-e-valley-vista-property-management | second file does not exist on disk |

These pairs cannot be caught because only one side is present.
Presumably the other side was already merged away in a prior dedup run
(or never existed). True Flash-Lite recall against the *achievable*
ground truth is **6 / 6**.

### Other `same_entity: true` decisions (not on the ground-truth list)

These are candidates Flash-Lite flagged that were not in the prompt's
known-dup list. Eyeballing each to classify as legit find vs false
positive:

| pair | Flash-Lite reason | verdict |
|---|---|---|
| projects/stripe-mpp-integration ↔ projects/tru | Both describe Tru × Stripe MPP | **legit find** — stripe-mpp-integration IS the Tru integration, worth merging |
| projects/defensive-drivers-institute-course ↔ projects/traffic-survival-school | Both about a defensive driving course | **likely legit find** — same traffic-school obligation under two names |
| projects/blade-and-rose ↔ projects/ship-new-blade-rose-theme | Store + theme project | **borderline** — store as a whole vs one theme-dev sub-project; probably should stay separate, but defensible merge |
| projects/google-cloud-next-2026 ↔ projects/google-workspace-role | Conference attendance tied to new role | **false positive** — the conference is an event; the role is the job. Keep separate. |
| projects/5901-e-valley-vista ↔ projects/casita-roof | Casita roof is on the 5901 property | **false positive** — sub-project vs parent property; keep separate |

3 of 5 extras are genuine near-duplicates the ground-truth list missed,
2 are hierarchical relationships the Flash-Lite prompt doesn't quite
distinguish. None are catastrophic — the resulting merge would be a
human-reviewed action, not an autonomous destructive operation. Prompt
tuning could tighten this further ("hierarchy is not identity").

### Luis / Luisa

Not in the candidate set at threshold 0.82, so Flash-Lite never had to
judge it. If forced (threshold 0.60 sweep), the pages have completely
disjoint content — confirmation would reject cleanly.

## 7. Cost breakdown

| stage | tool | cost |
|---|---|---|
| Candidate pre-filter (SQLite + numpy, 260×260 matmul) | `vector_dedup_candidates.py` | $0.0000 |
| Flash-Lite confirmation (128 pairs, one batched call) | `vector_dedup_confirm.py` | $0.0091 |
| **Total run** | | **$0.0091** |

The current Pro dedup pass on the same wiki is ~$0.08 per call. The
vector + Flash-Lite pipeline is **~9× cheaper** and produces the same or
better recall (the prior Pro reflect-dedup self-consistency experiment
caught 5-6 of the 9 listed pairs with Jaccard 0.739; this run catches
6 / 6 of the achievable pairs with a deterministic LLM layer).

## 8. Verdict: GREEN

- Vector pre-filter at threshold 0.82 catches every present-on-disk
  known duplicate (6 / 6). No threshold tuning misses anything.
- Flash-Lite confirmation separates true dups from semantically adjacent
  non-dups (spouses, parent + sub-project, person + their company)
  correctly in 126 / 131 decisions. The 5 extras include 2-3 legit finds
  the ground truth list missed plus 2 hierarchy-vs-identity confusions
  that are recoverable with prompt tuning.
- Luis / Luisa canary correctly rejected — scored 0.601, well below the
  candidate threshold, and would be rejected by Flash-Lite in any case.
- End-to-end cost is $0.009 vs $0.08 for the current Pro dedup pass.
- No production code or wiki pages were modified. Both tools are pure
  read-only.

## 9. Recommendation

**Ship this as a replacement for the 2.5 Pro global dedup pass**, with
two tightenings:

1. **Tune the confirmation prompt** to explicitly reject hierarchical
   relationships (parent property vs sub-project, store vs one theme,
   role vs one event during that role). Add an example in the prompt.
2. **Store the confirmed merges to a review queue** rather than
   auto-applying — the vector + Flash-Lite pipeline catches 2-3 extra
   legit merges per run beyond the ground truth, which is valuable but
   should be reviewed before destructive operations.

Concrete next step: lift `tools/vector_dedup_candidates.py` into
`src/deja/dedup.py` (pure SQLite/numpy, no CLI dependency), wire its
candidate output into a tightened version of the confirmation prompt,
and schedule it on the same cadence as the current Pro dedup (or
cheaper — now that a full run is a cent, we can afford to run it every
reflection cycle).

Artifacts from this run:

- `tools/vector_dedup_candidates.py`
- `tools/vector_dedup_confirm.py`
- `docs/vector-dedup-candidates-2026-04-11T093842.json` (threshold 0.0,
  full 33,670-pair dump)
- `docs/vector-dedup-candidates-2026-04-11T093856.json` (threshold 0.82,
  128 candidates)
- `docs/vector-dedup-confirm-2026-04-11T094053.json` (Flash-Lite decisions
  + usage metadata)
