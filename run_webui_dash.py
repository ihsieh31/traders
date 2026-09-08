#!/usr/bin/env python
"""
run_webui_dash.py - Run the Dash-based web UI for TradingAgents

Compatibility wrapper: the packaged entry point lives in ``webui.cli``
(F18) so the installed ``tradingagents-web`` command and this root script
share one startup implementation.
"""

from webui.cli import main


if __name__ == "__main__":
    main()
