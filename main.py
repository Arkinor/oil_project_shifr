"""
main.py — веб-сервис обезличивания PDF/Excel с локальной LLM (LM Studio).

Запуск:  python main.py
Сборка:  build.bat  ->  dist/PIIMasker.exe

Для сборки с треем добавьте в PyInstaller:
    --hidden-import=pystray --hidden-import=PIL --hidden-import=PIL.Image
и положите иконку рядом с main.py (icon.ico). Если pystray/Pillow не
установлены — сервис работает как обычно, без трея.

Контракт с фронтендом (не менять!):
  GET  /                     -> index.html
  POST /api/login            {login,password} -> {ok,login} | 401 "Неверный логин или пароль"
  GET  /api/me               (X-Login,X-Password) -> {login}
  GET  /api/health           -> {ok,model,lm_studio_url,llm_ok,rag,frozen,tray,users_count}
  POST /api/process          multipart file     -> {ok,filename,download_name,download_url,
                                                    elapsed,preview_type,preview,llm_used,extras,
                                                    masked,pages}
  GET  /api/download/{name}  (X-Login,X-Password) -> файл
"""
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import date as date_cls, datetime as datetime_cls, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from faker import Faker

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- пути / настройки
def _app_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

APP_DIR = _app_dir()
BASE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, KNOWLEDGE_DIR):
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = APP_DIR / "settings.json"
USERS_FILE = DATA_DIR / "users.json"
LOG_FILE = DATA_DIR / "activity.log"
SERVER_LOG_FILE = DATA_DIR / "server.log"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "lm_studio_url": "http://127.0.0.1:1234/v1",
    "model": "google/gemma-4-e2b",
    "api_key": "lm-studio",
    "temperature": 0.0,
    "max_tokens": 1024,
    "request_timeout": 90,
    "max_llm_chars": 12000,
    "max_upload_mb": 50,
    "host": "0.0.0.0",
    "port": 9321,
    "open_browser": True,
    "tray_enabled": True,
    "tray_minimize_on_close": True,
}

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S", encoding="utf-8",
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


# ---------------------------------------------------------------- пользователи
def ensure_users_file() -> None:
    if not USERS_FILE.exists():
        USERS_FILE.write_text(json.dumps({"users": [{"login": "admin", "password": "admin"}]},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Создан users.json (admin/admin)")


def load_users() -> List[Dict[str, str]]:
    ensure_users_file()
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8")).get("users", [])
    except Exception as e:
        logger.error(f"users.json: {e}")
        return []


def authenticate(login: str, password: str) -> bool:
    login = login or ""
    password = password or ""
    for u in load_users():
        if hmac.compare_digest(str(u.get("login", "")), login) and \
           hmac.compare_digest(str(u.get("password", "")), password):
            return True
    return False


def get_current_user(request: Request) -> str:
    login = request.headers.get("X-Login", "")
    password = request.headers.get("X-Password", "")
    if not login or not password or not authenticate(login, password):
        raise HTTPException(401, "Требуется авторизация")
    return login


# ---------------------------------------------------------------- PII-правила и фейки
MASK = "***"

CYR_UPPER = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
CYR_LOWER = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
LAT_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LAT_LOWER = "abcdefghijklmnopqrstuvwxyz"
ALL_LETTERS = CYR_LOWER + CYR_UPPER + LAT_LOWER + LAT_UPPER

ORG_PREFIXES = {"ООО", "ОАО", "ЗАО", "ПАО", "АО", "ИП", "ФГУП", "ГУП", "МУП",
                "ГБУ", "ГАУ", "АНО", "НКО", "ФОНД", "МИНИСТЕРСТВО", "ДЕПАРТАМЕНТ",
                "УПРАВЛЕНИЕ", "КОМПАНИЯ"}
ORG_NAMES = ["Вектор", "Гранит", "Сфера", "Прогресс", "Альфа", "Лазурит", "Меркурий",
             "Орион", "Темп", "Стандарт", "Ресурс", "Гарант", "Партнёр", "Рубин",
             "Кристалл", "Импульс", "Атлант", "Кедр", "Волна", "Заря"]
ORG_SUFFIXES = ["Групп", "Сервис", "Трейд", "Плюс", "Технологии", "Ресурс", "Снаб"]

# (тип, regex). Совпадение целиком заменяется похожим ненастоящим значением.
PLAIN_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")),
    ("phone", re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")),
    ("passport", re.compile(r"(?<!\d)\d{2}\s?\d{2}(?:\s*(?:№|номер)\s*|\s+)\d{6}(?!\d)", re.I)),
    ("snils", re.compile(r"(?<!\d)\d{3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{2}(?!\d)")),
    ("kpp", re.compile(r"(?<!\d)\d{4}[A-ZА-Я]{2}\d{3}(?!\d)", re.I)),
    # --- Реквизиты РФ: отдельные регексы с высоким PRIORITY ---
    # ИНН: 10 (юрлицо) или 12 (физлицо/ИП) цифр. Ловим и слитную форму "ИНН7707083893".
    ("inn", re.compile(r"(?<!\d)(?:ИНН\s*[:\-]?\s*)?(?:\d{10}|\d{12})(?!\d)", re.I)),
    # ОГРН 13, ОГРНИП 15
    ("ogrn", re.compile(r"(?<!\d)(?:ОГРНИП|ОГРН)\s*[:\-]?\s*(?:\d{13}|\d{15})(?!\d)", re.I)),
    ("ogrn_bare", re.compile(r"(?<!\d)(?:\d{13}|\d{15})(?!\d)")),
    # БИК 9 цифр
    ("bik", re.compile(r"(?<!\d)(?:БИК\s*[:\-]?\s*)?\d{9}(?!\d)", re.I)),
    # Расчётный/корреспондентский счёт 20 цифр
    ("rs", re.compile(r"(?<!\d)(?:(?:р\/с|к\/с|расч[её]тный|корр?\.?\s*сч[её]т|сч[её]т)\s*[:\-]?\s*)?\d{20}(?!\d)", re.I)),
    # --- ФИО / организации / адреса / даты / числа ---
    ("fio", re.compile(r"(?<![А-Яа-яЁё])(?:[А-ЯЁ][а-яё]{1,20}\s+){2}[А-ЯЁ][а-яё]{1,20}(?![А-Яа-яЁё])")),
    ("fio_initials", re.compile(r"(?<![А-Яа-яЁё])[А-ЯЁ][а-яё]{1,20}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.(?![А-Яа-яЁё])")),
    ("fio_lat", re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]{1,20}\s+){2}[A-Z][a-z]{1,20}(?![A-Za-z])")),
    ("org", re.compile(
        r"(?<![А-Яа-яЁё])(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО|Фонд|"
        r"Министерство|Департамент|Управление|Компания)\s+[«\"]?[А-ЯЁA-Z0-9][^\n,;]{1,100}", re.I)),
    ("address", re.compile(
        r"(?<![А-Яа-яЁё])(?:г\.|город|ул\.|улица|пр-т|проспект|пер\.|переулок|наб\.|набережная|"
        r"ш\.|шоссе|д\.|дом|кв\.|квартира|обл\.|область|респ\.|республика|п\.|пос\.|поселок|"
        r"с\.|село|мкр\.|микрорайон)\s*[^\n,;]{1,80}(?:,\s*[^\n;]{1,80}){0,4}", re.I)),
    ("address_lat", re.compile(
        r"(?<![A-Za-z])(?:st\.|street|ave\.|avenue|rd\.|road|blvd\.|boulevard|"
        r"apt\.|apartment|suite|bldg\.|building|city|town)\s+[^\n,;]{1,80}(?:,\s*[^\n;]{1,80}){0,3}", re.I)),
    ("date", re.compile(r"(?<!\d)(?:\d{2}[./-]\d{2}[./-]\d{4}|\d{4}[./-]\d{2}[./-]\d{2})(?!\d)")),
    ("number", re.compile(r"(?<![\d.,])-?(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?(?![\d])")),
]

# (тип, regex, номер группы). Меняется только значение, метка сохраняется.
GROUP_PATTERNS: List[Tuple[str, re.Pattern, int]] = [
    ("kpp", re.compile(r"(?:КПП)\s*[:\-]?\s*(\d{4}[0-9A-ZА-Я]{2}\d{3})", re.I), 1),
    ("inn", re.compile(r"(?:ИНН)\s*[:\-]?\s*(\d{10,12})", re.I), 1),
    ("ogrn", re.compile(r"(?:ОГРНИП|ОГРН)\s*[:\-]?\s*(\d{13,15})", re.I), 1),
    ("bik", re.compile(r"(?:БИК)\s*[:\-]?\s*(\d{9})", re.I), 1),
    ("rs", re.compile(r"(?:р\/с|к\/с|расч[её]тный\s*сч[её]т|корр?\.?\s*сч[её]т)\s*[:\-]?\s*(\d{20})", re.I), 1),
    ("fio", re.compile(
        r"(?:ФИО|Ф\.И\.О\.|Сотрудник|Работник|Владелец|Директор|Главный бухгалтер|"
        r"Заемщик|Заёмщик|Клиент|Получатель|Представитель|Подписант|Страхователь|"
        r"Пациент|Ученик|Студент|Арендатор|Арендодатель|Продавец|Покупатель|"
        r"Исполнитель|Заказчик|Ответственный)\s*[:\-]?\s*([А-ЯЁ][А-ЯЁа-яё. \t-]{2,80})", re.I), 1),
    ("fio_lat", re.compile(
        r"(?:Name|Full\s*name|Director|Owner|Employee|Customer|Borrower|Client|"
        r"Representative|Signatory)\s*[:\-]?\s*([A-Z][A-Za-z.\t -]{2,80})", re.I), 1),
    ("org", re.compile(
        r"(?:Организация|Наименование|Работодатель|Компания|Работодатель|"
        r"Страхователь|Продавец|Покупатель|Исполнитель|Заказчик)\s*[:\-]?\s*([^\n;,]{2,150})", re.I), 1),
    ("address", re.compile(
        r"(?:Адрес|Место жительства|Место нахождения|Почтовый адрес|Место регистрации|"
        r"Address)\s*[:\-]?\s*([^\n;]{5,200})", re.I), 1),
]

PRIORITY = {
    "email": 10, "phone": 10,
    "passport": 9, "snils": 9, "kpp": 9, "inn": 9, "ogrn": 9, "ogrn_bare": 9, "bik": 9, "rs": 9,
    "date": 8,
    "fio": 7, "fio_initials": 7, "fio_lat": 7,
    "org": 7, "address": 7, "address_lat": 7,
    "number": 1, "extra": 5,
}


def _clean_value(v: str) -> str:
    v = str(v).replace("\u00ad", "-").replace("\xa0", " ").replace("\u2011", "-")
    return v.strip(" \t\r\n,;.\"'«»")


def _mix_letters(text: str, rate: float = 0.08) -> str:
    """Подмешивает случайные буквы того же алфавита и регистра."""
    if not text or len(text) <= 2:
        return text
    cyr = bool(re.search(r"[А-Яа-яЁё]", text))
    if cyr:
        pool = CYR_UPPER if text.isupper() else CYR_LOWER
    else:
        pool = LAT_UPPER if text.isupper() else LAT_LOWER
    out = []
    for ch in text:
        out.append(ch)
        if ch.isalpha() and random.random() < rate:
            out.append(random.choice(pool))
    return "".join(out)


def _rand_digits(n: int, leading_zero: bool = False) -> str:
    if n <= 0:
        return ""
    first = "0" if leading_zero else random.choice("123456789")
    return first + "".join(random.choice("0123456789") for _ in range(n - 1))


def _inn_checksum(digits: str) -> str:
    """Возвращает контрольную цифру(ы) для ИНН 10/12 знаков."""
    if len(digits) == 9:  # для 10-значного ИНН — 1 контрольная
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        s = sum(int(d) * w for d, w in zip(digits, weights))
        return str(s % 11 % 10)
    if len(digits) == 11:  # для 12-значного — 2 контрольные
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(int(d) * w for d, w in zip(digits, w1)) % 11 % 10
        c2 = sum(int(d) * w for d, w in zip(digits, w2)) % 11 % 10
        return f"{c1}{c2}"
    return ""


def _ogrn_checksum(digits: str) -> str:
    """Возвращает контрольную цифру для ОГРН (13) / ОГРНИП (15)."""
    n = int(digits)
    if len(digits) == 12:
        return str(n % 11 % 10)
    if len(digits) == 14:
        return str(n % 13 % 10)
    return ""


def _snils_checksum(digits9: str) -> str:
    total = sum(int(d) * (9 - i) for i, d in enumerate(digits9))
    if total < 100:
        check = total
    elif total in (100, 101):
        check = 0
    else:
        check = total % 101
        if check == 100:
            check = 0
    return f"{check:02d}"


def _infer_kind(v: str) -> str:
    s = v.strip()
    if "@" in s:
        return "email"
    if re.search(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}", s):
        return "phone"
    if re.search(r"\d{2}\s?\d{2}(?:\s*(?:№|номер)\s*|\s+)\d{6}", s, re.I):
        return "passport"
    if re.search(r"\d{3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{2}", s):
        return "snils"
    if re.fullmatch(r"\d{4}[A-ZА-Я]{2}\d{3}", s, re.I):
        return "kpp"
    if re.fullmatch(r"\d{10}", s) or re.fullmatch(r"\d{12}", s):
        return "inn"
    if re.fullmatch(r"\d{13}", s) or re.fullmatch(r"\d{15}", s):
        return "ogrn"
    if re.fullmatch(r"\d{9}", s):
        return "bik"
    if re.fullmatch(r"\d{20}", s):
        return "rs"
    if re.search(r"(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО|Фонд|"
                 r"Министерство|Департамент|Управление|Компания)", s, re.I):
        return "org"
    if re.search(r"(?:г\.|ул\.|д\.|кв\.|обл\.|пр-т|проспект|наб\.|ш\.|мкр\.|пос\.|с\.|респ\.)", s, re.I):
        return "address"
    if re.search(r"(?:st\.|street|ave\.|avenue|rd\.|road|blvd\.|boulevard|apt\.|suite|bldg\.)", s, re.I):
        return "address_lat"
    words = s.split()
    if 2 <= len(words) <= 4 and all(w[:1].isupper() for w in words if w):
        # латиница?
        if re.search(r"[A-Za-z]", s) and not re.search(r"[А-Яа-яЁё]", s):
            return "fio_lat"
        return "fio"
    if re.fullmatch(r"[\d\s.,-]+", s):
        return "number"
    return "extra"


class Masker:
    """Генерирует и запоминает фейковые значения в рамках одного документа."""

    def __init__(self):
        self.fake = Faker("ru_RU")
        self.cache: Dict[Tuple[str, str], str] = {}

    def get(self, kind: str, original: str) -> str:
        key = (kind, original)
        if key not in self.cache:
            self.cache[key] = self._gen(kind, original)
        return self.cache[key]

    def _gen(self, kind: str, original: str) -> str:
        if kind == "extra":
            kind = _infer_kind(original)
        if kind in ("fio", "fio_lat"):
            if kind == "fio_lat":
                return self._fio_lat(original)
            name = ""
            for _ in range(5):
                name = re.sub(r"^(?:г-н|г-жа|господин|госпожа)\s+", "", self.fake.name(), flags=re.I)
                v = _mix_letters(name, 0.08)
                if v != original:
                    return v
            return _mix_letters(name, 0.08) if name else "Иванов Иван Иванович"
        if kind == "fio_initials":
            return (f"{_mix_letters(self.fake.last_name(), 0.1)} "
                    f"{self.fake.first_name()[0]}.{self.fake.middle_name()[0]}.")
        if kind == "phone":
            compact = not any(c in original for c in " ()-")
            prefix = "8" if original.strip().startswith("8") else "+7"
            d = _rand_digits(10)
            return prefix + d if compact else f"{prefix} ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
        if kind == "email":
            return self.fake.email()
        if kind == "org":
            m = re.match(r"^([А-ЯA-Z]+)\s+", original)
            prefix = m.group(1) if m and m.group(1).upper() in ORG_PREFIXES else ""
            if prefix.upper() == "ИП":
                return "ИП " + _mix_letters(self.fake.name(), 0.05)
            name = f"{random.choice(ORG_NAMES)} {random.choice(ORG_SUFFIXES)}"
            return f"{prefix} «{name}»" if prefix else f"«{name}»"
        if kind in ("address", "address_lat"):
            if kind == "address_lat":
                return (f"{self.fake.street_name()} St., Bldg. {self.fake.building_number()}, "
                        f"{self.fake.city()}")
            return (f"г. {self.fake.city_name()}, {self.fake.street_name()}, "
                    f"д. {self.fake.building_number()}")
        if kind == "passport":
            m = re.match(r"^(\d{2})(\s*)(\d{2})(.*?)(\d{6})$", original, re.S)
            if m:
                series = _rand_digits(4)
                return series[:2] + m.group(2) + series[2:] + m.group(4) + _rand_digits(6)
            return _rand_digits(4) + " " + _rand_digits(6)
        if kind == "snils":
            digits9 = "".join(random.choice("0123456789") for _ in range(9))
            num = digits9 + _snils_checksum(digits9)
            if "-" in original:
                return f"{num[:3]}-{num[3:6]}-{num[6:9]} {num[9:]}"
            if " " in original:
                return f"{num[:3]} {num[3:6]} {num[6:9]} {num[9:]}"
            return num
        if kind == "kpp":
            letters = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ"
            return _rand_digits(4, True) + "".join(random.choice(letters) for _ in range(2)) + _rand_digits(3)
        if kind == "inn":
            n = len(re.sub(r"\D", "", original))
            if n == 10:
                base = _rand_digits(9)
                return base + _inn_checksum(base)
            if n == 12:
                base = _rand_digits(11)
                return base + _inn_checksum(base)
            return _rand_digits(n or 10)
        if kind in ("ogrn", "ogrn_bare"):
            n = len(re.sub(r"\D", "", original))
            if n == 13:
                base = _rand_digits(12, leading_zero=False)
                return base + _ogrn_checksum(base)
            if n == 15:
                base = _rand_digits(14, leading_zero=False)
                return base + _ogrn_checksum(base)
            return _rand_digits(n or 13)
        if kind == "bik":
            return _rand_digits(9, leading_zero=False)
        if kind == "rs":
            return _rand_digits(20, leading_zero=False)
        if kind == "date":
            m = re.match(r"^(\d{2})([./-])(\d{2})\2(\d{4})$", original)
            if m:
                d = date_cls(1995, 1, 1) + timedelta(days=random.randint(0, 12000))
                return d.strftime(f"%d{m.group(2)}%m{m.group(2)}%Y")
            m = re.match(r"^(\d{4})([./-])(\d{2})\2(\d{2})$", original)
            if m:
                d = date_cls(1995, 1, 1) + timedelta(days=random.randint(0, 12000))
                return d.strftime(f"%Y{m.group(2)}%m{m.group(2)}%d")
            return self._number(original)
        if kind == "number":
            return self._number(original)
        return _mix_letters(" ".join(self.fake.word().capitalize()
                                     for _ in range(max(1, len(original.split())))), 0.05)

    def _fio_lat(self, original: str) -> str:
        first = self.fake.first_name_male() if random.random() < 0.5 else self.fake.first_name_female()
        last = self.fake.last_name_male() if random.random() < 0.5 else self.fake.last_name_female()
        # транслит через простую замену кириллицы (без внешних пакетов)
        tr = {
            "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
            "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
            "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
            "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        }
        def translit(s: str) -> str:
            out = []
            for ch in s:
                low = ch.lower()
                r = tr.get(low, ch)
                out.append(r.capitalize() if ch.isupper() else r)
            return "".join(out)
        f, l = translit(first), translit(last)
        if original.split() and original.split()[0][:1].isupper():
            return f"{l} {f}"
        return f"{l} {f}"

    @staticmethod
    def _number_once(original: str) -> str:
        s = original.strip()
        sign = ""
        if s[:1] in ("-", "−"):
            sign, s = "-", s[1:]
        dec = None
        if "," in s and "." in s:
            dec = "," if s.rfind(",") > s.rfind(".") else "."
        elif "," in s:
            dec = ","
        elif "." in s and s.count(".") == 1 and 1 <= len(s.rsplit(".", 1)[1]) <= 2:
            dec = "."
        thou = None
        if " " in s:
            thou = " "
        elif "\u00a0" in s:
            thou = "\u00a0"
        elif dec == "," and "." in s:
            thou = "."
        elif dec == "." and "," in s:
            thou = ","
        elif dec is None and "." in s:
            thou = "."
        clean = s.replace(thou, "") if thou else s
        if dec and dec in clean:
            int_part, frac_part = clean.split(dec, 1)
        else:
            int_part, frac_part = clean, ""
        int_digits = re.sub(r"\D", "", int_part) or "0"
        frac_digits = re.sub(r"\D", "", frac_part)
        new_int = _rand_digits(len(int_digits), leading_zero=int_digits.startswith("0"))
        new_frac = "".join(random.choice("0123456789") for _ in range(len(frac_digits)))
        if thou:
            chunks, x = [], new_int
            while x:
                chunks.insert(0, x[-3:])
                x = x[:-3]
            new_int = thou.join(chunks)
        return sign + new_int + ((dec + new_frac) if dec and frac_digits else "")

    @staticmethod
    def _number(original: str) -> str:
        for _ in range(10):
            result = Masker._number_once(original)
            if result != original:
                return result
        return Masker._number_once(original)


def _add_candidate(cands: List[Tuple[int, int, str, str]], start: int, end: int,
                   kind: str, raw: str) -> None:
    left = len(raw) - len(raw.lstrip())
    right = len(raw) - len(raw.rstrip())
    start += left
    end -= right
    if end - start >= 2:
        cands.append((start, end, kind, raw.strip()))


def iter_matches(text: str, extras: Any = ()) -> List[Tuple[int, int, str, str]]:
    """Возвращает непересекающиеся совпадения (start, end, тип, значение)."""
    if not isinstance(text, str) or not text:
        return []
    # 1:1-нормализация, чтобы позиции совпадали с исходной строкой
    mt = text.replace("\u00ad", "-").replace("\xa0", " ").replace("\u2011", "-")
    cands: List[Tuple[int, int, str, str]] = []
    for kind, rx, group in GROUP_PATTERNS:
        for m in rx.finditer(mt):
            _add_candidate(cands, m.start(group), m.end(group), kind, m.group(group))
    for kind, rx in PLAIN_PATTERNS:
        for m in rx.finditer(mt):
            _add_candidate(cands, m.start(), m.end(), kind, m.group(0))
    for x in extras:
        v = _clean_value(x)
        if len(v) >= 3 and v != MASK:
            kind = _infer_kind(v)
            for m in re.finditer(re.escape(v), mt, re.I):
                _add_candidate(cands, m.start(), m.end(), kind, m.group(0))
    best: Dict[Tuple[int, int], Tuple[int, int, str, str]] = {}
    for c in cands:
        key = (c[0], c[1])
        prev = best.get(key)
        if prev is None or PRIORITY.get(c[2], 0) > PRIORITY.get(prev[2], 0):
            best[key] = c
    selected: List[Tuple[int, int, str, str]] = []
    for c in sorted(best.values(), key=lambda x: (-(x[1] - x[0]), -PRIORITY.get(x[2], 0), x[0])):
        if all(c[1] <= s or c[0] >= e for s, e, _, _ in selected):
            selected.append(c)
    return sorted(selected, key=lambda x: x[0])


def mask_text(text: Any, extras: Any = (), masker: Masker | None = None) -> Any:
    if not isinstance(text, str) or not text:
        return text
    matches = iter_matches(text, extras)
    if not matches:
        return text
    masker = masker or Masker()
    out, last = [], 0
    for start, end, kind, value in matches:
        out.append(text[last:start])
        out.append(masker.get(kind, value))
        last = end
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------- RAG на LangChain
KNOWLEDGE: List[str] = [
    "ИНН — 10 цифр у организации, 12 цифр у ИП и физлица. Искать отдельные числа без букв, "
    "в том числе рядом с подписью «ИНН».",
    "КПП — 9 символов: 4 цифры, 2 буквы (цифры или заглавные буквы), 3 цифры. Пример: 770701001.",
    "ОГРН — 13 цифр у организации, ОГРНИП — 15 цифр. Маскировать всё число целиком.",
    "СНИЛС — 11 цифр в формате 123-456-789 00 или 123 456 789 00.",
    "Паспорт — серия 4 цифры и номер 6 цифр, обычно через пробел: 45 09 123456, 4509 123456, "
    "серия 4509 номер 123456.",
    "Телефон — +7 или 8, затем 10 цифр, допускаются пробелы, дефисы и скобки: +7 (999) 123-45-67.",
    "Email — адрес вида name@example.ru. Маскировать целиком.",
    "Банковские реквизиты — расчётный счёт из 20 цифр, БИК из 9 цифр, корреспондентский счёт из 20 цифр.",
    "ФИО — три слова с заглавной буквы (Фамилия Имя Отчество) или Фамилия И.О. "
    "Также искать значение после меток «ФИО», «Сотрудник», «Владелец», «Директор», «Заемщик», "
    "«Клиент», «Получатель», «Представитель», «Подписант».",
    "Организации — названия после ООО, АО, ПАО, ЗАО, ИП, ГУП, МУП, ГБУ, АНО, НКО, Фонд, "
    "а также после меток «Организация», «Наименование», «Работодатель».",
    "Адреса — строки после «Адрес», «Место жительства», «Место нахождения», а также конструкции "
    "с «г.», «ул.», «д.», «кв.», «обл.», «пр-т», «ш.», «мкр.». Маскировать адрес целиком.",
    "Латиница — ФИО, адреса и организации латиницей искать аналогично кириллическим: "
    "три слова с заглавной, маркеры Street/Ave/St/Bldg/Apt, префиксы Inc/Ltd/LLC/GmbH.",
    "Все найденные значения заменяются похожими ненастоящими значениями: ФИО — фейковое ФИО, "
    "телефон — фейковый телефон, адрес — фейковый адрес, числа — другие числа той же длины и формата. "
    "Структура документа сохраняется: таблицы, страницы и расположение текста не меняются.",
]


class HashEmbeddings:
    """Лёгкие локальные эмбеддинги без внешней модели (RAG работает офлайн)."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[А-Яа-яЁёA-Za-z0-9]+", (text or "").lower())
        for t in tokens:
            feats = [t] + [t[i:i + 3] for i in range(max(0, len(t) - 2))]
            for f in feats:
                h = int(hashlib.md5(f.encode("utf-8")).hexdigest(), 16)
                vec[h % self.dim] += 1.0
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)


RETRIEVER = None


def build_rag() -> None:
    global RETRIEVER
    try:
        from langchain_core.documents import Document
        from langchain_core.vectorstores import InMemoryVectorStore

        docs = list(KNOWLEDGE)
        for p in KNOWLEDGE_DIR.glob("*.txt"):
            try:
                docs.extend([c.strip() for c in p.read_text(encoding="utf-8").split("\n\n") if c.strip()])
            except Exception as e:
                logger.warning(f"knowledge {p.name}: {e}")
        store = InMemoryVectorStore(HashEmbeddings())
        store.add_documents([Document(page_content=d, metadata={"source": "pii_rules"}) for d in docs])
        RETRIEVER = store.as_retriever(search_kwargs={"k": 3})
        logger.info(f"RAG готов: {len(docs)} фрагментов")
    except Exception as e:
        RETRIEVER = None
        logger.error(f"RAG: {e}")


def rag_search(query: str) -> str:
    if RETRIEVER is None:
        return "\n\n".join(KNOWLEDGE[:3])
    try:
        docs = RETRIEVER.invoke(query)
        return "\n\n".join(d.page_content for d in docs)
    except Exception as e:
        logger.warning(f"RAG search: {e}")
        return ""


# ---------------------------------------------------------------- LLM / инструменты
def get_llm(settings: Dict[str, Any] | None = None):
    s = settings or load_settings()
    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            base_url=s["lm_studio_url"], api_key=s.get("api_key", "lm-studio"),
            model=s["model"], temperature=float(s.get("temperature", 0)),
            timeout=float(s.get("request_timeout", 90)), max_retries=0,
            max_tokens=int(s.get("max_tokens", 1024)),
        )
    except Exception as e:
        logger.error(f"LLM init: {e}")
        return None


def check_llm(settings: Dict[str, Any] | None = None) -> bool:
    s = settings or load_settings()
    url = s["lm_studio_url"].rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {s.get('api_key', 'lm-studio')}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _parse_json_array(raw: str) -> List[Any]:
    raw = raw or ""
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, list) else []
        except Exception:
            pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            data = json.loads(m.group(0))
            for key in ("values", "items", "pii", "result"):
                if isinstance(data.get(key), list):
                    return data[key]
        except Exception:
            pass
    quoted = re.findall(r'"([^"\n]{3,200})"', raw)
    if quoted:
        return quoted
    return [ln.strip(" -•\t") for ln in raw.splitlines() if 3 <= len(ln.strip(" -•\t")) <= 200]


def detect_extra_pii(text: str) -> Tuple[List[str], bool]:
    """LLM ищет неструктурированные ПДн и реквизиты как fallback для регексов."""
    s = load_settings()
    llm = get_llm(s)
    text = (text or "").strip()
    if llm is None or not text:
        return [], False
    context = rag_search("ФИО организации адреса реквизиты ИНН ОГРН БИК правила маскировки")
    limit = int(s.get("max_llm_chars", 12000))
    chunks = [text[i:i + 6000] for i in range(0, min(len(text), limit), 6000)]
    text_norm = text.replace("\u00ad", "-").replace("\xa0", " ").lower()
    found: set[str] = set()
    ok = False
    system = (
        "Ты — анализатор персональных данных. Отвечай только JSON-массивом строк, без пояснений. "
        "Из текста выпиши дословно: ФИО (кириллица и латиница), полные названия организаций, "
        "почтовые адреса, а также реквизиты, если они есть: ИНН (10/12 цифр), ОГРН/ОГРНИП (13/15), "
        "БИК (9), расчётный счёт (20), КПП, СНИЛС, паспорт, телефоны, email. "
        "Выписывай значения в том виде, в котором они встречаются в тексте (с пробелами/дефисами). "
        "Если ничего нет — верни []. Не придумывай значения."
    )
    for chunk in chunks:
        prompt = (f"Правила:\n{context}\n\nТекст:\n{chunk}\n\n"
                  "Верни только JSON-массив найденных строк. Пример: [\"Иванов Иван Иванович\", "
                  "\"ООО Ромашка\", \"ИНН 7707083893\", \"г. Москва, ул. Тверская, д. 1\"]")
        try:
            resp = llm.invoke([("system", system), ("human", prompt)])
            raw = resp.content if hasattr(resp, "content") else str(resp)
            ok = True
            for item in _parse_json_array(str(raw)):
                if isinstance(item, dict):
                    item = item.get("value") or item.get("text") or item.get("name") or ""
                v = _clean_value(item) if isinstance(item, str) else ""
                if 3 <= len(v) <= 200 and v != MASK and v.lower() in text_norm:
                    found.add(v)
        except Exception as e:
            logger.warning(f"LLM detect: {e}")
            break
    return sorted(found), ok


# ---------------------------------------------------------------- обработка файлов
def extract_pdf_text(path: Path) -> str:
    import pymupdf
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            raise ValueError("PDF защищён паролем")
        return "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def _pdf_text_ratio(path: Path) -> float:
    """Доля страниц с непустым текстом (0..1). Помогает выявить сканы."""
    try:
        import pymupdf
        doc = pymupdf.open(path)
        try:
            if not len(doc):
                return 0.0
            non_empty = 0
            for page in doc:
                if page.get_text("text").strip():
                    non_empty += 1
            return non_empty / len(doc)
        finally:
            doc.close()
    except Exception:
        return 1.0


def _cyr_font() -> str | None:
    """Ищет кириллический TTF в стандартных каталогах Windows / Linux / macOS."""
    candidates = [
        # Windows
        (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / n)
        for n in ("arial.ttf", "segoeui.ttf", "calibri.ttf", "times.ttf", "tahoma.ttf")
    ] + [
        # Linux
        Path("/usr/share/fonts/truetype/dejavu") / n for n in ("DejaVuSans.ttf", "DejaVuSerif.ttf")
    ] + [
        Path("/usr/share/fonts/truetype/liberation") / n for n in ("LiberationSans-Regular.ttf",)
    ] + [
        # macOS
        Path("/Library/Fonts") / n for n in ("Arial.ttf", "Times New Roman.ttf")
    ] + [
        Path("/System/Library/Fonts/Supplemental") / n for n in ("Arial.ttf", "Times New Roman.ttf")
    ]
    for p in candidates:
        try:
            if p.exists() and p.stat().st_size > 0:
                return str(p)
        except Exception:
            continue
    return None


def _text_width(font, text: str, size: float) -> float:
    try:
        return float(font.text_length(text, fontsize=size))
    except Exception:
        return len(text) * size * 0.55


def _span_color(span: Dict[str, Any]) -> Tuple[float, float, float]:
    c = span.get("color", 0)
    if not isinstance(c, int):
        return (0.0, 0.0, 0.0)
    return (((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)


def mask_pdf_file(input_path: Path, output_path: Path, extras: Any = ()) -> Dict[str, Any]:
    """Заменить ПДн в PDF, перерисовывая затронутые текстовые фрагменты целиком.

    Красные аннотации ставятся на весь span, а не на отдельное значение: иначе
    PyMuPDF после удаления части строки перерисовывает остаток span и сдвигает
    соседние метки (например, «ОГРН»).
    """
    import pymupdf
    doc = pymupdf.open(input_path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF защищён паролем")
    total = 0
    pages = len(doc)
    fontfile = _cyr_font()
    font = pymupdf.Font(fontfile=fontfile) if fontfile else None
    masker = Masker()
    for page in doc:
        page_dict = page.get_text("dict")
        all_spans: List[Dict[str, Any]] = []
        for block in page_dict.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        all_spans.append(span)
        jobs: List[Tuple[Any, Tuple[float, float], float, str, Tuple[float, float, float]]] = []
        for span in all_spans:
            text = span.get("text", "")
            if not text:
                continue
            fake = mask_text(text, extras, masker)
            if fake == text:
                continue
            bbox = pymupdf.Rect(span["bbox"])
            origin = span.get("origin") or (bbox.x0, bbox.y1)
            size = float(span.get("size") or 10.0)
            next_x = page.rect.x1 - 18
            for other in all_spans:
                if other is span:
                    continue
                ob = pymupdf.Rect(other["bbox"])
                if ob.x0 <= bbox.x0 + 1:
                    continue
                overlap = min(bbox.y1, ob.y1) - max(bbox.y0, ob.y0)
                if overlap > 0.4 * min(bbox.height, ob.height):
                    next_x = min(next_x, ob.x0)
            avail = max(12.0, next_x - bbox.x0 - 2)
            if font is not None:
                fs = size
                width = _text_width(font, fake, fs)
                if width > avail > 0:
                    fs = max(4.0, fs * avail / width)
                draw_text = fake
            else:
                fs = max(4.0, min(11.0, size))
                draw_text = MASK
            jobs.append((bbox, (float(origin[0]), float(origin[1])), fs,
                         draw_text, _span_color(span)))
        if not jobs:
            continue
        for bbox, _, _, _, _ in jobs:
            page.add_redact_annot(bbox, fill=None)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        for _, origin, fs, draw_text, color in jobs:
            if fontfile:
                page.insert_text(origin, draw_text, fontname="F0", fontfile=fontfile,
                                 fontsize=fs, color=color)
            else:
                page.insert_text(origin, draw_text, fontname="helv", fontsize=fs, color=color)
            total += 1
    doc.save(output_path, garbage=3, deflate=True)
    doc.close()
    return {"masked": total, "pages": pages}


def extract_excel_text(path: Path, limit: int = 20000) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts, size = [], 0
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if v is None:
                        continue
                    s = str(v)
                    if s.strip():
                        parts.append(s)
                        size += len(s) + 1
                        if size >= limit:
                            return "\n".join(parts)
    finally:
        wb.close()
    return "\n".join(parts)


def _excel_preview(ws, max_rows: int = 8, max_cols: int = 8) -> List[List[str]]:
    preview: List[List[str]] = []
    try:
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            preview.append([("" if c is None else str(c))[:60] for c in row[:max_cols]])
    except Exception:
        pass
    return preview


def mask_excel_file(input_path: Path, output_path: Path, extras: Any = ()) -> Dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(input_path)
    masker = Masker()
    masked = 0
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    v = cell.value
                    if v is None or isinstance(v, bool):
                        continue
                    if isinstance(v, str):
                        if v and not v.startswith("="):
                            nv = mask_text(v, extras, masker)
                            if nv != v:
                                cell.value = nv
                                masked += 1
                    elif isinstance(v, (datetime_cls, date_cls)):
                        cell.value = v + timedelta(days=random.randint(-3650, 3650))
                        masked += 1
                    elif isinstance(v, (int, float)):
                        # Если это похоже на реквизит (ИНН/ОГРН/БИК/р/с/КПП) — маскируем
                        # как соответствующий тип, иначе как число.
                        raw = str(int(v)) if isinstance(v, int) else str(v)
                        kind = _infer_kind(raw)
                        if kind in ("inn", "ogrn", "ogrn_bare", "bik", "rs", "kpp", "snils"):
                            fake = masker.get(kind, raw)
                            try:
                                cell.value = int(fake) if isinstance(v, int) else float(fake.replace(" ", "").replace(",", "."))
                                masked += 1
                            except Exception:
                                pass
                        else:
                            fake = masker.get("number", raw)
                            try:
                                cell.value = (int(fake) if isinstance(v, int)
                                              else float(fake.replace(" ", "").replace("\u00a0", "").replace(",", ".")))
                                masked += 1
                            except Exception:
                                pass
        wb.save(output_path)
        preview = _excel_preview(wb.active)
    finally:
        wb.close()
    return {"masked": masked, "pages": 1, "preview_rows": preview}


def process_pdf(input_path: Path, output_path: Path) -> Dict[str, Any]:
    warning = None
    ratio = _pdf_text_ratio(input_path)
    if ratio < 0.5:
        warning = "PDF похож на скан: часть страниц без текстового слоя. OCR не поддерживается — эти страницы не будут обезличены."
        logger.warning(f"PDF scan-like: {input_path.name}, text_pages_ratio={ratio:.2f}")
    text = extract_pdf_text(input_path)
    extras, llm_ok = detect_extra_pii(text)
    stats = mask_pdf_file(input_path, output_path, extras)
    preview = ""
    try:
        import pymupdf
        doc = pymupdf.open(output_path)
        try:
            if len(doc):
                preview = doc[0].get_text("text")[:1800]
        finally:
            doc.close()
    except Exception:
        pass
    result: Dict[str, Any] = {"preview_type": "pdf", "preview": preview, "llm_used": llm_ok,
                              "extras": extras[:50], **stats}
    if warning:
        result["warning"] = warning
    return result


def process_excel(input_path: Path, output_path: Path) -> Dict[str, Any]:
    text = extract_excel_text(input_path)
    extras, llm_ok = detect_extra_pii(text)
    stats = mask_excel_file(input_path, output_path, extras)
    return {"preview_type": "excel", "preview": stats.pop("preview_rows", []),
            "llm_used": llm_ok, "extras": extras[:50], **stats}


# ---------------------------------------------------------------- FastAPI
app = FastAPI(title="PII Masker + RAG")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class LoginRequest(BaseModel):
    login: str
    password: str


@app.get("/", response_class=HTMLResponse)
def root():
    path = BASE_DIR / "index.html"
    if not path.exists():
        path = Path(__file__).parent / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8") if path.exists() else "<h1>index.html не найден</h1>")


@app.post("/api/login")
def login(req: LoginRequest):
    if authenticate(req.login, req.password):
        logger.info(f"LOGIN | {req.login}")
        return {"ok": True, "login": req.login}
    logger.warning(f"LOGIN_FAIL | {req.login}")
    raise HTTPException(401, "Неверный логин или пароль")


@app.get("/api/me")
def me(user: str = Depends(get_current_user)):
    return {"login": user}


@app.get("/api/health")
def health():
    s = load_settings()
    if RETRIEVER is None:
        build_rag()
    return {
        "ok": True,
        "model": s["model"],
        "lm_studio_url": s["lm_studio_url"],
        "llm_ok": check_llm(s),
        "rag": RETRIEVER is not None,
        "frozen": bool(getattr(sys, "frozen", False)),
        "tray": TRAY_ICON is not None,
        "users_count": len(load_users()),
    }


def _safe_name(name: str) -> str:
    name = Path(name or "document").name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:200] or "document"


@app.post("/api/process")
def process(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    s = load_settings()
    original = _safe_name(file.filename or "document")
    ext = Path(original).suffix.lower()
    if ext not in (".pdf", ".xlsx"):
        # Явный и понятный отказ, в т.ч. для .xls (старый формат openpyxl не читает).
        raise HTTPException(400, "Поддерживаются только PDF и Excel (.xlsx)")

    max_mb = int(s.get("max_upload_mb", 50))
    content = file.file.read(max_mb * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > max_mb * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {max_mb} МБ")

    uid = uuid.uuid4().hex[:12]
    in_path = UPLOAD_DIR / f"{user}_{uid}_{original}"
    out_name = f"masked_{uid}_{original}"
    out_path = OUTPUT_DIR / out_name
    in_path.write_bytes(content)

    started = time.time()
    try:
        result = process_pdf(in_path, out_path) if ext == ".pdf" else process_excel(in_path, out_path)
    except Exception as e:
        logger.error(f"PROCESS_ERROR | {user} | {original} | {e}")
        raise HTTPException(500, f"Ошибка обработки: {e}")
    finally:
        in_path.unlink(missing_ok=True)

    elapsed = round(time.time() - started, 1)
    logger.info(f"MASK | user={user} | file={original} | masked={result.get('masked')} | "
                f"llm={result.get('llm_used')} | {elapsed}s")
    return {"ok": True, "filename": original, "download_name": f"masked_{original}",
            "download_url": f"/api/download/{out_name}", "elapsed": elapsed, **result}


@app.get("/api/download/{filename}")
def download(filename: str, user: str = Depends(get_current_user)):
    name = Path(filename).name
    path = (OUTPUT_DIR / name).resolve()
    if not str(path).startswith(str(OUTPUT_DIR.resolve()) + os.sep):
        raise HTTPException(400, "Недопустимое имя файла")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    media = ("application/pdf" if name.lower().endswith(".pdf")
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    logger.info(f"DOWNLOAD | {user} | {name}")
    return FileResponse(path, media_type=media, filename=name)


# ---------------------------------------------------------------- трей
TRAY_ICON = None


def _tray_image():
    from PIL import Image, ImageDraw
    base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    for candidate in ("icon.ico", "icon.png", "app.ico"):
        p = base / candidate
        if p.exists():
            try:
                return Image.open(p)
            except Exception:
                pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(47, 111, 235, 255))
    d.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 255))
    return img


def _open_browser(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}")


def _open_output_dir() -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(OUTPUT_DIR))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{OUTPUT_DIR}"')
        else:
            os.system(f'xdg-open "{OUTPUT_DIR}"')
    except Exception as e:
        logger.warning(f"open_output_dir: {e}")


def start_tray(port: int) -> None:
    global TRAY_ICON
    if not getattr(sys, "frozen", False):
        return
    try:
        import pystray
        from pystray import MenuItem as Item, Menu
    except Exception as e:
        logger.warning(f"pystray недоступен: {e}")
        return

    def on_open(icon, item):
        _open_browser(port)

    def on_outputs(icon, item):
        _open_output_dir()

    def on_quit(icon, item):
        try:
            icon.stop()
        finally:
            os._exit(0)

    menu = Menu(
        Item("Открыть сайт", on_open, default=True),
        Item("Папка результатов", on_outputs),
        Menu.SEPARATOR,
        Item("Выход", on_quit),
    )

    try:
        icon = pystray.Icon("pii_masker", _tray_image(), "PII Masker", menu)
        TRAY_ICON = icon
        icon.run()
    except Exception as e:
        logger.error(f"tray run: {e}")
        TRAY_ICON = None


# ---------------------------------------------------------------- запуск
class _NoConsoleStream:
    def write(self, *args, **kwargs):
        return 0

    def flush(self, *args, **kwargs):
        return 0

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("no console")


def _setup_file_logging() -> None:
    try:
        fh = logging.FileHandler(SERVER_LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.INFO)
    except Exception:
        pass


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _run_uvicorn(host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


def run_server() -> None:
    ensure_settings()
    ensure_users_file()
    build_rag()

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        if sys.stdout is None:
            sys.stdout = _NoConsoleStream()
        if sys.stderr is None:
            sys.stderr = _NoConsoleStream()
        _setup_file_logging()

    s = load_settings()
    port = int(s.get("port", 9321))
    host = str(s.get("host", "0.0.0.0"))

    if not frozen:
        print(f"Сайт: http://127.0.0.1:{port}  |  в сети: http://{_lan_ip()}:{port}")

    if s.get("open_browser", True):
        threading.Timer(1.5, lambda: _open_browser(port)).start()

    use_tray = frozen and bool(s.get("tray_enabled", True))

    if use_tray:
        server_thread = threading.Thread(
            target=_run_uvicorn, args=(host, port), daemon=True, name="uvicorn"
        )
        server_thread.start()
        start_tray(port)
        if TRAY_ICON is None:
            server_thread.join()
    else:
        _run_uvicorn(host, port)


if __name__ == "__main__":
    run_server()