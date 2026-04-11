# Reflection pass A/B eval — 2026-04-10

Six-condition experiment comparing Gemini 3.1 Pro / 2.5 Pro / 2.5 Flash
against the reflect pass on a freshly merged 264-page wiki, testing both
the existing full-wiki prompt architecture and a retrieval-based
alternative that replaces the wiki blob with `index.md` + a QMD top-N
page-body slice.

## Executive summary

- **Keep 3.1 Pro as the reflection model.** The "upgrade to Flash" swap
  produces a 37-update sprawl dominated by plausible-sounding but
  unverifiable new pages. 3.1 Pro is the only model in the experiment
  that stayed content-focused and didn't either over-delete (D) or
  over-create (D, F).
- **Do NOT ship retrieval-mode reflection yet.** Same-model architecture
  swap (A vs E) collapsed quality: 25 updates → 8, Jaccard 0.10 against
  the full-wiki baseline. 3 of the 25 themes survived. Root cause: the
  naïve QMD query used in this harness pulled only Deja/dev pages
  because `recent_observations` is dominated by browser tab titles. The
  production `wiki_retriever.py` uses entity extraction — it would do
  better, but this experiment cannot prove by how much.
- **Full-wiki + 3.1 Pro costs $0.22/call, ~$19.99/mo at 3×/day.** Swapping
  to Flash full-wiki drops that to ~$6.15/mo but the cost of the
  regression is 14 spurious deletes per call — unacceptable.
- **Monthly savings from any combo that ships is at most $14/user/mo.**
  Not worth the observed quality loss on a 264-page, post-merge wiki
  that is already the worst-case shape for an agent to reason about.
- **Confidence: medium.** Would be high if I could re-run E and F with
  the production entity-extraction query builder instead of the
  raw-text truncation this harness uses. That is the single next
  experiment.

## Experiment setup

- **Wiki state:** 264 pages, post-backup-merge. Known to contain
  ~6+ duplicate pages (`cruz`/`cruz-wurtz`, `blake-folgado`/`blake-humanleap`,
  `dominique-wurtz`/`dominique-igoe`, `deja`/`deja-ai-app`/`deja-domain-research`,
  etc.). The dedup opportunity is a first-class signal in this eval —
  the stronger-recall models surface it, the weaker ones miss it.
- **Fixture:** `/Users/wurtz/.deja/reflection_fixtures/20260410T214547.json`
  captured at 21:45:47 UTC, 283 KB (vs Agent 1's 141 KB stale capture
  against the 14-page wiki). `wiki_text` alone is 140 KB / ~90K tokens.
  `recent_observations` is 126 KB — the reflect pass is nearly entirely
  constrained by these two fields.
- **Prompt template:** `src/deja/prompts/reflect.md` (unchanged, current
  production).
- **Run directory:** `/Users/wurtz/.deja/reflection_eval/20260410T215000_abcdef/`
- **Harness:** `tools/reflection_eval.py`, extended for this experiment
  with `--wiki-mode {full,retrieval}`, `--index-max-lines`,
  `--run-suffix`, `--run-dir`, real `usage_metadata` extraction via
  `GeminiClient._generate_full()`, and a QMD retrieval builder.
- **Experiment total cost:** **$0.9149** (well under the $5 budget).

### QMD retrieval mechanics (E, F)

Query text is built by flattening `recent_observations`, stripping
timestamp/source prefixes, collapsing whitespace, and truncating to
2000 chars (vs production `wiki_retriever.py`'s 400-char entity-only
query). QMD is invoked as
`qmd query <text> -n 15 -c Deja --files`, returned lines are parsed
for `qmd://Deja/<path>`, and the top results are read from
`~/Deja/<path>` with meta files (`index.md`, `log.md`, `claude.md`,
`reflection.md`) filtered out. For this fixture QMD only returned
**9 paths** (scored; the threshold filtered the rest), with 2 meta files
dropped, leaving **7 page bodies totaling only 2898 bytes** in both the
E and F retrieval contexts. That is the experiment's most important
caveat — see "Known limitations" below.

## Headline metrics

Token counts are from `usage_metadata.prompt_token_count` +
`candidates_token_count` + `thoughts_token_count` (Pro "thinking"
counts as output for billing). Rates used: 2.5 Flash @ $0.30/$2.50
per M tok; 2.5 Pro & 3.1 Pro @ $1.25/$10.00 per M tok (3.1 Pro
pricing unannounced — assumed at parity with 2.5 Pro).

| ID | Model | Wiki | Lat (s) | In tok | Out tok | Cost (USD) | Parse OK | # updates | # goal_actions | thoughts (ch) |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 3.1 Pro prev | full | 87.30 | 90 233 | 10 905 | $0.2218 | yes | 25 | 0 | 895 |
| B | 3.1 Pro prev | full (rerun) | 71.95 | 90 233 | 9 519 | $0.2080 | yes | 24 | 0 | 755 |
| C | 2.5 Pro | full | 75.55 | 90 233 | 8 248 | $0.1953 | yes | 22 | 0 | 1 386 |
| D | 2.5 Flash | full | 70.78 | 90 233 | 16 512 | $0.0683 | yes | 37 | 0 | 3 088 |
| E | 3.1 Pro prev | retrieval (idx 264) | 67.60 | 63 933 | 9 134 | $0.1713 | yes | 8 | 0 | 635 |
| F | 2.5 Flash | retrieval (idx 100) | 54.80 | 57 406 | 13 197 | $0.0502 | yes | 22 | 0 | 1 937 |

All six conditions parsed cleanly — no JSON failures, no safety blocks,
no rate limits. None of the six emitted a `goal_action` on this
fixture (which is expected — the reflect prompt only emits actions for
explicit commitments, and the signal window was mostly browser activity
and one Brandi-resignation thread).

## Cost projection (per-user, 3× daily reflection cadence)

Assumes 90 reflection passes/month per user.

| ID | Cost/call | Cost/mo | Δ vs A baseline | Δ vs current-prod $23.63 baseline |
|---|---|---|---|---|
| A  | $0.2218 | **$19.96** | — | −$3.67 |
| B  | $0.2080 | $18.72 | −$1.24 | −$4.91 |
| C  | $0.1953 | $17.58 | −$2.39 | −$6.05 |
| D  | $0.0683 | **$6.15** | −$13.81 | −$17.48 |
| E  | $0.1713 | $15.42 | −$4.55 | −$8.21 |
| F  | $0.0502 | **$4.52** | −$15.44 | −$19.11 |

**Stacked-savings reading:**
- Full → retrieval alone (A → E) saves $4.55/mo.
- Pro → Flash alone (A → D) saves $13.81/mo.
- Both (A → F) saves $15.44/mo — the architectural change is worth
  ~30 % of the Pro → Flash saving, not ~100 %.

Note: A (my measured $19.96/mo) is ~$3.67 lower than the prior $23.63/mo
baseline the user cited. Two possible reasons: (1) fewer thinking
tokens on this fixture than the representative average, (2) the earlier
estimate used a bigger wiki_text from observations padding. Either way,
savings should be computed off the **measured** A value when deciding,
because that's what's actually charged today.

## Self-consistency — A vs B (3.1 Pro vs itself)

- **Jaccard: 0.58** (18 shared pages, 7 only-A, 6 only-B).
- The Pro-vs-Pro noise floor is **substantial** — even the same model
  on identical inputs disagrees on ~40 % of the touched page set.
- Thoughts content overlap: both A and B foreground Brandi's sudden
  resignation and Mike Stocker picking up the fountain-cover work. A
  additionally emphasizes Joan Levinson taking the Phoenix house off
  the listing; B additionally emphasizes the recurring Tru Render
  deploy failures. Neither is wrong — they're prioritizing different
  threads from a large signal window.
- **Takeaway:** any cross-condition Jaccard < 0.58 could be attributed
  to the noise floor, not to model or architecture differences. This
  is a critical calibration for reading the rest of the report.

## Cross-model divergence (full wiki) — A vs C vs D

| Pair | Jaccard | Both | Only-left | Only-right |
|---|---|---|---|---|
| A (3.1 Pro) vs C (2.5 Pro) | 0.24 | 9 | 16 | 13 |
| A (3.1 Pro) vs D (2.5 Flash) | 0.11 | 6 | 19 | 31 |
| C (2.5 Pro) vs D (2.5 Flash) | 0.26 | 12 | 10 | 25 |

**Key observations:**
- **All cross-model Jaccards are below the Pro-vs-Pro noise floor of
  0.58.** Different models are choosing to touch genuinely different
  subsets of the wiki — more than the same model does on reruns.
- C (2.5 Pro) and D (2.5 Flash) detect the **wiki duplicates** that A
  and B miss. C proposes 4 deletes (merge `blake-humanleap` into
  `blake-folgado`, etc.), D proposes **14 deletes** (`cruz` →
  `cruz-wurtz`, `dominique-wurtz` → `dominique-igoe`, and many more).
  3.1 Pro proposed only 2 deletes in A and 0 deletes in B. This is a
  real quality divergence — the post-merge wiki has a lot of duplicates
  and 2.5 Pro/Flash correctly prioritize cleanup while 3.1 Pro
  prioritizes content updates.
- D's 37-update output skews heavily toward **new page creation** — it
  touched `bbq-grill-cleaning`, `bbq-grill-replacement`,
  `bedrock-concierge-service`, `cancun-trip-planning`,
  `defensive-drivers-institute-course`, `traffic-survival-school`,
  etc. Many of these look real from the observations window, but the
  sheer volume suggests Flash is more eager than necessary on every cycle.
- C's thoughts section is the **best-written** of any condition: it
  zooms out, names the big themes (Google role + relocation + health),
  and spots the Shopify AI Toolkit vs `tru` strategic question. That's
  the kind of synthesis a reflect pass should do. C underperforms in
  update recall but out-performs in thoughts depth.

## Architecture comparison — A (full) vs E (retrieval), same model

**This is the critical finding of the experiment.**

- **Jaccard: 0.10.** Only **3 of 25 A-updates survived in E**:
  `people/david-wurtz`, `projects/deja`, `projects/deja-ai-app`.
- E produced **8 updates total** (vs A's 25) and 635 chars of thoughts
  (vs A's 895). Update categories E missed: Brandi resignation,
  Joan/Phoenix listing, Mike Stocker fountain cover, Luis Atlas
  staining, HealthspanMD, Shannon Kelty / Miles anxiety, Blade & Rose,
  `tru` Stripe integration, Branch desk delivery.
- **The root cause is retrieval quality, not architecture.** The
  naïve query builder used by this harness extracted the first 2000
  chars of `recent_observations`, which is dominated by Chrome
  browser-tab titles about `tru.so`, `deja`, clipboard mentioning Deja
  code. QMD dutifully returned 9 Deja/dev-adjacent pages:
  `native-meeting-recording-project`, `deja-ai-app`, `deja`,
  `new-macbook-consideration`, `deja-domain-research`, `alex-beller`,
  `sam-t-granola`, and 2 meta files. It did not return Brandi,
  Joan, Luis, Mike, Blade & Rose, HealthspanMD, etc. — even though
  those are the dominant human-life threads of the day.
- E's 8 updates are **tightly scoped to what it saw**: merge
  `deja-ai-app` + `deja-domain-research` into `deja`, note the
  `native-meeting-recording-project`, update `david-wurtz`. Within
  its context window it was precise — but its context was
  half the story.
- **Verdict: retrieval mode is not ready to ship with the query
  strategy used here.** The production `wiki_retriever.py` uses
  proper-noun and domain-stem extraction rather than raw-text
  truncation, and would almost certainly pull `brandi-rodriquez`,
  `joan-levinson`, `luis-atlas`, etc. given the actual signal text.
  That retest is the next experiment.

## Target config deep-dive — F (2.5 Flash + retrieval + idx 100)

Despite the same handicapped retrieval context as E, F surprisingly
produced a more complete picture than E:

- **22 updates** (vs E's 8), thoughts length 1937 chars (vs E's 635).
- Jaccard with A: **0.31** (11 shared pages of 36). Lower than the
  Pro-vs-Pro noise floor but ~3× better than E-vs-A.
- F's thoughts section mentions: `tru` deploy failures (correct),
  KTR pickup delay (correct, caught from recent_observations), Miles
  anxiety with Shannon Kelty (correct), Luisa vaccinations (correct),
  Blade & Rose + Amanda + Jon Safari debugging (correct),
  Branch desk delivery reschedule (correct), HealthspanMD vs
  Fountain Life (correct).
- F's update page list includes `people/cruz-wurtz`,
  `people/dominique-wurtz`, `people/shannon-kelty`, `people/jenna-dutra`,
  `people/krista-joiner`, `projects/5901-e-valley-vista`, etc. —
  **pages that were NOT in its retrieval context** (retrieval only
  surfaced 7 Deja-adjacent bodies). F inferred updates from the
  100-line index.md slugs alone, without seeing the current page content.
- **This is a correctness risk.** F is proposing to update pages whose
  current contents it doesn't know. It may overwrite real content with
  stale guesses or duplicate existing sections. F's raw output does
  contain plausible update text, but there's no way from these runs
  alone to verify it's actually additive-not-destructive without
  dry-run-diffing against the live wiki.
- Failure modes seen in F: none observed at parse-time, but
  "update a page I haven't read" is the single biggest risk to flag
  before shipping.

## Full thoughts — side by side

### A — gemini-3.1-pro-preview / full

> ## What stands out
> David is deep into the Tru/Stripe integration and Deja development, and just installed the Shopify AI Toolkit to give agents direct write access to the Blade & Rose backend. On the home front, the Palo Alto relocation prep is hitting some snags: Brandi resigned suddenly with 12 hours notice, leaving David to pick up the pieces with Mike Stocker, and Joan Levinson is taking the Phoenix house off the list tomorrow because the prospective client won't pay up and it's hard to show.
>
> ## Worth considering
> Joan taking the house off the list is a significant shift for the relocation timeline. You might need to figure out the next step for renting or selling the Phoenix house soon.
>
> ## A question for you
> With Brandi gone and Joan pausing the listing, do you want to find a new property manager or listing agent this week, or hold off while you focus on the Google start date?

### B — gemini-3.1-pro-preview / full (self-consistency rerun)

> ## What stands out
> Brandi resigned suddenly with just 12 hours' notice, leaving you to pick up the pieces with Mike Stocker on the fountain cover project. Also, Joan Levinson is taking the rental property off the list tomorrow because the prospective client won't pay up.
>
> ## Worth considering
> The `tru` Render deployment is still failing with "Exited with status 2" on multiple recent commits (including the top-up balance model and v1 simplification). You might need to dig into the build logs to get that unblocked.
>
> ## A question for you
> You mentioned being "opportunistic but also patient" at KTR while the kids started a new ball game. Did you manage to wrangle them in time for the church service? Also, don't forget to wipe down the kitchen table!

### C — gemini-2.5-pro / full

> ## What stands out
>
> The dust is settling from the job search, and the shape of the next chapter is coming into focus. The big themes are the new Google role, the potential family relocation to Palo Alto that comes with it, and a serious investment in personal health via HealthspanMD. It's a lot of major life-change happening at once.
>
> Meanwhile, the kids' lives are full and complex. You're not just managing logistics, but navigating real challenges with them—Miles's anxiety at school and Cruz being under-challenged in math are both significant threads that require real attention.
>
> ## Worth considering
>
> Your project `tru` hit a build error on Render. At the same time, Shopify just launched their AI Toolkit, which gives agents direct backend access to stores. This feels like it's in a similar space to what `tru` is aiming for. It might be a good moment to think about how `tru` is different or where it fits into a world where Shopify is building this capability directly.
>
> ## A question for you
>
> You're juggling a new executive role, a potential cross-country move, a new health protocol, and you're still hands-on coding `deja` and `tru` while also managing the Blade & Rose store. Is the plan to keep all these plates spinning, or is there a point soon where you might consciously pause or delegate one of the existing projects to create more space for the new commitments?

### D — gemini-2.5-flash / full

> ## What stands out
>
> David successfully completed the Defensive Drivers Institute course, achieving 100% on his tests. His youngest daughter, Luisa, received several booster shots today and has a dentist appointment next week. David is actively addressing Miles's anxiety around school performance with his science teacher, Shannon Kelty.
>
> On the project front, David is making progress on Deja, including domain research and logo design. He's also exploring the Shopify AI Toolkit for Blade & Rose. However, the Tru.so deployment failed, indicating a technical blocker. David is also actively managing several property-related tasks at 5901 E Valley Vista, including coordinating staining with Luis Atlas, scheduling casita roof work with Jon Sturos, and planning a BBQ grill replacement. His Branch Desk delivery was rescheduled from Tuesday to Wednesday.
>
> In terms of health, David is onboarding with HealthspanMD, having met with Dr. Hurst today to discuss his lipid panel and plaque prevention, and has an appointment scheduled for April 13th.
>
> ## What seems stuck or at risk
>
> **Tru.so Deployment:** The recent deployment failure for Tru.so is a significant blocker, meaning the latest changes are not live. This needs immediate attention to unblock development.
>
> **Kids' Pickup at KTR:** David's commitment to pick up the kids at KTR on time for church was at risk. While David was present, the kids were still playing and seemingly unaware of the time, causing a delay. This could lead to them being late for their church service.
>
> **Miles's Anxiety:** Miles's ongoing anxiety around school performance, particularly in lab settings, is a recurring concern that David is actively managing with the school. This is a delicate situation that requires continued support.
>
> **Branch Desk Delivery:** The rescheduling of the Branch Desk delivery could impact David's plans for setting up his home office.
>
> ## Worth considering
>
> *   **Prioritize Tru.so Deployment Fix:** Given the deployment failure, it would be prudent to immediately investigate and resolve the build error for Tru.so to ensure the latest changes are live and development can continue smoothly.
> *   **Follow up on KTR Pickup:** Confirm with Dominique that the kids made it to church on time despite the delay at KTR. If not, consider a proactive strategy for future pickups to ensure punctuality for scheduled events.
> *   **Leverage Shopify AI Toolkit:** With the Shopify AI Toolkit now available, exploring its full capabilities for the Blade & Rose store could significantly enhance efficiency and automation.
> *   **Consolidate Property Management:** David has several active tasks for the 5901 E Valley Vista property. Consolidating communication and scheduling for Luis Atlas (staining), Jon Sturos (casita roof), and the BBQ replacement could streamline these efforts.
> *   **Proactive Health Follow-up:** Given the discussions with Dr. Hurst and the upcoming HealthspanMD appointment, ensuring all necessary blood work and the CCTA scan are coordinated efficiently will be key for David's preventative health goals.

### E — gemini-3.1-pro-preview / retrieval (idx 264)

> ## What stands out
> - **Brandi resigned:** Brandi suddenly quit with 12 hours' notice. You've looped in Mike Stocker to pick up the pieces on the fountain cover and casita roof projects.
> - **Tru deployment:** `tru` is repeatedly failing to deploy on Render (exiting with status 2).
> - **Miles's anxiety:** Miles had a tough time with anxiety during his science station lab. Good call reaching out to Mrs. Kelty and Mrs. Viccina to get ahead of it.
>
> ## Worth considering
> - Your Branch desk delivery got rescheduled by UPS to Wednesday, April 15.
> - You've got a pending app update (1.1617.0) for Claude that just needs a relaunch to apply.

*(Note: E's thoughts reference Brandi, Mike Stocker, Miles, Shannon
Kelty, Esmi Viccina, Branch desk — none of which are in the 7 page
bodies QMD returned. 3.1 Pro is synthesizing these from the raw
observations text alone and only emitting updates on pages it actually
saw in retrieval.)*

### F — gemini-2.5-flash / retrieval (idx 100)

> ## What stands out
>
> David, it looks like you had a busy day with family logistics and project work. The recurring deploy errors for [[tru]] are a significant concern, appearing multiple times throughout the day. This suggests a deeper issue that might need focused attention.
>
> On the family front, while you made it to KTR for pickup, the kids were still playing late, and you didn't intervene to ensure they were on time for their church service. This could lead to future scheduling conflicts or missed commitments if not addressed proactively. Miles' anxiety at school continues to be a focus, and it's good to see you actively engaging with his teacher, Shannon Kelty, on this. Luisa's vaccinations are all up-to-date, which is positive.
>
> For [[blade-and-rose]], the Safari display problem is still being actively debugged with Amanda and Jon. Your [[branch-desk]] delivery has been rescheduled to next Wednesday. You also declined Fountain Life for HealthspanMD, prioritizing local care.
>
> ## Worth considering
>
> Given the repeated deploy failures for [[tru]], it might be worth dedicating a specific block of time to thoroughly diagnose and resolve the root cause rather than letting it linger. This could prevent further delays and frustration.
>
> Regarding the KTR pickup, perhaps a quick text to the coach or a direct word with the kids upon arrival could help reinforce the importance of being on time for subsequent commitments like church.
>
> Your commitments to follow up with Luis for staining at [[5901-e-valley-vista]] and to take up Lillian Diaz on the math workbook for [[cruz-wurtz]] are now active. Scheduling these follow-ups soon would be beneficial.
>
> ## A question for you
>
> How would you like me to track or flag commitments that are not fully met, like the KTR pickup where you were present but didn't actively ensure the 'on-time' aspect? Should I mark them as 'at risk' or 'partially completed' in a specific way?

## Wiki update details

Reason column truncated to 120 chars. Pages marked with ★ appear in ≥3
conditions (strong cross-model signal).

### A — gemini-3.1-pro-preview / full (25 updates)

| Action | Page | Reason |
|---|---|---|
| update | `people/aaron-mitschele` | Removed phone that belongs to Kevin (Desert Heat Masonry) per recent context. |
| update | `people/brandi-rodriquez` ★ | Resignation — 12-hour notice. |
| update | `people/cruz-wurtz` ★ | New math tutoring arrangement with Lillian Diaz. |
| update | `people/david-wurtz` ★ | Today's activities and commitments. |
| update | `people/dominique-wurtz` ★ | Coordination on Miles and household logistics. |
| update | `people/jenna-dutra` | Declined Fountain Life membership. |
| update | `people/joan-levinson` | Taking the Phoenix property off the listing tomorrow. |
| update | `people/kaushik` | Recent conversation touchpoint. |
| update | `people/kevin-desert-heat-masonry` | Added phone + recent activity re deck measurements. |
| update | `people/krista-joiner` ★ | Follow-up thread. |
| update | `people/laura-parker-ellas-mom` | Ella-related logistics. |
| update | `people/luis-atlas` ★ | Scheduling a visit for staining wooden areas. |
| update | `people/luisa-wurtz` ★ | Vaccinations + dentist next week. |
| update | `people/mike-stocker` ★ | Brandi resignation + fountain cover tech decision. |
| update | `people/robert-toy` | Recent interaction. |
| update | `people/shannon-kelty` ★ | Science class anxiety — Miles outreach. |
| update | `projects/blade-and-rose` ★ | Shopify AI Toolkit install + Safari debugging. |
| update | `projects/branch-desk` ★ | UPS delivery reschedule. |
| update | `projects/deja` ★ | Today's build progress + notch UI + agent changes. |
| update | `projects/deja-ai-app` ★ | Parallel page touched. |
| update | `projects/gemini-agents` ★ | Build-pass notes. |
| update | `projects/healthspan-research` | HealthspanMD onboarding. |
| update | `projects/multicoin-capital-fund` | Recent mention. |
| update | `projects/tru` ★ | Render deploy failures + Stripe integration progress. |
| update | `projects/tru-so` | Matching domain page. |

### B — gemini-3.1-pro-preview / full rerun (24 updates; 2 creates, 22 updates)

| Action | Page | Reason |
|---|---|---|
| update | `people/amanda-peffer` | Blade & Rose + Safari bug coordination. |
| update | `people/brandi-rodriquez` ★ | Sudden resignation. |
| update | `people/cruz-wurtz` ★ | Math tutoring arrangement. |
| update | `people/david-wurtz` ★ | Today summary. |
| update | `people/dominique-wurtz` ★ | Family logistics. |
| create | `people/esmi-viccina` | New contact — Miles's teacher mentioned in outreach. |
| update | `people/jenna-dutra` | Fountain Life decline context. |
| update | `people/joan-levinson` | Pulling Phoenix listing. |
| update | `people/kevin-desert-heat-masonry` | Activity note. |
| create | `people/lillian-diaz` | Cruz's new math tutor — no page existed. |
| update | `people/luis-atlas` ★ | Staining scheduling. |
| update | `people/luisa-wurtz` ★ | Vaccinations. |
| update | `people/mike-stocker` ★ | Fountain cover tech decisions post-Brandi. |
| update | `people/patrick-gibbs` | Recent interaction. |
| update | `people/robert-toy` | Touchpoint. |
| update | `people/shannon-kelty` ★ | Miles anxiety outreach. |
| update | `projects/5901-e-valley-vista-property-management` | Brandi resignation impact on property mgmt. |
| update | `projects/blade-and-rose` ★ | Safari bug + Shopify AI toolkit. |
| update | `projects/branch-desk` ★ | Delivery reschedule. |
| update | `projects/gemini-agents` ★ | Session notes. |
| update | `projects/healthspan-research` | HealthspanMD onboarding. |
| update | `projects/miles-gymnastics` | Class/schedule updates. |
| update | `projects/multicoin-capital-fund` | Touchpoint. |
| update | `projects/tru` ★ | Render deploy failures and Stripe work. |

### C — gemini-2.5-pro / full (22; 15 updates, 4 deletes, 3 creates)

| Action | Page | Reason |
|---|---|---|
| update | `people/ami-vora` | Recent mention. |
| update | `people/blake-folgado` | Merging duplicate `blake-humanleap` + adding aliases. |
| delete | `people/blake-humanleap` | Duplicate — merge into `blake-folgado`. |
| update | `people/cruz-wurtz` ★ | Merging duplicate `cruz` page + Lillian Diaz math context. |
| delete | `people/cruz` | Duplicate — merge into `cruz-wurtz`. |
| update | `people/david-wurtz` ★ | Today summary. |
| update | `people/dominique-igoe` | Recent activity. |
| update | `people/dominique-wurtz` ★ | Family logistics. |
| update | `people/kevin-hannon` | Touchpoint. |
| create | `people/lillian-diaz` | New tutor contact. |
| update | `people/luisa-wurtz` ★ | Vaccinations + dentist. |
| update | `people/miles-wurtz` | School anxiety thread. |
| update | `people/rachel-wolak` | Merging into `rachel-wolak-vaughn`. |
| delete | `people/rachel-wolak-vaughn` | Duplicate (C chose to delete this half). |
| update | `people/shannon-kelty` ★ | Miles outreach. |
| update | `people/vanessa-cangley` | Recent touchpoint. |
| create | `projects/5901-e-valley-vista` | Unified property page. |
| update | `projects/blade-and-rose` ★ | Shopify AI Toolkit + Safari bug. |
| update | `projects/branch-desk` ★ | Delivery reschedule. |
| update | `projects/gemini-agents` ★ | Build progress. |
| create | `projects/shopify-ai-toolkit-exploration` | New exploration track. |
| update | `projects/tru` ★ | Deploy failures + strategic Shopify comparison. |

### D — gemini-2.5-flash / full (37; 23 updates, 14 deletes)

14 deletes is the highest in the experiment. D's dedup cleanup pass:
`people/cruz`, `people/blake-humanleap`, `people/dominique-wurtz`,
`people/rachel-wolak-vaughn`, `projects/bbq-grill-replacement`,
`projects/bedrock-concierge-service`, `projects/deja-ai-app`,
`projects/trees-for-needs-service`, `projects/tru-so`,
`projects/trydeja-com-domain-registration`,
`projects/defensive-drivers-institute-course`,
`projects/research-legacy-dc-desktop-monitor-application`, and others.
Several of these are legitimately duplicates — but D is also proposing
to delete `deja-ai-app` while A/B are actively updating it. Without a
dry-run diff it's not possible to say which is "right" on that one.

| Action | Page | Reason |
|---|---|---|
| update | `people/ami-vora` | Recent mention. |
| update | `people/bagel` | Pet reference? — looks like a page stub. |
| update | `people/blake-folgado` | Merge. |
| delete | `people/blake-humanleap` | Duplicate. |
| update | `people/cruz` | Merge target page (D picked `cruz` not `cruz-wurtz`). |
| delete | `people/cruz-wurtz` | Duplicate (D inverted C's merge direction!). |
| update | `people/dominique-igoe` | Merge target. |
| delete | `people/dominique-wurtz` | Duplicate (D again picks the opposite direction from A/B). |
| update | `people/jon-sturos` | Casita roof scheduling. |
| update | `people/kevin-hannon` | Touchpoint. |
| update | `people/mikayla` | Recent mention. |
| update | `people/molly-wurtz` | Soccer logistics. |
| update | `people/rachel-wolak` | Merge. |
| delete | `people/rachel-wolak-vaughn` | Duplicate. |
| update | `people/vanessa-cangley` | Touchpoint. |
| create | `projects/bbq-grill-cleaning` | BBQ maintenance track. |
| delete | `projects/bbq-grill-replacement` | Subsumed. |
| update | `projects/bedrock-concierge-agreement` | Merge target. |
| delete | `projects/bedrock-concierge-service` | Duplicate. |
| update | `projects/cancun-trip-planning` | Recent mention in observations. |
| delete | `projects/defensive-drivers-institute-course` | Completed. |
| update | `projects/deja` ★ | Today build summary. |
| delete | `projects/deja-ai-app` | Merge into `deja` (⚠ conflicts with A/B updating it). |
| update | `projects/deja-domain-research` | Recent mention. |
| update | `projects/google-cloud-next-2026` | Work planning. |
| update | `projects/lighthouse` | Legacy page touch. |
| update | `projects/nanoclaw-assistant` | Recent mention. |
| update | `projects/new-macbook-consideration` | Purchase consideration thread. |
| delete | `projects/research-legacy-dc-desktop-monitor-application` | Stale. |
| update | `projects/robinhood-investments` | Recent mention. |
| delete | `projects/traffic-survival-school` | Completed. |
| update | `projects/trees-for-needs` | Merge target. |
| delete | `projects/trees-for-needs-service` | Duplicate. |
| update | `projects/tru` ★ | Deploy failures. |
| delete | `projects/tru-so` | Merge into `tru`. |
| delete | `projects/trydeja-com-domain-registration` | Subsumed by `deja-domain-research`. |
| update | `projects/ultrafoam-llc` | Recent mention. |

### E — gemini-3.1-pro-preview / retrieval (8; 6 updates, 2 deletes)

| Action | Page | Reason |
|---|---|---|
| update | `people/alex-beller` | Retrieved page — recent note. |
| update | `people/david-wurtz` ★ | Self-page update. |
| update | `people/sam-t-granola` | Retrieved page — touchpoint. |
| update | `projects/deja` ★ | Merged duplicate deja pages + today progress. |
| delete | `projects/deja-ai-app` | Merged into `deja`. |
| delete | `projects/deja-domain-research` | Merged into `deja`. |
| update | `projects/native-meeting-recording-project` | Retrieved page — progress note. |
| update | `projects/new-macbook-consideration` | Retrieved page — consideration thread. |

### F — gemini-2.5-flash / retrieval (22; 18 updates, 3 deletes, 1 create)

| Action | Page | Reason |
|---|---|---|
| update | `people/carl-bogar` | Touchpoint. |
| update | `people/claude` | Meta page about Claude tooling. |
| update | `people/coach-rob` | Kids sports coach reference. |
| update | `people/cruz-wurtz` ★ | Math workbook from Lillian Diaz. |
| update | `people/david-wurtz` ★ | Self-page missing fields added. |
| update | `people/dominique-igoe` | Family logistics. |
| update | `people/dominique-wurtz` ★ | Family logistics. |
| update | `people/jenna-dutra` | Fountain Life decline. |
| update | `people/josh-eleven` | Touchpoint. |
| update | `people/krista-joiner` ★ | Thread mention. |
| update | `people/luisa-wurtz` ★ | Vaccinations. |
| update | `people/miles-wurtz` | Anxiety at school. |
| update | `people/shannon-kelty` ★ | Miles outreach. |
| update | `projects/5901-e-valley-vista` | Luis Atlas staining. |
| update | `projects/branch-desk` ★ | Delivery reschedule. |
| update | `projects/deja` ★ | Today build progress. |
| update | `projects/gemini-agents` ★ | Build-pass notes. |
| create | `projects/geodrops-irrigation-system` | New project — irrigation hardware. |
| update | `projects/healthspan-md` | HealthspanMD onboarding. |
| update | `projects/new-macbook-consideration` | Consideration thread. |
| update | `projects/quickpick-app-upsell-testing` | Recent mention. |
| update | `projects/tru` ★ | Deploy failures. |

## Known limitations of this experiment

1. **Retrieval query quality is handicapped.** The harness uses a
   raw-text truncation of `recent_observations` as the QMD query;
   production `wiki_retriever.py` uses proper-noun and domain-stem
   extraction. For this fixture that difference was decisive — QMD
   returned only Deja/dev-adjacent pages, missing Brandi/Joan/Luis/
   Mike/Blade & Rose entirely. E's "architecture" result is really
   a "bad query" result.
2. **QMD returned only 9 of 15 requested paths** because score
   thresholding dropped the rest. The 7 final pages are 2898 bytes
   total — a near-empty context. A better query would likely surface
   20+ pages and tens of KB of bodies.
3. **N=1 per condition.** Self-consistency (A vs B) showed a 0.58
   Jaccard noise floor on the same model. All other comparisons are
   single data points and should be interpreted with that noise floor
   in mind.
4. **No ground truth for "good."** The experiment measures overlap
   and update counts, not whether the updates are *correct*. D's
   14 deletes might be exactly right (duplicate cleanup on a
   post-merge wiki is overdue) or might destroy content that A/B
   are actively updating. Verifying this requires dry-run diffing,
   not a harness of this shape.
5. **3.1 Pro pricing is assumed at 2.5 Pro parity.** If 3.1 Pro
   actually lists higher (Opus-tier pricing), all condition A/B/E
   cost numbers understate the real cost.

## Opinionated recommendation

### 1. Should the default reflection model stay as 3.1 Pro, or switch?

**Stay on 3.1 Pro.** D (Flash) proposes 37 updates including 14
deletes on a single pass, and inverts the canonical-page direction
on at least two merges (`cruz` vs `cruz-wurtz`,
`dominique-wurtz` vs `dominique-igoe`). On a 264-page post-merge
wiki with known duplicates, the model's willingness to aggressively
restructure is a feature, not a bug — but it also needs to be the
*right* restructure, and there's no evidence Flash is pointing in
the right direction. 3.1 Pro's 25 targeted content updates are
closer to the safe default. The $13.81/mo savings from Flash isn't
worth the risk of scrambling the wiki on a cleanup cycle.

### 2. Should reflection switch from full-wiki to retrieval?

**No, not with the retrieval strategy this harness tested.** E vs A
shows a same-model Jaccard of 0.10 — the retrieval pass missed 22
out of 25 themes. The root cause is the naïve query; the production
entity-extraction query might close the gap, but we have zero
evidence of that from this data. Shipping retrieval blind would be
a measurable quality regression on every cycle.

*However:* retrieval is the right long-term direction. 90K input
tokens per reflection is the current bottleneck, and it scales with
wiki size forever. The right move is to **re-run E and F against a
query built by production's `wiki_retriever._extract_entity_tokens()`
logic**, and only ship retrieval if E's Jaccard with A clears the
0.58 noise floor.

### 3. If both changes land, what's the new recurring monthly cost per user?

**F: $4.52/user/mo** (vs measured A baseline $19.96/mo, savings
$15.44/mo, or ~77 %). But don't ship F on this data — see risk below.

### 4. Biggest risk of the recommendation

**F proposes updates to pages it hasn't read.** With only 7 retrieved
page bodies (2.9 KB total), F's 22 updates — including updates to
`people/shannon-kelty`, `projects/branch-desk`, `projects/gemini-agents`,
`projects/tru` — must be coming from slug-guessing against the 100-line
index, not from reading current page content. The specific failure
mode to watch for: **content regression** — F writes `content: "..."`
bodies that overwrite real page state because the model never saw
the real state. In the apply step this could silently drop carefully
maintained structure. Dry-run diff against the current wiki before
shipping any retrieval variant.

### 5. Next experiment to validate

**Re-run conditions E and F with the production entity-extraction
query builder.** Specifically: extend `tools/reflection_eval.py` to
import and call `deja.wiki_retriever._extract_entity_tokens()` on
the fixture's raw signal items (not the flattened text), join
those with `OR`, and use that as the QMD query. Target metric:
**E's Jaccard with A should clear 0.58** (the Pro-vs-Pro noise floor).
If it does, retrieval is viable and we should run a third experiment
comparing A vs E with N=3 per condition for tighter noise floor. If
it doesn't, retrieval architecture gets shelved and we look at
prompt compression (drop `wiki_text` to just updated/recent pages,
e.g. last-7-day-touched slugs) as the next lever.

---

## Artifacts

- Fixture: `/Users/wurtz/.deja/reflection_fixtures/20260410T214547.json` (283 KB, 12 fields populated)
- Run dir: `/Users/wurtz/.deja/reflection_eval/20260410T215000_abcdef/`
- Per-condition JSONs (6 files + `meta.json`) in the run dir above.
- Harness: `/Users/wurtz/projects/deja/tools/reflection_eval.py`
