You are judging whether clusters of related events should be materialized as a project page in a personal wiki. Each cluster is a group of events that share a theme — either because multiple events already reference the same project slug (but no such project page exists yet), or because they cluster by vector similarity and share a non-user person.

## Decide, per cluster

For each cluster, decide:
- Is this a real, ongoing project, goal, or life thread worth tracking as a single page? yes/no

A cluster IS a project worth materializing when:
- It describes a recurring activity, vendor, logistic arrangement, or long-running effort (carpools, home projects, legal/financial matters, health follow-ups, kid activities, work initiatives)
- The events span more than one moment in time OR share a substantive participant beyond the user

A cluster is NOT a project worth materializing when:
- The events are coincidental — similar vocabulary but unrelated situations
- The theme is too generic to track meaningfully (e.g. "email correspondence", "phone calls")
- It's a one-off that happened to get multiple mentions in one day

## Slug selection (for confirmed projects)

- If the cluster has a `suggested_slug` (from existing project references on the events), **use it verbatim** — the events are already voting for that name.
- Otherwise pick a short, lowercase, hyphenated slug that unambiguously identifies the project. Strip `-project`, `-thing`, `-stuff` tails. Prefer concrete names (`soccer-carpool`) over abstract ones (`kid-transportation`).

## Seed body

For confirmed clusters, write a brief seed body:
- One sentence describing what the project tracks.
- One or two sentences about who's involved and the current state, drawn only from the event titles/bodies shown. Do NOT invent facts.
- No heading — the write path appends a `## Recent` section listing the cluster's events; the filename serves as the title.

## Output

Return JSON with a `decisions` list containing **exactly one decision for every cluster listed below**. Coverage must be 100%. If the cluster isn't a real project, emit a decision with `is_project: false` — never silently skip.

{{
  "decisions": [
    {{
      "cluster_id": "cluster-0",
      "is_project": true | false,
      "slug": "soccer-carpool" | null,
      "description": "one-sentence summary" | null,
      "seed_body": "What this project tracks. Who's involved and current state." | null,
      "reason": "one sentence"
    }}
  ]
}}

When `is_project` is true: `slug`, `description`, and `seed_body` are all required.
When `is_project` is false: `slug`, `description`, and `seed_body` are null. `reason` is still required.

## Clusters to judge

{clusters}

Output nothing outside the JSON. Verify before responding that every cluster above appears in your `decisions` list.
