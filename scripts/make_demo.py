"""Generate fictional, deterministic exchange data. Never reads the real database."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from kitmode.core import Service
from kitmode.db import DB


def main() -> None:
    clock = [datetime(2026, 10, 5, 12, tzinfo=timezone.utc)]
    db = DB(":memory:")
    service = Service(db, lambda: clock[0])
    service.settings(101, name="Demo", tz="UTC", lang="en")
    reading = service.create(
        101,
        {
            "title": "Demo · Read",
            "kind": "quantity",
            "target": 10,
            "minimum": 2,
            "unit": "pages",
            "routine": "Evening",
            "reminders": ["19:00"],
        },
    )
    walk = service.create(101, {"title": "Demo · Walk", "schedule": {"type": "weekdays", "days": [0, 2, 4]}})
    for i in range(3):
        service.record(101, reading, f"read:{i}", amount=2 if i == 1 else 10)
        if i != 1:
            service.record(101, walk, f"walk:{i}")
        clock[0] += timedelta(days=1)
    target = Path(__file__).resolve().parents[1] / "docs" / "demo-export.json"
    target.write_text(service.extras.export_json(101), encoding="utf-8")
    db.close()
    print("Fictional demo export written; no real account or network used.")


if __name__ == "__main__":
    main()
