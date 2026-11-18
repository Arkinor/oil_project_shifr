"""config.py — пути, константы и настройки сервиса.

Все каталоги данных и значения по умолчанию собраны здесь; остальные модули
только читают их.
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict


# ================================================================
# 1. ПУТИ, НАСТРОЙКИ, ЛОГИ
# ================================================================
def _app_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

APP_DIR = _app_dir()
BASE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
STATE_DIR = DATA_DIR / "state"
for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = (".png", ".jpg", ".jpeg")

SETTINGS_FILE = APP_DIR / "settings.json"
USERS_FILE = DATA_DIR / "users.json"
LOG_FILE = DATA_DIR / "activity.log"
SERVER_LOG_FILE = DATA_DIR / "server.log"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "lm_studio_url": "http://127.0.0.1:1234/v1",
    "model": "google/gemma-4-e2b",
    "api_key": "lm-studio",
    "temperature": 0.0,
    "max_tokens": 4096,
    "request_timeout": 120,
    # LM Studio загружает модель по требованию (JIT): первый запрос может ждать
    # загрузку несколько минут.
    "model_load_timeout": 600,
    "llm_retries": 3,
    "llm_chunk_chars": 3500,
    "llm_enabled": True,
    "ocr_lang": "rus+eng",
    "max_upload_mb": 50,
    "host": "0.0.0.0",
    "port": 9321,
    "open_browser": True,
    "tray_enabled": True,
    "tray_minimize_on_close": True,
}

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format=_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S", encoding="utf-8",
)
logger = logging.getLogger("activity")


def ensure_settings() -> None:
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Создан settings.json")


def load_settings() -> Dict[str, Any]:
    ensure_settings()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**DEFAULT_SETTINGS, **data}
    except Exception as e:
        logger.error(f"settings.json: {e}")
        return dict(DEFAULT_SETTINGS)
