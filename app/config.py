"""Settings, read from the environment (or a local .env file)."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-opus-5-5")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_DEEP_MODEL = env("GEMINI_DEEP_MODEL", GEMINI_MODEL)  # the need reasoning and the writer
# Tried in order when a model is busy or out of quota.
GEMINI_BACKUPS = [
    m.strip() for m in env("GEMINI_BACKUPS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash").split(",") if m.strip()
]
DB_PATH = env("DATABASE_PATH", str(ROOT / "drafter.db"))
SELLER_FILE = env("SELLER_PROFILE", str(ROOT / "seller" / "zamp.json"))
PEERS_FILE = ROOT / "seller" / "peers.json"

HOOK_THRESHOLD = int(env("HOOK_THRESHOLD", "60"))
HUNTER_MIN_SCORE = int(env("HUNTER_MIN_SCORE", "70"))
MAX_PAGES = 14
MAX_PAGE_CHARS = 12_000
MAX_SUBMITS = 2  # the first try plus one rewrite
MAX_QUESTIONS = 6
MAX_WORDS = 130


def missing_keys() -> list[str]:
    """Keys the app cannot run without. Serper and Hunter degrade gracefully; Claude is optional."""
    has_ai = env("AI_BACKEND") == "claude-code" or env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
    missing = [] if has_ai else ["GEMINI_API_KEY"]
    return missing + ([] if env("TAVILY_API_KEY") else ["TAVILY_API_KEY"])


def load_seller() -> dict:
    return json.loads(Path(SELLER_FILE).read_text(encoding="utf-8"))


def load_peers() -> list[dict]:
    return json.loads(PEERS_FILE.read_text(encoding="utf-8"))
