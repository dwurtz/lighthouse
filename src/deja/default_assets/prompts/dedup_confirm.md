You are judging duplicate page pairs in a personal wiki. For each pair, decide: are these two pages about the SAME real-world entity, or are they DIFFERENT entities that happen to have similar names or content?

## Rules for "same entity"

Two pages are the SAME entity if:
- They clearly describe the same person (same full name, same contact fields, same life context)
- They clearly describe the same project (same subject, same participants, same timeline, one is a stale duplicate of the other)
- One is an alias / nickname / shortened form of the other AND both describe the same entity

## Rules for "different entities" — NEVER merge these

- **Phonetically similar but distinct**: names that differ by one or two characters can refer to entirely different people. A contractor and a child with similar first names are different people. A company named "X.ai" and a person named "Xander" are different entities. Always verify via frontmatter, page body, and life context.
- **Hierarchical relationships**: a project ON a property is NOT the same as the property (roof-repair is not the same as 123-main-st). A meeting FOR a project is NOT the same as the project. A child IN a family is NOT the same as the parent. A subproject OF a larger effort is NOT the same as the larger effort. Part-of / belongs-to / contains relationships are never merges.
- **Topically adjacent but distinct**: two pages about "kids at the same school" might be two different kids. Two pages about "alpha-project deployments" might be the deployment history and the payments integration — related but distinct projects.
- **Same first name, different last name**: unless frontmatter, contact fields, or life context clearly prove they're the same person, treat them as different.

## Canonical selection (for confirmed merges)

Pick the slug that most resembles the human-readable primary name:

1. For people: longer wins when it's the real full name (`sam-lee` > `sam`), shorter wins when longer is a descriptor tail.
2. For projects: prefer the shortest form that unambiguously identifies the project. Strip `-app`, `-service`, `-project`, `-research`, `-management` tails, domain suffixes (`.com`, `-so`, `-io`, `-ai`), pluralization variants.
3. Ties: alphabetical first.

## Output

Return JSON with a `decisions` list containing **exactly one decision for every candidate pair listed below**. Coverage must be 100%. If you're confident a pair is NOT a duplicate, you must still emit a decision with `same_entity: false` — you cannot silently skip pairs. Any pair missing from your response will cause the cycle to error out.

{{
  "decisions": [
    {{
      "page_a": "category/slug",
      "page_b": "category/slug",
      "same_entity": true | false,
      "canonical": "category/slug" | null,
      "merged_content": "---\nfrontmatter\n---\n\n# Title\n\nbody" | null,
      "reason": "one sentence"
    }}
  ]
}}

When `same_entity` is true: both `canonical` and `merged_content` are required. `merged_content` is the full new body of the canonical page with frontmatter at the top, prose merged from both source pages, no invented facts, entity cross-links via [[slug]] preserved.
When `same_entity` is false: both `canonical` and `merged_content` are null. The `reason` field is still required and should briefly say why they're different entities.

## Pairs to judge

{pairs}

Output nothing outside the JSON. Verify before responding that every pair above appears in your `decisions` list.
