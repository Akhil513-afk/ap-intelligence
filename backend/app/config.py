"""Runtime configuration. Everything is overridable with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("AP_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
REPORT_DIR = DATA_DIR / "reports"
DB_URL = os.getenv("AP_DATABASE_URL", f"sqlite:///{DATA_DIR / 'ap_intelligence.db'}")
SAMPLE_DIR = ROOT / "sample_data" / "invoices"
HISTORY_DIR = ROOT / "sample_data" / "history"
FRONTEND_DIST = ROOT / "frontend" / "dist"

MAX_UPLOAD_MB = float(os.getenv("AP_MAX_UPLOAD_MB", "10"))
OCR_DPI = int(os.getenv("AP_OCR_DPI", "300"))

# Optional LLM extraction (Anthropic Claude). The app works fully without it.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("AP_LLM_MODEL", "claude-sonnet-4-5")
ANTHROPIC_API_URL = os.getenv("AP_LLM_API_URL", "https://api.anthropic.com/v1/messages")
LLM_TIMEOUT_S = float(os.getenv("AP_LLM_TIMEOUT_S", "45"))

REVIEWERS = ["Priya Nair (AP Lead)", "Rahul Mehta (AP Analyst)", "Ananya Iyer (Finance Controller)"]

for d in (DATA_DIR, UPLOAD_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)
