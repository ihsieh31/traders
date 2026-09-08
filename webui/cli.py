"""Packaged CLI entry for the Dash Web UI (F18).

The console script ``tradingagents-web`` points here; the root-level
``run_webui_dash.py`` is a thin compatibility wrapper importing this
module. Heavy app imports are deferred until after argument parsing so
``--help`` exits without building the Dash app or touching the network.
"""

import argparse
import socket
import sys


def find_available_port(start_port, end_port=None):
    """Find an available port in the given range"""
    if end_port is None:
        end_port = start_port + 100  # Try up to 100 ports after the start port

    for port in range(start_port, end_port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('localhost', port))
                return port
            except OSError:
                continue

    return None


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="TradingAgents Dash Web UI")

    parser.add_argument(
        "--port",
        type=int,
        default=7860,
        help="Port to run the server on",
    )

    parser.add_argument(
        "--share",
        action="store_true",
        help="Share the app publicly",
    )

    parser.add_argument(
        "--server-name",
        type=str,
        default="127.0.0.1",
        help="Server name to run the app on",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run in debug mode",
    )

    parser.add_argument(
        "--max-threads",
        type=int,
        default=40,
        help="Maximum number of threads",
    )

    return parser.parse_args()


def main():
    """Run the Dash web UI"""
    args = parse_args()

    # Imported after parsing so --help never loads the Dash app.
    from webui.app_dash import run_app

    # Find an available port if the specified one is not available
    port = find_available_port(args.port)
    if port is None:
        print(f"Error: Could not find an available port between {args.port} and {args.port + 100}")
        return 1

    if port != args.port:
        print(f"Port {args.port} is already in use. Using port {port} instead.")

    print(f"Starting TradingAgents Dash Web UI on port {port}...")

    # Run the app
    sys.exit(run_app(
        port=port,
        share=args.share,
        server_name=args.server_name,
        debug=args.debug,
        max_threads=args.max_threads
    ))


if __name__ == "__main__":
    main()
