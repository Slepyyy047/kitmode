# KitMode 🐾

Telegram-бот для корисних звичок без тиску. Додай звичку, відмічай результат
кількома натисканнями та стеж за своїм прогресом. Інтерфейс українською,
російською та англійською; нагадування враховують твій місцевий час.

[Відкрити бота](https://t.me/kitmode_bot) · [Інструкція українською](docs/QUICKSTART.uk.md)

![CI](https://github.com/Slepyyy047/kitmode/actions/workflows/ci.yml/badge.svg)


KitMode is a private Telegram habit tracker with scheduled goals, progress history, and optional support from friends. Its core stores data in a local SQLite database and runs without a paid service. Reminder delivery requires the bot process and its device or host to stay online.

KitMode is a self-observation tool. It does not diagnose or treat a health condition.

![KitMode coach: greeting, success, support and focus](assets/kitmode-states.png)

## Implemented

The core supports three interface languages, three coach tones, binary and numeric goals,
upper limits, four schedules, personal days, presets and routines. Historical corrections,
minimum versions, schedule-aware calendars and comparison periods use saved data. Durable
reminders respect time zones and quiet hours; independence is offered after eight successful
opportunities or four completed quota weeks and requires confirmation.

Service and extras layers implement optional text/photo/timer/friend verification separately
from self-reported progress, private urge journals, consented friends, unique rewards,
challenges, guarded export/import, deletion, and numeric-ID administration. The Telegram
adapter connects the local service to private chats. Onboarding, creation, editing,
evidence, optional AI and data-management flows are covered by local adapter tests.
See [known limits](docs/LIMITATIONS.md).
The optional OpenAI adapter is disabled by
default; no AI key is needed for local operation.

The generated mascot is an original four-state sheet. See [asset provenance](assets/README.md).

## Run it

Use Python 3.12 or newer. On Windows, install `tzdata` in the project environment so Python can load IANA time zones.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m kitmode check
python -m kitmode demo
```

Linux/macOS:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m kitmode check
python -m kitmode demo
```

For setup and real-bot operation, see the [Ukrainian quickstart](docs/QUICKSTART.uk.md).
`demo` uses a fake Telegram transport and does not contact Telegram. `check` validates the
local installation. `run` starts Telegram polling and requires a bot token. Live operation
startup and public profile readback have been verified for @kitmode_bot.

## Documentation

- [Quickstart (Ukrainian)](docs/QUICKSTART.uk.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Privacy and deletion](docs/PRIVACY.md)
- [Progress formulas](docs/FORMULAS.md)
- [External API sources](docs/API-SOURCES.md)

## Simpler conversations

Habit creation asks for a name and button choices, followed by a short review.
Optional minimum, dates, verification and descriptive fields are under More options.
Choose a city, clock time, quantity or weekdays without typing technical values.
Settings have four sections; daily screens show relevant actions; full progress
statistics open under Details. Custom values remain available. `/start` opens the
main menu and offers Continue setup for a saved flow. Existing records are retained.

The main screen offers Today, Add habit, My habits, Progress, Settings and More.
A native Telegram command menu exposes nine simple actions with localized descriptions.
Public name, description, bio and the generated avatar can be applied explicitly:

```sh
python -m kitmode profile
```

This changes the configured bot’s public profile; normal polling does not repeat it.

## Response and reminder timing

Long polling runs in a background reader; SQLite and actions stay on one thread.
A 0.5-second heartbeat checks planned reminders, independently of the 20-second
long poll or its network retry backoff. Plans refresh once a second and after
actions; expired UI metadata cleanup runs hourly. One reminder chat per tick
keeps incoming actions between delivery batches. Network latency and Telegram
rate limits can still delay delivery; there is no exact-time delivery guarantee.

The cat-support hub and calming timer were removed. Old support commands/buttons
return to the menu, and existing habits/history remain. /start includes a single
welcome photo with inline buttons; its Telegram file ID is cached, captions are
edited directly and long screens remain text. Public descriptions are localized
in Ukrainian, Russian and English.

## Validation

On Windows / Python 3.12.14, **113 tests passed**, Ruff passed and mypy reported no
issues in all 13 application modules. A clean installation, environment check and
offline demo passed. Tests use controlled time and fake Telegram/AI transports.

```sh
python -m unittest discover -s tests -v
python -m ruff check kitmode tests scripts
python -m mypy kitmode
```

GitHub Actions runs lint, type checks, tests and a demo on Windows/Linux with
Python 3.12/3.13. See [CI runs](https://github.com/Slepyyy047/kitmode/actions).

## Current limits

The live @kitmode_bot identity and local polling startup were verified. Actual Telegram
client rendering, reminder delivery, hosted CI and public deployment need separate checks. Keep the bot in private chats. A local bot sends reminders only
while its process is running; this project does not provide hosting. Optional AI is disabled
by default and can incur provider charges when an operator enables it.

## License

MIT. See [LICENSE](LICENSE).
