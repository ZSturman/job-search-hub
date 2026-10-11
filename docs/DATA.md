# Personal data and recovery

## What lives where

| Data | Owner / location |
|---|---|
| Search keywords, exclusions, locations, languages, limits | Notion Search Profiles |
| Source targets, cadence, enabled state | Notion Sources |
| Scoring preferences, approved profile facts and reusable answers | Notion |
| Decisions, application statuses, notes | Notion |
| Original user-created resumes | User's chosen files; never moved or rewritten |
| Cataloged PDF copies and extracted unapproved facts | Private local data directory |
| Tokens and passwords | OS credential store, or explicit JOBHUB_* environment |
| Cache, model metadata, execution/notification ledgers | Local SQLite/logs |
| Resume titles, tags, IDs, availability, recommendations | Metadata in Notion; files remain local |

Cataloging copies a PDF to `data/resumes/CONTENT_HASH/resume.pdf`. Changing its filename does not create another document. Adding the same bytes updates catalog labels. Extraction writes `data/profile/pending.json` and requires human approval before sync. Do not add sensitive facts directly to Notion unless you intend to store them there; the local approval queue is the private staging step.

The Profile Facts loader distinguishes active, verified, approved-for-profile, approved-for-applications, metric-verified, and sensitive facts. Qualifications and reusable answers retain factual evidence checks. Recommendations never include resume sections or generated application documents.

New private files use mode 0600 and directories use 0700 where supported. Local files and backups are **not encrypted automatically**. Use disk encryption or an encrypted location if needed. A keychain protects credentials, not your PDF/SQLite contents. Notion stores the records you intentionally publish there under your workspace's access permissions.

## Backups

```bash
jobhub maintenance backup ~/jobhub-backup.zip
jobhub maintenance backup ~/jobhub-local.zip --local-only
```

Backups contain nonsecret config, cataloged documents, pending facts, caches/ledgers, and optionally a Notion JSON snapshot. They exclude credentials, logs, generated runtime snapshots, previous backups, and symlinks. SQLite databases are backed up consistently, including committed WAL state. Choose a new output filename; existing backups are preserved.

## Restore

```bash
jobhub maintenance restore ~/jobhub-backup.zip --destination ~/jobhub-restored
jobhub maintenance restore ~/jobhub-backup.zip --destination ~/jobhub-restored --apply
```

The first command previews. Actual restore accepts an empty destination, validates the entire archive, rejects path traversal and credential-bearing config, stages private files, and promotes them after validation. It restores local state only. Set JOBHUB_HOME to the restored directory and configure credentials separately. Reconnect to the intended hub with setup.

If the original Notion hub still exists, reconnect it. To recover its exported rows into a new hub, first create/configure that hub and import the snapshot explicitly. Restored `notion.json` is an export, not a live page backup: page-body content, views, and Notion history are not exported. Rebuild the template for navigation and schema.

## Optional one-way imports

```bash
jobhub maintenance import ~/Documents/selected-resumes
jobhub maintenance import ~/Documents/selected-resumes --apply
jobhub maintenance import ~/jobhub-restored/notion.json
jobhub maintenance import ~/jobhub-restored/notion.json --apply
```

Directory import considers PDFs only. Preview lists proposed hashes/titles without copying. Apply deduplicates by content hash and can resume after interruption.

Notion import accepts `jobhub-export-v1` JSON. Destination and source root must differ. Stable Import IDs prevent duplicate rows on restart, and a second pass remaps relations to destination IDs. Formula/rollup/time values are computed by the new hub. Retired workflow states map to Review/Planned and submission metadata maps to Manual. Credentials are never imported.

No automatic legacy migration runs at installation or setup. Existing personal data can be deliberately selected later; the source must remain untouched. Imports are not a continuous two-way sync. Preserve the export for retry/recovery and avoid editing destination imported rows until the import finishes.

## Failure behavior

Preview/inspection clients block Notion mutations; temporary stores hold execution observations. Failed required posting checks block AI advancement. Unknown posting availability/date requires review. Invalid model output preserves prior scores and selections. Application intent is still tracked. A process lock serializes refresh, capture, and mutating CLI operations. A terminated process releases the lock; rerunning resumes from durable caches and records.

Notion does not provide a client-defined unique constraint. Apply uses existing opportunity/application identities and a local process lock; interrupted relation linking is restartable. Concurrent writers on separate computers using the same hub are outside the one-user, one-computer default and require server-side coordination before being supported.
