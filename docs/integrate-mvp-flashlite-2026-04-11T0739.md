# Integrate MVP — Flash-Lite capability test (2026-04-11T07:39:29)

- Model: `gemini-2.5-flash-lite`  temperature=0.2  runs/variant=3
- Fixture: cruz / cruz-wurtz duplicate pair + Lillian Diaz math workbook signal
- Total cost: $0.0040

## Headline table

| Variant | Run | Dedup | Canonical | Links | FM | Reason | In tok | Out tok | Cost $ | Latency s |
|---|---|---|---|---|---|---|---|---|---|---|
| V0_control | 1 | N | - | 2 | N | N | 3247 | 271 | 0.00043 | 1.9 |
| V0_control | 2 | N | - | 2 | N | N | 3247 | 283 | 0.00044 | 2.2 |
| V0_control | 3 | N | - | 2 | N | N | 3247 | 307 | 0.00045 | 1.9 |
| V1_dedup | 1 | N | - | 2 | N | N | 3364 | 246 | 0.00043 | 4.4 |
| V1_dedup | 2 | N | - | 2 | N | N | 3364 | 246 | 0.00043 | 5.3 |
| V1_dedup | 3 | N | - | 2 | Y | N | 3364 | 257 | 0.00044 | 14.1 |
| V2_dedup_crosslink_fm | 1 | N | - | 2 | Y | N | 3477 | 259 | 0.00045 | 2.8 |
| V2_dedup_crosslink_fm | 2 | N | - | 5 | Y | N | 3477 | 296 | 0.00047 | 2.5 |
| V2_dedup_crosslink_fm | 3 | N | - | 2 | Y | N | 3477 | 266 | 0.00045 | 8.0 |

## Per-variant summary

- **V0_control**: dedup 0/3, canonical choices=—, avg links=2.0, frontmatter 0/3, reason-sensible 0/3, avg cost=$0.00044, avg latency=2.0s
- **V1_dedup**: dedup 0/3, canonical choices=—, avg links=2.0, frontmatter 1/3, reason-sensible 0/3, avg cost=$0.00044, avg latency=7.9s
- **V2_dedup_crosslink_fm**: dedup 0/3, canonical choices=—, avg links=3.0, frontmatter 3/3, reason-sensible 0/3, avg cost=$0.00046, avg latency=4.4s

## Verdict

**RED** — V1 only hit dedup in 0% of runs — Flash-Lite cannot reliably reason about dedup inside integrate. Keep dedup in reflect or upgrade.

## Fixture signals_text

```
[2026-04-10 16:42] email Lillian Diaz (lillian.diaz@isaz.edu) -> David Wurtz: "Hi David, I dropped off the math workbook for Cruz today. He was really engaged during our session — see you next week!"
[2026-04-10 16:45] iMessage You -> Nie: "Lili dropped off the new workbook for Cruz 👍"
[2026-04-10 17:10] calendar event accepted: "Cruz math tutoring — Tue 4pm (Lillian Diaz)"
```
