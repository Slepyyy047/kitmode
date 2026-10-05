# Current operational limits

- Automated tests use synthetic data and fake Telegram transports. A passing test
  suite does not guarantee delivery timing or client rendering on every device.
- Reminders require an online running process. Downtime notifications older than
  90 minutes are discarded rather than replayed. A lost Telegram send response can
  cause a duplicate outgoing message after retry; saved progress and XP remain unique.
- SQLite is plain local storage. The application does not provide encryption, cloud
  synchronization or automatic backup cleanup. Exported notes/journals need protection.
- Photos stay in Telegram; the app stores file IDs only, and neither photos nor elapsed
  timers prove real activity. Verification is separate from self-report statistics.
- Import is bounded to 1 MB/500 habits per file for safe parsing. There is no product
  limit on the user's total habit count. Modified/merged exports can evade whole-file
  duplicate detection; unchanged repeat exports are recognized by canonical hash.
- JSON interchange restores habits, historical versions, records, pauses, archives and
  private urge entries. Profile configuration is not silently applied; social permission,
  verification approval, media links, running timers and rewards are not imported.
- Weekly quota windows touching a partial week evaluate that full eligible week.
  Account-wide streaks display the longest single-habit streak; they are not a joint
  all-habits streak. See FORMULAS.md for exact definitions.
- A numeric quantity total across unlike units is not meaningful; chat displays units
  separately. Changing a habit's unit should be treated as a new measurement definition.
- Optional AI sends the explicitly entered, consented prompt, with a separate private
  context gate; no automatic history or diary context is sent. It cannot change data.
  The bot cannot reliably infer whether arbitrary free text contains unknown sensitive
  details. The user must choose the private flag for such prompts. Provider retention
  rules apply even with `store:false`. No live or paid provider request was tested.
- This version is for private chats. Group chat/group challenges and self-hosted web
  dashboards are outside this delivery.
- The Telegram interface uses simulated adapter tests for local verification. These do
  not establish real Bot API delivery, command UX in a live Telegram client, or behavior
  under Telegram outages. Verify live delivery with a test account before inviting users.
- Optional AI requires both operator configuration/spending approval and per-user
  consent. It is not a health professional, and its output is not a substitute for
  qualified care. No live or paid AI request was made for this delivery.
