#!/usr/bin/env python3
"""Validate the ownership summary embedded in the consolidated documentation."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTATION = ROOT / "docs/DOCUMENTATION.md"
START = "<!-- ownership-summary:start -->"
END = "<!-- ownership-summary:end -->"


def render_ownership():
    return "\n".join([
        START,
        "| Implementation owner | Responsibility |",
        "|---|---|",
        "| `execution` | requests, order planning, dispatch, protection, recovery, exits, intent execution |",
        "| `long_run_support` | state, config, sessions, preflight, round support, symbols, reporting |",
        "| `tradingagents.graph` | analysis graph and call-time compatibility wrappers |",
        END,
    ])


def main():
    text = DOCUMENTATION.read_text()
    if text.count(START) != 1 or text.count(END) != 1:
        raise SystemExit("consolidated documentation is missing ownership markers")
    tail = text.split(START, 1)[1]
    actual = START + tail.split(END, 1)[0] + END
    expected = render_ownership()
    if actual != expected:
        raise SystemExit("consolidated documentation ownership summary is stale")
    print("consolidated documentation ownership summary is current")


if __name__ == "__main__":
    main()
