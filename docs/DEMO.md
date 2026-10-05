# Demonstration (simulated, not a live Telegram transcript)

`python -m kitmode demo` uses the real SQLite/service/reminder components and a
fake transport. It creates a fictional reader, delivers a scheduled notification,
records ten pages, undoes that record, records the predeclared two-page minimum,
closes/reopens SQLite and checks that progress and unique XP survived.

Expected report:

```json
{
  "mode": "SIMULATED; no Telegram or AI request",
  "habit": "Read",
  "minimum": 1,
  "completed": 1,
  "xp": 10,
  "restart": "persisted",
  "undo": "verified",
  "reminders": "one, cancelled after completion"
}
```

Example chat content (illustrative; no real personal records):

```text
KitMode: Привіт, я KitMode — допоможу будувати звички без тиску.
User: Europe/Kyiv
KitMode: Перевір звичку: Читати · 10 сторінок · щодня · мінімум 2
User: [Створити]
KitMode: Час для твого плану 🐾
User: [Додати кількість] 2
KitMode: Запис збережено.
User: [Прогрес]
KitMode: Повністю: 0 · мінімум: 1
```

`tests/test_telegram.py`, `tests/test_flows.py` and `tests/test_final_core.py` exercise
onboarding, preview/confirmation, evidence, support, administration, retry and deletion
with actual update dictionaries and opaque callbacks
through the chat handler. `tests/test_core.py` and `test_regressions.py` control the
clock instead of waiting for real hours or DST changes. The live checklist is separate.

`docs/demo-export.json` contains only fictional reading/walking records and can be
used to exercise import preview. Regenerate it with `python scripts/make_demo.py`
after installing the package. Generation always uses an in-memory database.
