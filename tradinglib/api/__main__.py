"""``python -m tradinglib.api`` - run the TradingLib API server.

This is the supported way to start the server. It exists because the loop
choice is not cosmetic: uvicorn selects ``uvloop`` whenever it is installed,
uvloop spawns subprocesses by forking the server process, and forking this
process (which holds ``curl_cffi`` handles through yfinance) kills the child
before it can ``exec`` - so every LLM agent stage dies with
``exit code: -11``. :func:`tradinglib.api.app.serve` pins the standard asyncio
loop, which spawns without that fork handler.

Usage::

    python -m tradinglib.api                       # 127.0.0.1:8200
    python -m tradinglib.api --port 8210            # another port
    python -m tradinglib.api --host 0.0.0.0 --reload
"""

from __future__ import annotations

import argparse

from tradinglib.api.app import serve


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and run the server."""
    parser = argparse.ArgumentParser(prog="python -m tradinglib.api", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default localhost)")
    parser.add_argument("--port", type=int, default=8200, help="port to bind (default 8200)")
    parser.add_argument("--reload", action="store_true", help="reload on source changes")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, reload=args.reload, log_level=args.log_level)


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
