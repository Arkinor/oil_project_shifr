"""config.py — пути, константы и настройки сервиса.

Все каталоги данных и значения по умолчанию собраны здесь; остальные модули
только читают их.
"""
import json
import logging
import os
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

SETTINGS_FILE = APP_DIR / "settings.json"
USERS_FILE = DATA_DIR / "users.json"
LOG_FILE = DATA_DIR / "activity.log"
SERVER_LOG_FILE = DATA_DIR / "server.log"
# Журнал обезличивания: логин -> имя файла -> найденные пары original/replacement.
# Дополняется по ходу обработки, чтобы было видно, что уходит в модель.
MASK_LOG_FILE = DATA_DIR / "masking_log.json"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "lm_studio_url": "http://127.0.0.1:1234/v1",
    "model": "google/gemma-4-e2b",
    "api_key": "lm-studio",
    "temperature": 0.0,
    "max_tokens": 4096,
    # Минимальное время ожидания ответа. Для длинных фрагментов запас растёт
    # вместе с их размером (см. _chunk_timeout): локальная модель пишет ответ
    # токен за токеном, и 120 с хватает только на короткий текст.
    "request_timeout": 120,
    # LM Studio загружает модель по требованию (JIT): первый запрос может ждать
    # загрузку несколько минут.
    "model_load_timeout": 600,
    "llm_retries": 3,
    "llm_chunk_chars": 3500,
    "llm_enabled": True,
    # Отключает «размышления» thinking-моделей. Без этого модель тратит на них
    # весь max_tokens: запрос идёт минутами и обрывается по таймауту.
    # Если сервер параметр не принимает, клиент сам повторяет запрос без него.
    "reasoning_effort": "none",
    # Удалять ли из документов картинки и штампы. По умолчанию выключено:
    # обезличивается только текстовый слой, а печати, подписи, схемы и сканы
    # остаются как есть. Включение удаляет их целиком ради приватности.
    "strip_images": False,
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


def _missing_settings(data: Dict[str, Any],
                      defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Настройки из defaults, которых нет в data (вложенные объекты — тоже)."""
    missing: Dict[str, Any] = {}
    for key, value in defaults.items():
        if key not in data:
            missing[key] = value
        elif isinstance(value, dict) and isinstance(data.get(key), dict):
            nested = _missing_settings(data[key], value)
            if nested:
                missing[key] = nested
    return missing


def _merge_missing(data: Dict[str, Any], missing: Dict[str, Any]) -> None:
    for key, value in missing.items():
        current = data.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_missing(current, value)
        elif key not in data:
            data[key] = value


def _write_settings(data: Dict[str, Any]) -> None:
    """Пишет файл через временный, чтобы не оставить обрезанный settings.json."""
    tmp = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def ensure_settings() -> None:
    """Создаёт settings.json и дополняет его настройками, появившимися в конфиге.

    Значения, которые уже есть в файле, не меняются: файл принадлежит
    пользователю. Добавляются только отсутствующие ключи из DEFAULT_SETTINGS,
    чтобы новые настройки не жили «невидимо» в коде.
    """
    if not SETTINGS_FILE.exists():
        try:
            _write_settings(DEFAULT_SETTINGS)
            logger.info("Создан settings.json")
        except Exception as e:
            logger.error(f"settings.json: не удалось создать: {e}")
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except Exception as e:
        # Битый файл не перезаписываем: иначе настройки пользователя пропадут.
        logger.error(f"settings.json: {e}")
        return
    if not isinstance(data, dict):
        logger.error("settings.json: ожидался объект JSON, файл не изменён")
        return
    missing = _missing_settings(data, DEFAULT_SETTINGS)
    if not missing:
        return
    _merge_missing(data, missing)
    try:
        _write_settings(data)
    except Exception as e:
        logger.error(f"settings.json: не удалось дополнить: {e}")
        return
    logger.info("settings.json: добавлены настройки "
                + ", ".join(sorted(missing)))


def load_settings() -> Dict[str, Any]:
    ensure_settings()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        return {**DEFAULT_SETTINGS, **data}
    except Exception as e:
        logger.error(f"settings.json: {e}")
        return dict(DEFAULT_SETTINGS)
