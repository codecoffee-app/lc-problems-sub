#!/usr/bin/env python3
"""Create missing problem folders with plain config.json and data/1.json."""

from __future__ import annotations

import json
from pathlib import Path

CONFIG = {"current": 1, "limit": 100}
ONE = []
CATEGORIES = ("accepted", "errors")


def ensure_slug_dir(slug_dir: Path) -> bool:
    """Ensure slug dir has config.json and data/1.json. Returns True if created/fixed."""
    config_path = slug_dir / "config.json"
    data_one_path = slug_dir / "data" / "1.json"
    legacy_one_path = slug_dir / "1.json"

    already_ok = config_path.is_file() and data_one_path.is_file()
    if already_ok and not legacy_one_path.exists():
        return False

    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "data").mkdir(exist_ok=True)

    if not config_path.is_file():
        config_path.write_text(
            json.dumps(CONFIG, separators=(",", ":")),
            encoding="utf-8",
        )

    if not data_one_path.is_file():
        if legacy_one_path.is_file():
            legacy_one_path.replace(data_one_path)
        else:
            data_one_path.write_text(
                json.dumps(ONE, separators=(",", ":")),
                encoding="utf-8",
            )
    elif legacy_one_path.exists():
        legacy_one_path.unlink()

    return True


def main() -> None:
    root = Path(__file__).resolve().parent
    input_path = root / "input.json"
    problems_dir = root / "problems"

    with input_path.open(encoding="utf-8") as f:
        items = json.load(f)

    created = 0
    skipped = 0

    for category in CATEGORIES:
        (problems_dir / category).mkdir(parents=True, exist_ok=True)

    for item in items:
        slug = item.get("titleSlug")
        if not slug:
            continue

        for category in CATEGORIES:
            slug_dir = problems_dir / category / slug
            if ensure_slug_dir(slug_dir):
                created += 1
            else:
                skipped += 1

    print(f"Created/fixed: {created}")
    print(f"Skipped (already ok): {skipped}")
    print(f"Total items: {len(items)}")
    print(f"Categories: {', '.join(CATEGORIES)}")


if __name__ == "__main__":
    main()
