#!/usr/bin/env python3
"""Entry point for the R3TR generator.

Default - start the NiceGUI web interface::

    python run.py                # http://localhost:8080
    python run.py --port 9000 --host 0.0.0.0

Classic CLI mode::

    python run.py --cli -f data.csv -o out -c 100 --force
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SAP R3TR generator - NiceGUI web UI (default) or classic CLI (-cli).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for the web UI (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8080, help="Port for the web UI (default: %(default)s)")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run the classic command-line mode instead of the web UI "
        "(remaining flags are passed to the CLI: -f -o -c -d --force "
        "--export-components --import-components --exclude-tables).",
    )
    return parser


def main() -> int:
    argv = sys.argv[1:]
    # Split off our own flags so the CLI front-end can keep its own set.
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)

    if args.cli:
        from r3tr.cli import main as cli_main

        return cli_main(rest)

    from r3tr.app import run

    print(f"Starting R3TR generator UI -> http://{args.host}:{args.port}")
    run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
