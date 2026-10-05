# Data exchange, version 1

Use Settings → JSON export for a valid source file. `/import` or Settings → Import
accepts the export as a Telegram document, validates it and shows counts before the
confirmation button writes anything. Each file is bounded to 1 MB; split larger
exchanges deliberately. This is a transport/validation bound, not a habit-count limit.

Root object: `format: "kitmode-export"`, `version: 1`, `exported_at`, `user`,
`habits`, `versions`, `records`, `pauses`, `archives`, `urges`.
Dates use `YYYY-MM-DD`; UTC timestamps include the timezone offset. Exported habits
contain their source IDs, titles and sensitivity/archive flags. Versions contain
`habit_id`, `effective` and a complete goal/schedule `spec`. Records contain
`habit_id`, `day`, `status`, `value`, `note`, `verification`, serialized `snapshot`
and `updated_at`. Accepted stored states are `full`, `minimum`, `progress`, `fail`,
`skip`, `none`. A snapshot must match the effective version for that date.

Import remaps all habit IDs to new IDs owned by the current account. It does not
import friendship, permissions, tokens, timers, awards or admin authority; exported
profile settings are informational and are not silently applied. Verification/media
evidence is stripped and cannot grant a friend's approval. Sensitive versions and
snapshots cannot be downgraded to bypass privacy checks.

Duplicate root keys, duplicate record/version IDs, nonfinite numbers, malformed dates,
unsupported fields and broken references are rejected before any mutation. A hash of
the canonical payload excluding `exported_at` prevents repeating the same import.
This detects repeat copies, not arbitrary edited files or manually merged databases.

CSV is a read-only human export of records; it is not an import format. Fields that
could become spreadsheet formulas are escaped. Sensitive titles are neutralized.
Notes and exported private journals remain private data even with neutral titles.
