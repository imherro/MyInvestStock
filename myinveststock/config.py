from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOCAL_DATA_DIR = DATA_DIR / "local"
RAW_DATA_DIR = DATA_DIR / "raw"
DB_PATH = LOCAL_DATA_DIR / "myinveststock.sqlite"

LEADER_INDEX_URL = "https://leader.okbbc.com/api/index"
THEME_INDEX_URL = "https://theme.okbbc.com/api/index"
FOOTER_SCRIPT_URL = "https://invest.okbbc.com/footer.js"
HEADER_SCRIPT_URL = "https://invest.okbbc.com/header.js"
STATIC_ASSET_VERSION = "20260626-bulk-research-entry"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8016
