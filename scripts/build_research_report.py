from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.report import build_stock_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one deterministic StockResearchReport from structured inputs.")
    parser.add_argument("input_file", type=Path)
    args = parser.parse_args()

    input_data: Any = json.loads(args.input_file.read_text(encoding="utf-8"))
    report = build_stock_report(input_data)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
