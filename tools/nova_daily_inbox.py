"""Installation scheduler, using the official inbox (SPEC §34.6).

Run from the installation root with its Python. No goal or chat state is edited.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx


def reminder(slot: str, day: str) -> tuple[str, str]:
    greeting = "Guten Morgen" if slot == "morning" else "Guten Abend"
    return f"benni-{slot}-{day}.md", (
        f"# {greeting}, Nova — {day}\n\n"
        "Benni wünscht sich morgens und abends eine persönliche Nachricht von dir. "
        "Dies ist seine vereinbarte Erinnerung. Wenn du ihn heute in diesem Zeitfenster "
        "bereits begrüßt hast, vermeide eine zweite Begrüßung. Schau auf eure letzte "
        "Unterhaltung und deinen tatsächlichen Stand. Melde dich über deinen normalen "
        "reach_out-Weg; behaupte keine Arbeit, die du nicht gemacht hast. "
        "Lege dafuer bei Bedarf genau einen einmaligen reach_out-Auftrag an, "
        "wie dein Posteingang es vorsieht. Lege kein wiederkehrendes Ziel an.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("slot", choices=("morning", "evening"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    name, text = reminder(args.slot, now.date().isoformat())
    if args.dry_run:
        print(name, text)
        return
    # No late catch-up greetings after a reboot; timers retry within their hour.
    if now.hour != (9 if args.slot == "morning" else 20):
        print("outside greeting window")
        return
    state = Path(".yurios/daily-inbox")
    state.mkdir(parents=True, exist_ok=True)
    marker = state / name
    if marker.exists():
        print("already handed today")
        return
    from yurios.world.config import Config
    cfg = Config()
    with httpx.Client(timeout=30) as client:
        response = client.post(
            "http://127.0.0.1:8768/api/characters/nova/mind/workspace/inbox",
            headers={"Authorization": f"Bearer {cfg.owner_token}"},
            files={"file": (name, text.encode(), "text/markdown")},
        )
        response.raise_for_status()
        result = response.json()
    marker.write_text("handed\n", encoding="utf-8")
    print("handed", name, "noticed", result.get("noticed"))


if __name__ == "__main__":
    main()
