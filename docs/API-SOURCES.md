# API and time-zone sources

Checked on 2026-10-05. These primary sources inform the implementation and the limits described in the other docs.

- [Telegram Bot API](https://core.telegram.org/bots/api) — bot methods, updates, callback data, file access, and `getUpdates` polling.
- [Telegram Bot FAQ: avoiding limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this) — Telegram's guidance on message rate limits.
- [Python `zoneinfo` data sources](https://docs.python.org/3/library/zoneinfo.html#data-sources) — time-zone database lookup and the Windows `tzdata` package fallback.
- [Python `sqlite3` transaction control](https://docs.python.org/3/library/sqlite3.html#transaction-control) — Python SQLite transaction behavior.
- [Python `urllib.request.urlopen`](https://docs.python.org/3/library/urllib.request.html#urllib.request.urlopen) — standard-library HTTPS request interface.
- [OpenAI text generation guide](https://developers.openai.com/api/docs/guides/text) — Responses API text generation.
- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data) — provider data retention and storage controls. `store: false` should not be described as guaranteed immediate deletion.

The live checklist distinguishes local/simulated verification from real Telegram testing. Links above are reference material; they do not imply that a live bot or paid API request was tested.

## Public profile and command menu (checked 2026-10-05)

[Telegram profile photo](https://core.telegram.org/bots/api#setmyprofilephoto),
[descriptions](https://core.telegram.org/bots/api#setmydescription),
[commands](https://core.telegram.org/bots/api#setmycommands) and
[menu button](https://core.telegram.org/bots/api#setchatmenubutton). Static profile
photos use new multipart JPEG uploads. Text and command scope/language variants
are read back after setup to confirm the saved public settings.

Timing and media revision (2026-10-05):
- https://core.telegram.org/bots/api#getupdates — long-poll timeout and offset acknowledgment.
- https://core.telegram.org/bots/api#sendphoto — reuse a photo file_id and attach an inline keyboard.
- https://core.telegram.org/bots/api#editmessagecaption — edit photo captions directly.
- https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this — retain message pacing and handle 429 retry_after.
