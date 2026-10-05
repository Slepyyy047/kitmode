# Privacy and data handling

KitMode is designed for personal habit tracking. Sensitive habits and urge notes can reveal private information. Use the bot only in a private Telegram chat, protect the device and database file, and avoid placing secrets or identifying details in free-text notes.

## What is stored

The local SQLite database can contain Telegram user IDs, optional display names and settings, habit names and schedules, records, notes, friend relationships and consent, verification status, timer timestamps, sensitive-habit urge entries, support feedback, and reminder jobs. It also stores Telegram update IDs and opaque callback tokens needed for duplicate and stale-button handling.

SQLite is stored as a plain local database file. KitMode does not encrypt that file; operating-system account and disk protections apply. Anyone with file access may be able to read it.

## Photos and Telegram

Telegram owns the original uploaded message and its retention history. Where a photo verification flow is used, KitMode stores a Telegram `file_id` as evidence metadata; it does not promise deletion of the Telegram message or Telegram-side copies. Import attachments are fetched temporarily for validation and are not saved as a photo archive by KitMode. Avoid photos for sensitive habits. Photos and timers are not reliable proof that an activity occurred.

## Sharing and AI

Friend access requires an invitation and relationship; habit visibility is configured per habit. Sensitive habits are excluded from friend visibility and progress cards. Verification by a friend has a separate consent setting and can be revoked.

AI is disabled by default. The optional provider receives only the prompt text supplied to the AI feature; KitMode does not automatically add journals, evidence, or sensitive-habit details to that request. A user must consent, and the operator must configure the provider and approve any spending. The request uses `store: false`, but this setting is not a guarantee that the provider instantly erases all data or that no retention applies. Review the provider's current data controls before enabling AI.

## Export and deletion

JSON export includes profile settings, habits, versions, records, pauses, and urge entries. CSV export includes habit and record fields; sensitive habit titles are replaced with a neutral label. Exports can contain sensitive data: store them privately and remove them when no longer needed.

Deleting a profile removes its KitMode database records through the application. It cannot remove messages from Telegram chat history, data on a friend's device, external provider records, or operator-created backups. Maintain any manual backup securely and remove it within 7 days; KitMode does not run a backup cleanup job.

## Retention and deployment

There is no remote KitMode server in the local run mode. Local reminders require the device and process to remain available. A public deployment changes the data controller, access, backup, and retention risks; document those arrangements before hosting real users' data.
