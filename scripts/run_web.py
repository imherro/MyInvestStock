from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from myinveststock.config import DEFAULT_HOST, DEFAULT_PORT
from myinveststock.db import init_db
from myinveststock.web import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the MyInvestStock local web app.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    init_db()
    run(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
