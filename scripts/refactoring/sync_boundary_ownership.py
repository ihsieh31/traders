#!/usr/bin/env python3
"""Render boundary ownership from the inventory, the sole symbol-owner source."""
from collections import defaultdict
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START = "<!-- inventory-ownership:start -->"
END = "<!-- inventory-ownership:end -->"


def render_ownership(inventory):
    owners = defaultdict(list)
    for section in ("execution_service", "long_run"):
        for record in inventory[section]:
            owners[record["new_owner"]].append(record["name"])
    lines = [START, "| Implementation owner | Baseline symbols |", "|---|---|"]
    for owner, names in sorted(owners.items()):
        lines.append(f"| `{owner}` | " + ", ".join(f"`{name}`" for name in sorted(names)) + " |")
    return "\n".join([*lines, END])


def main():
    inventory = json.loads((ROOT / "docs/refactoring/execution-long-run-inventory.json").read_text())
    path = ROOT / "docs/refactoring/execution-long-run-boundaries.md"
    text = path.read_text()
    before, tail = text.split(START, 1)
    _, after = tail.split(END, 1)
    path.write_text(before + render_ownership(inventory) + after)


if __name__ == "__main__":
    main()
