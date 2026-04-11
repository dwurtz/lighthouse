# Déjà — Use Cases

Brainstorm of who Déjà is for and the jobs it does for them. Organized by the underlying job-to-be-done, with landing-page-ready headline candidates for each group.

---

## 1. Memory recall — "the gutter guy"

*Your second brain for the names, numbers, and details you'd otherwise lose.*

- The contractor who fixed your gutter two years ago
- That sushi place your friend recommended in passing
- The model number of the vacuum you liked before it broke
- The stretch your PT showed you that helped your shoulder
- Why you picked Postgres over MySQL on that old project
- The random password hint you wrote in a note two laptops ago

**Headline candidate:** *"Remember everything. Even the stuff you forgot to remember."*

---

## 2. Time tracking & billing — "the lawyer / freelancer"

*Auto-captured timesheets from the work you were already doing.*

- Lawyers billing 6-minute increments per client
- Consultants who jump between 4 projects a day
- Agencies invoicing for meetings, Slack threads, doc reviews
- Contractors reconstructing last week's hours on Friday afternoon
- "Déjà, generate my timesheet for Acme Corp this week"

**Headline candidate:** *"Stop reconstructing your week on Friday afternoon."*

---

## 3. Meeting memory — "the standup that writes itself"

*Every meeting is captured, summarized, and linked to the people + project.*

- Auto-notes for every Zoom/Meet/in-person
- "What did I commit to in the 1:1 with Sara?"
- Standup prep: "What did I ship yesterday?"
- Follow-ups auto-detected and surfaced
- Decision history — "why did we go with option B?"

**Headline candidate:** *"Your meetings, remembered for you."*

---

## 4. Relationship CRM — "the personal touch at scale"

*Remember the human details — so you show up like someone who cares.*

- Kids' names, spouse's job, dog's name
- What you talked about last time
- Birthdays mentioned once in passing
- Favors owed and done
- "Who was that founder Josh introduced me to at the dinner?"

**Headline candidate:** *"Remember the details that make people feel seen."*

---

## 5. Home & life admin — "the household COO"

*A running log of the invisible work of owning a life.*

- Appliance warranties, paint colors, contractor contacts
- Car maintenance from email receipts
- Bills, renewals, subscriptions audit
- Home inventory for insurance
- "When did we last service the HVAC?"

**Headline candidate:** *"Your house's memory, not yours."*

---

## 6. Research threads — "the breadcrumb trail"

*Every article, tab, and conversation on a topic, stitched together.*

- Everything you researched about moving to Portugal
- All the Roth IRA posts you've saved over 3 years
- The baby-name contenders scattered across 12 conversations
- That one quote from a book you half-remember

**Headline candidate:** *"Pick up any research thread where you left off."*

---

## 7. Parenting co-pilot — "the other parent's brain"

*The stuff one parent usually holds in their head, shared.*

- Permission slips, school schedules, teacher names
- Kids' friends' parents' contacts
- Medication dosages, pediatrician notes
- Milestones, firsts, funny quotes
- Carpool rotations, playdate history

**Headline candidate:** *"Carry less in your head. Be more present with them."*

---

## 8. Sales / founder pipeline — "the lightweight CRM"

*For people who should use Salesforce but won't.*

- Every lead conversation auto-captured
- Last-touch dates without logging anything
- Personal details from intro calls
- Follow-ups owed surfaced at the right time
- "Who did I promise to send a deck to this week?"

**Headline candidate:** *"A CRM that works without the CRM."*

---

## 9. Health & self-knowledge — "patterns you can't see"

*The trends hiding in your daily life.*

- Sleep / mood / symptom correlations
- Food sensitivities you start to notice
- Medication adherence
- Therapy session prep
- "When did this back pain start?"

**Headline candidate:** *"See the patterns in your own life."*

---

## 10. Tax & expense — "the shoebox, solved"

*Deductions and reimbursements, auto-captured.*

- Contractor / freelance expense log
- Receipt trail from email + browser
- Mileage, home office, per-diem
- Reimbursable client expenses
- Year-end summary that TurboTax can ingest

**Headline candidate:** *"April 14th doesn't have to be a shoebox emergency."*

---

## 11. Travel memory — "the journal you didn't write"

*Every trip, archived without you journaling.*

- Hotels and restaurants you loved (or hated)
- Local contacts from trips
- Packing lists that worked
- "What was that coffee shop in Lisbon?"

**Headline candidate:** *"Your travel journal, writing itself."*

---

## 12. Creative resurfacing — "the idea graveyard, revived"

*Half-thoughts, voice memos, and late-night brainstorms brought back when they're relevant.*

- Book titles, song lyrics, project ideas
- Voice notes as first drafts
- "You had a thought about this exact problem six months ago"

**Headline candidate:** *"Your half-formed ideas don't have to die in Notes.app."*

---

## 13. Personal context for every AI — "bring your memory to the model"

*Déjà as the memory layer every other AI tool plugs into.*

- Ask Claude or ChatGPT a question and have it answer with *your* context — what you worked on this week, who you met, what you said
- "Claude, draft a follow-up to yesterday's meeting with the Acme team" — Déjà supplies the meeting notes
- Coding assistants that know what you were debugging an hour ago
- No more pasting context into every new chat
- Your life is the prompt

**Headline candidate:** *"The memory layer for every AI you use."*

**Implementation note:** Déjà exposes `/api/chat` over its local unix socket. An MCP server wrapping that endpoint lets Claude Code, Claude Desktop, or any MCP client query your memory and recent events directly. Current wiring status in this repo: unverified — needs confirmation before shipping this use case as a live feature.

---

## Framing options for the landing page

- **A. One hero, twelve cards** — broad appeal, risk of feeling generic. Good for viral share.
- **B. Persona-anchored** — pick 3–4 personas (the freelancer, the parent, the founder, the forgetful) and anchor each to 2–3 use cases. Sharper positioning, less overwhelming. *Current lean.*
- **C. One sharp wedge above the fold**, the rest as "also works for…" below. Best for conversion if the primary wedge is known.

## Open questions

1. Which of the 12 groups to keep, cut, or merge?
2. Persona-anchored (B) or card-grid (A)?
3. Primary wedge — the one use case the hero section hinges on. Candidates: #1 memory recall (universal, emotional) vs #2 billing (best revenue wedge).
