#!/usr/bin/env python3
"""
Dummy HTTP server for testing haniel process management.

Usage:
    python dummy_server.py [options]

Options:
    --port PORT       Port to listen on (default: 8080)
    --delay SECONDS   Delay before becoming ready (default: 0)
    --ready-message   Message to print when ready (default: "Server ready")
    --shutdown-endpoint  Enable /shutdown endpoint

This server is designed to test various ready conditions:
- port:{port} - Server listens on the specified port
- delay:{seconds} - Server waits before printing ready message
- log:{pattern} - Server prints a message matching the pattern
- http:{url} - Server responds to HTTP requests
"""

import argparse
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread


class DummyHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dummy server."""

    shutdown_requested = False

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Ready")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Hello from dummy server")

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == "/shutdown":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Shutting down")
            DummyHandler.shutdown_requested = True
            # Schedule shutdown in a separate thread
            Thread(target=self._delayed_shutdown, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

    def _delayed_shutdown(self) -> None:
        """Shutdown after a brief delay to allow response to be sent."""
        time.sleep(0.1)
        self.server.shutdown()

    def log_message(self, format: str, *args) -> None:
        """Override to print to stdout instead of stderr."""
        print(f"[HTTP] {format % args}", flush=True)


def run_server(
    port: int = 8080,
    delay: float = 0,
    ready_message: str = "Server ready",
    shutdown_endpoint: bool = False,
) -> None:
    """Run the dummy HTTP server.

    Args:
        port: Port to listen on
        delay: Delay before becoming ready
        ready_message: Message to print when ready
        shutdown_endpoint: Whether to enable the /shutdown endpoint
    """
    print(f"Starting dummy server on port {port}...", flush=True)

    # Set up signal handlers
    def signal_handler(signum, frame):
        print(f"Received signal {signum}, shutting down...", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Apply startup delay
    if delay > 0:
        print(f"Waiting {delay} seconds before starting...", flush=True)
        time.sleep(delay)

    # Create and start server
    server = HTTPServer(("", port), DummyHandler)

    # Print ready message (for log: condition testing)
    print(ready_message, flush=True)

    print(f"Listening on port {port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Server stopped", flush=True)


def main() -> None:
    """Parse arguments and run the server."""
    parser = argparse.ArgumentParser(description="Dummy HTTP server for testing")
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Delay before becoming ready (default: 0)",
    )
    parser.add_argument(
        "--ready-message",
        type=str,
        default="Server ready",
        help="Message to print when ready (default: 'Server ready')",
    )
    parser.add_argument(
        "--shutdown-endpoint",
        action="store_true",
        help="Enable /shutdown endpoint",
    )

    args = parser.parse_args()

    run_server(
        port=args.port,
        delay=args.delay,
        ready_message=args.ready_message,
        shutdown_endpoint=args.shutdown_endpoint,
    )


if __name__ == "__main__":
    main()
