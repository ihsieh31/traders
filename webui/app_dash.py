"""
app_dash.py - Simplified Dash-based web UI for TradingAgents

This is the refactored version of app_dash.py that uses organized modules
for better code structure and maintainability.

RECENT FIX: Multiple Symbol Page Refresh Issue
- Fixed issue where only the first symbol would show after page refresh when analyzing multiple symbols
- The app now stores symbols list in browser storage and restores all symbol pages correctly
- Added safeguards to prevent index out of range errors during pagination
- Users can now refresh the page while analyzing multiple symbols without losing access to all symbol pages
"""

import dash
import dash_bootstrap_components as dbc
from flask import Flask
import logging

from webui.config.constants import APP_CONFIG, COLORS
from webui.layout import create_main_layout
from webui.callbacks import register_all_callbacks


def create_app():
    """Create and configure the Dash application"""
    
    # Initialize Flask server
    server = Flask(__name__)

    # Initialize Dash app with Bootstrap
    app = dash.Dash(
        __name__,
        server=server,
        external_stylesheets=[
            dbc.themes.DARKLY,
            *APP_CONFIG["external_stylesheets"]
        ],
        suppress_callback_exceptions=APP_CONFIG["suppress_callback_exceptions"],
        update_title=APP_CONFIG["update_title"],
    )

    # Set app title
    app.title = APP_CONFIG["title"]

    # Set the layout
    app.layout = create_main_layout()

    # Register all callbacks
    register_all_callbacks(app)

    return app


def run_app(port=7860, share=False, server_name="127.0.0.1", debug=False, max_threads=1):
    """Run the TradingAgents Dash Web UI"""
    
    # Create the app
    app = create_app()
    
    if debug:
        print(f"Starting TradingAgents Dash Web UI on port {port}...")
    else:
        print("Starting TradingAgents Web UI...")
    
    # Suppress verbose HTTP request logs from Werkzeug
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # Optionally also silence Dash's callback exceptions logger
    logging.getLogger("dash.callback").setLevel(logging.ERROR)
    
    # Run the app
    app.run(
        port=port,
        host=server_name,
        debug=debug,
        dev_tools_hot_reload=debug,
        use_reloader=False  # Disable reloader to prevent double-start in debug mode
    )
    
    return 0


# The app instance is created lazily: building it eagerly at import time
# fetches Alpaca account data, so any import of this module (directly or via
# the webui package) would trigger network calls.
_app = None


def __getattr__(name):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    run_app(debug=True) 