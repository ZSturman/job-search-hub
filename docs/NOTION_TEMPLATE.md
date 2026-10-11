# Building and connecting the Notion hub

No public duplication URL is available yet. The CLI builds the initial template; after disposable-workspace acceptance, its owner can publish a clean template link for users to duplicate.

## First template

1. Create a **new blank private page** named **Job Search Hub**.
2. Create an internal Notion integration with read, insert, and update content access in that workspace. Grant it access to the new page and descendants. Use a separate integration if you want credentials isolated from other projects.
3. Install the package locally as described in README.
4. Run `jobhub setup --create-template` and paste the new root URL. The token prompt is hidden.
5. Setup builds canonical databases beneath **Data & diagnostics**, then the five numbered pages with linked views and previous/next navigation.
6. Add role keywords and enable intended sources on **1 — My search**. Profiles initially have no personal interests, location, or pay floors. The sample Remotive source starts disabled.
7. Run `jobhub doctor`, `jobhub run --preview`, and `jobhub run`.

For a duplicate of an existing complete template, use `jobhub setup --root-url URL` without creation. Setup walks descendants of that root, discovers data sources by canonical title, and validates types, bounded options, relations, rollups, and formulas. IDs are derived, not manually configured by the user.

## Numbered pages

| Page | Focused views | Your action |
|---|---|---|
| 1 — My search | Search Profiles, Sources, Scoring Criteria | Edit search preferences and sources |
| 2 — Review jobs | Active Opportunities ordered by score | Read explanations; choose Shortlist/Apply/Skip |
| 3 — Resolve questions | Answer Bank, Qualification Requirements, Profile Facts | Review evidence and approve reusable answers |
| 4 — Apply | Planned Applications, Resume Library | Choose a PDF you created; apply at the employer URL |
| 5 — Track progress | Application outcomes, Organizations | Record submissions, interviews, offers, notes |

Advanced properties are hidden in the focused views, not removed from the functional database model. The template's full database pages retain diagnostics. The root's system paragraph shows refresh time, queue counts, pending tasks, and failures.

Applying directly in Notion is reconciled on `jobhub run`. CLI `jobs decide ID Apply` reconciles immediately. An application is a tracking record and does not submit anything. Notes and human decisions are preserved by lifecycle reconciliation.

## Canonical databases

**Opportunities, Applications, Organizations, Profile Facts, Answer Bank, Qualification Requirements, Scoring Criteria, Search Profiles, Sources, Resume Library.** Keep these canonical database titles so root discovery can find them; custom pages/views may be added around them.

The original seven-database functional model is retained. Internally the compatibility key `resume_facts` identifies **Profile Facts**; it does not enable writing. Search Profiles and Sources are user-editable; source health is system-maintained. Resume Library contains metadata and reusable type suggestions, never uploaded files.

Linked view copies under the numbered pages are ignored during discovery so the canonical databases are not mistaken for duplicates. Ambiguous canonical titles or schema mismatches fail with diagnostics instead of selecting an arbitrary database.

## API compatibility

Ordinary data-source/page operations stay pinned to **2025-09-03**. View creation/listing uses a separate **2026-03-11** header because the [Notion Views API](https://developers.notion.com/reference/create-view) requires that version. This is scoped to `/views` requests. The fixture checks these payloads and idempotency; live view creation/rendering remains a release gate.

If creation is interrupted, rerun the same `setup --create-template --root-url URL`. The builder recognizes its own root marker, reuses existing pages/databases, and fills missing relations, views, and defaults. It refuses a populated page without that marker. Preserve the root and inspect `doctor` output if a schema was manually changed; do not delete a working private hub as a troubleshooting shortcut.

## Disposable-workspace release test

Create a separate test workspace/page and integration. Use synthetic facts, a synthetic resume you wrote, and test jobs. Do not duplicate a populated personal operational hub into a public template.

- Verify setup from one root URL, a fresh build, a duplicate build, and a rerun after interruption.
- Check every table's visible columns, filters, sorting, previous/next links, and home status.
- Edit keywords/sources once through Notion and once through CLI; request plans should match.
- Apply without a resume/answer; confirm exactly one Planned application after two runs.
- Check unavailable model, unknown/closed posting, rate limit, invalid model output, and preview.
- Approve a staged fact, review an answer, select an existing resume, and manually track Submitted → Interviewing → Offer.
- Capture genuine screenshots using [these instructions](images/README.md).

Only publish an empty, clean template after this test succeeds.
