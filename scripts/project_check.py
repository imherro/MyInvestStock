from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def check(condition: bool, message: str) -> bool:
    status = "OK" if condition else "FAIL"
    print(f"{status} {message}")
    return condition


def main() -> int:
    ok = True
    ok &= check((ROOT / ".env.example").exists(), ".env.example exists")
    ok &= check((ROOT / ".gitignore").exists(), ".gitignore exists")
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", ".env"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        ok &= check(ignored.returncode == 0, ".env is ignored by git")
    except FileNotFoundError:
        ok &= check(False, "git is available")

    for module in ["myinveststock.db", "myinveststock.leader_index", "myinveststock.theme_index", "myinveststock.web"]:
        try:
            importlib.import_module(module)
            ok &= check(True, f"import {module}")
        except Exception as exc:  # pragma: no cover - diagnostic script
            print(exc)
            ok &= check(False, f"import {module}")

    config_source = (ROOT / "myinveststock" / "config.py").read_text(encoding="utf-8")
    leader_source = (ROOT / "myinveststock" / "leader_index.py").read_text(encoding="utf-8")
    ok &= check("https://invest.okbbc.com/footer.js" in config_source, "unified footer script is wired")
    ok &= check('LEADER_INDEX_URL = "https://leader.okbbc.com/api/index"' in config_source, "upstream source is /api/index")
    ok &= check('THEME_INDEX_URL = "https://theme.okbbc.com/api/index"' in config_source, "theme context source is /api/index")
    ok &= check("themes[].stock_leaders" not in leader_source, "ingest does not expand from stock_leaders")
    docs = (ROOT / "docs" / "DATA_SOURCES.md").read_text(encoding="utf-8")
    ok &= check("key_results.primary_output.items" in docs, "primary result path is documented")
    ok &= check("mainline_ranking" in docs, "theme mainline context is documented")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
