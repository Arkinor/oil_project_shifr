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
import math
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
    "max_tokens": 2048,
    "request_timeout": 120,
    "max_llm_chars": 30000,
    "llm_chunk_chars": 4000,
    "llm_enabled": True,
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
POSITIONS = ["Генеральный директор", "Директор", "Главный бухгалтер", "Бухгалтер",
             "Менеджер", "Инженер", "Юрист", "Начальник отдела", "Руководитель проекта",
             "Заместитель директора", "Специалист"]

# Служебные слова, которые сами по себе не являются ПДн.
STOP_VALUES = {
    "договор", "акт", "счёт", "счет", "итого", "всего", "приложение", "сторона",
    "стороны", "россия", "российская федерация", "рф", "город", "улица", "дом",
    "квартира", "инн", "огрн", "огрнип", "кпп", "бик", "снилс", "паспорт",
    "телефон", "адрес", "фио", "подпись", "директор", "бухгалтер", "организация",
    "наименование", "дата", "номер", "подписант", "представитель", "заказчик",
    "исполнитель", "покупатель", "продавец", "работник", "сотрудник", "клиент",
    "ооо", "оао", "зао", "пао", "ао", "ип", "фгуп", "гуп", "муп", "гбу", "гау",
    "ано", "нко", "фонд", "министерство", "департамент", "управление", "компания",
    "г.", "ул.", "д.", "кв.", "обл.", "стр.", "корп.", "пр-т", "наб.", "ш.",
    "место", "жительства", "нахождения", "регистрации", "почтовый",
}

# Слова-заголовки таблиц/анкет: три подряд таких слова — не ФИО и не организация.
HEADER_WORDS = {
    "фамилия", "имя", "отчество", "фио", "организация", "наименование",
    "адрес", "телефон", "тел", "почта", "email", "инн", "огрн", "огрнип",
    "кпп", "бик", "снилс", "паспорт", "дата", "номер", "сумма", "итого",
    "всего", "должность", "подпись", "расшифровка", "примечание", "код",
    "счет", "счёт", "банк", "получатель", "плательщик", "заказчик",
    "исполнитель", "продавец", "покупатель", "сторона", "договор", "акт",
    "приложение", "место", "жительства", "нахождения", "регистрации",
    "почтовый", "индекс", "город", "улица", "дом", "квартира", "корпус",
    "строение", "область", "район", "республика", "край", "поселок",
    "село", "микрорайон", "проспект", "переулок", "набережная", "шоссе",
    "бульвар", "площадь", "аллея", "тупик", "проезд", "литера", "офис",
    "помещение", "здание", "этаж", "комната", "руководитель", "директор",
    "бухгалтер", "главный", "менеджер", "специалист", "инженер", "начальник",
    "отдел", "отделение", "филиал", "представительство", "сотрудник",
    "работник", "клиент", "заемщик", "заёмщик", "страхователь", "пациент",
    "ученик", "студент", "арендатор", "арендодатель", "ответственный",
    "подписант", "представитель", "владелец", "гражданин", "гражданка",
    "россия", "российская", "федерация", "рф",
    "name", "first", "last", "surname", "middle", "full", "organization",
    "company", "address", "phone", "email", "date", "number", "signature",
    "position", "title", "code", "amount", "total", "note", "comments",
    # Названия колонок/граф в таблицах и бланках (точные формы).
    "услуга", "услуги", "услуг", "услугам", "товар", "товары", "товаров",
    "назначение", "назначения", "цена", "цены", "цен", "стоимость",
    "количество", "кол", "кол-во", "единица", "единицы", "единиц", "изм",
    "измерение", "измерения", "ед", "шт", "раздел", "период", "основание",
    "реквизиты", "валюта", "ставка", "процент", "стр", "строка", "пункт",
    "лист", "страница", "формат", "тип", "статус", "серия", "месяц", "год",
    "квартал", "день", "час", "документ", "вид", "характеристика",
    "показатель", "свойство", "направление", "категория", "группа", "класс",
    "подразделение", "согласование", "утверждение", "описание",
    "того", "ндс", "скидка", "надбавка", "остаток", "расход", "приход",
}

# Основы служебных названий: слово считается заголовком, если начинается с
# одной из них. Нужно для падежных форм («Товаров», «Услуг», «Назначению»),
# которые точное сравнение с HEADER_WORDS не покрывает. Специально не включаем
# основы, похожие на имена собственные/топонимы («остат» → Осташков,
# «площад» → площадь Ленина, «тариф»/«позиц» → названия организаций).
HEADER_STEMS = (
    "услуг", "товар", "назначен", "стоимост", "количествен", "единиц",
    "измерен", "номенклатур", "артикул", "описан", "характерист",
    "категор", "подразделен", "реквизит", "контрагент", "наименован",
    "показател", "примечан", "сведени", "данн", "значен",
)

# Служебные союзы/предлоги и буквы-маркеры внутри заголовка
# («Цена за единицу», «Итого без НДС», «п/п», «н/д»).
FIELD_FILLERS = {
    "за", "по", "из", "в", "во", "на", "и", "без", "с", "от", "для", "к",
    "под", "над", "или", "не", "п", "н", "д", "е", "у", "г",
}


def _is_header_like(value: str) -> bool:
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+", str(value or "").lower())
    return bool(words) and all(w in HEADER_WORDS for w in words)


def _is_field_name(value: str) -> bool:
    """Заголовок колонки/строки («Товары», «Назначение услуги», «Ед. изм.»).

    Такой фрагмент — подпись поля, а не персональные данные: его замена ломает
    структуру документа. Наличие цифры или слова вне служебного списка значит,
    что это данные, и они по-прежнему маскируются.
    """
    s = str(value or "")
    if re.search(r"\d", s):
        return False
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+", s.lower())
    if not words or len(words) > 4:
        return False
    for w in words:
        if w in HEADER_WORDS or w in FIELD_FILLERS:
            continue
        if w.startswith(HEADER_STEMS):
            continue
        return False
    return True


_HEADER_SENSITIVE_KINDS = {"fio", "fio_initials", "fio_lat", "signature"}


def _has_header_word(value: str) -> bool:
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+", str(value or "").lower())
    return any(w in HEADER_WORDS for w in words)


VALID_KINDS = {
    "fio", "fio_initials", "fio_lat", "surname", "name", "patronymic",
    "org", "org_name", "abbr", "address", "address_lat", "city", "street",
    "house", "apartment", "index", "position", "signature", "birthdate",
    "email", "phone", "passport", "snils", "inn", "ogrn", "ogrn_bare", "kpp",
    "bik", "rs", "date", "number", "extra",
}

KIND_ALIASES = {
    "фио": "fio", "person": "fio", "person_name": "fio", "full_name": "fio",
    "fullname": "fio", "имя_фамилия": "fio", "фио_полностью": "fio",
    "фио_инициалы": "fio_initials", "initials": "fio_initials",
    "инициалы": "fio_initials", "fio_initials": "fio_initials",
    "фамилия": "surname", "last_name": "surname", "lastname": "surname",
    "surname": "surname", "family_name": "surname",
    "имя": "name", "first_name": "name", "firstname": "name", "given_name": "name",
    "отчество": "patronymic", "middle_name": "patronymic", "middlename": "patronymic",
    "patronymic": "patronymic", "patronym": "patronymic",
    "организация": "org", "organization": "org", "company": "org", "org": "org",
    "legal_entity": "org", "юридическое_лицо": "org", "название_организации": "org",
    "org_name": "org_name", "brand": "org_name", "название": "org_name",
    "краткое_наименование": "org_name",
    "адрес": "address", "address": "address", "location": "address",
    "город": "city", "city": "city", "town": "city", "населённый_пункт": "city",
    "улица": "street", "street": "street", "проспект": "street",
    "дом": "house", "house": "house", "building": "house", "здание": "house",
    "квартира": "apartment", "apartment": "apartment", "flat": "apartment",
    "офис": "apartment", "помещение": "apartment",
    "индекс": "index", "postal_code": "index", "zip": "index", "почтовый_индекс": "index",
    "должность": "position", "position": "position", "job_title": "position",
    "профессия": "position",
    "подпись": "signature", "signature": "signature", "signatory": "signature",
    "подписант": "signature", "электронная_подпись": "signature",
    "аббревиатура": "abbr", "abbreviation": "abbr", "abbr": "abbr", "сокращение": "abbr",
    "дата_рождения": "birthdate", "birthdate": "birthdate", "birth_date": "birthdate",
    "email": "email", "почта": "email", "e_mail": "email",
    "телефон": "phone", "phone": "phone", "тел": "phone",
    "паспорт": "passport", "passport": "passport",
    "снилс": "snils", "snils": "snils",
    "инн": "inn", "inn": "inn", "огрн": "ogrn", "ogrn": "ogrn",
    "огрнип": "ogrn", "кпп": "kpp", "kpp": "kpp", "бик": "bik", "bik": "bik",
    "расчётный_счёт": "rs", "расчетный_счет": "rs", "р/с": "rs", "account": "rs",
    "дата": "date", "date": "date", "номер": "number", "number": "number",
    "other": "extra", "другое": "extra", "extra": "extra",
}


_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:ФИО|Ф\.И\.О\.|ФАМИЛИЯ|ИМЯ|ОТЧЕСТВО|"
    r"ИНН|ОГРНИП|ОГРН|КПП|БИК|СНИЛС|ПАСПОРТ(?:\s+РФ)?|"
    r"Р\/С|К\/С|РАСЧ[ЕЁ]ТНЫЙ\s+СЧ[ЕЁ]Т|КОРР?\.?\s*СЧ[ЕЁ]Т|ТЕЛЕФОН|ТЕЛ\.|"
    r"EMAIL|E-MAIL|АДРЕС|МЕСТО\s+ЖИТЕЛЬСТВА|МЕСТО\s+НАХОЖДЕНИЯ|"
    r"ОРГАНИЗАЦИЯ|НАИМЕНОВАНИЕ|ДОЛЖНОСТЬ|ПРОФЕССИЯ|"
    r"ДАТА\s+РОЖДЕНИЯ|РАСШИФРОВКА\s+ПОДПИСИ|"
    r"ПОДПИСЬ|ПОДПИСАНТ|ГРАЖДАНИН|ГРАЖДАНКА|ФИЗЛИЦО)\s*[:\-]?\s*", re.I)


_ZERO_WIDTH = "\u00ad\u200b\u200c\u200d\ufeff"


def _clean_value(v: Any) -> str:
    v = str(v or "")
    for ch in _ZERO_WIDTH:
        v = v.replace(ch, "")
    v = v.replace("\xa0", " ").replace("\u2011", "-")
    v = v.strip(" \t\r\n,;")
    # Снимаем только парные обрамляющие кавычки: одиночная кавычка ломает поиск.
    for left, right in (("«", "»"), ('"', '"'), ("'", "'")):
        if len(v) >= 2 and v.startswith(left) and v.endswith(right):
            v = v[1:-1].strip()
            break
    v = re.sub(r"\s+", " ", v).strip()
    # Метку («ИНН», «Адрес», «ФИО» и т.п.) не заменяем — оставляем в документе.
    return _LABEL_PREFIX_RE.sub("", v, count=1).strip()


def _norm_char(ch: str) -> str:
    if ch in "\u00a0\u2007\u202f":
        return " "
    if ch in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212":
        return "-"
    if ch in "\u2018\u2019\u201a\u201b\u2032":
        return "'"
    if ch in "\u201c\u201d\u201e\u201f\u2033":
        return '"'
    return ch


def _normalize_with_map(text: str) -> Tuple[str, List[int]]:
    """Нормализует текст, сохраняя карту «позиция -> индекс в исходнике»."""
    out: List[str] = []
    idx: List[int] = []
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH:
            continue
        out.append(_norm_char(ch))
        idx.append(i)
    return "".join(out), idx


def _flex_pattern(value: str) -> str | None:
    """Гибкий шаблон: любые пробелы/переносы, кавычки, тире, ё/е."""
    v = _clean_value(value)
    if not v:
        return None
    v = (v.replace("«", '"').replace("»", '"').replace("„", '"')
         .replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'"))
    parts: List[str] = []
    for tok in v.split(" "):
        if not tok:
            continue
        e = re.escape(tok)
        e = e.replace(r"\.", r"\.?")
        e = e.replace('"', '["«»„“”]?')
        e = e.replace("'", "['‘’]?")
        e = e.replace(r"\-", "[-–—]")
        e = e.replace("ё", "[её]").replace("Ё", "[ЕЁ]")
        parts.append(e)
    if not parts:
        return None
    # значение из LLM обычно приходит без обрамляющих кавычек — захватываем их,
    # чтобы не оставалось «»» или незакрытой « после подстановки фейка
    return r'(?<!\w)["«»„“”]?' + r"\s+".join(parts) + r'[»"]?(?!\w)'


def _find_occurrences(text: str, value: str) -> List[re.Match]:
    pat = _flex_pattern(value)
    if not pat:
        return []
    try:
        return list(re.finditer(pat, text, re.I))
    except re.error:
        return []


def _is_bad_value(v: str) -> bool:
    v = str(v or "").strip()
    if len(v) < 2 or len(v) > 300 or v == MASK:
        return True
    if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", v):
        return True
    return (v.lower() in STOP_VALUES or _is_header_like(v)
            or _is_field_name(v))


def _map_kind(kind: str) -> str:
    k = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    if k in KIND_ALIASES:
        return KIND_ALIASES[k]
    return k if k in VALID_KINDS else ""


# (тип, regex). Совпадение целиком заменяется похожим ненастоящим значением.
# Регулярки — страховочная сеть для структурированных данных и явных маркеров;
# основной поиск сущностей делает LLM (см. detect_pii_llm).
PLAIN_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")),
    ("phone", re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")),
    ("passport", re.compile(r"(?<!\d)\d{2}\s?\d{2}(?:\s*(?:№|номер)\s*|\s+)\d{6}(?!\d)", re.I)),
    ("snils", re.compile(r"(?<!\d)\d{3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{2}(?!\d)")),
    ("kpp", re.compile(r"(?<!\d)\d{4}[A-ZА-Я]{2}\d{3}(?!\d)", re.I)),
    # Метки («ИНН», «ОГРН», «БИК»...) не входят в совпадение — их сохраняет
    # GROUP_PATTERNS, которые меняют только числовое значение.
    ("inn", re.compile(r"(?<!\d)(?:\d{10}|\d{12})(?!\d)")),
    ("ogrn", re.compile(r"(?<!\d)(?:\d{13}|\d{15})(?!\d)")),
    ("bik", re.compile(r"(?<!\d)\d{9}(?!\d)")),
    ("rs", re.compile(r"(?<!\d)\d{20}(?!\d)")),
    # --- Организации: только префикс + до 6 слов названия, без захвата всей строки ---
    ("org", re.compile(
        r"(?<![А-Яа-яЁёA-Za-z])"
        r"(?i:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО|Фонд|"
        r"Министерство|Департамент|Управление|Компания)\s*[«\"]?"
        r"[А-ЯЁA-Z][\w\-]*(?:\s+[А-ЯЁA-Z][\w\-]*){0,5}[»\"]?")),
    # --- ФИО: обычный регистр, ВЕРХНИЙ РЕГИСТР, смешанный, инициалы ---
    ("fio", re.compile(
        r"(?<![А-Яа-яЁёA-Za-z])"
        r"(?:[А-ЯЁ][а-яёА-ЯЁ-]{1,25}\s+){2}[А-ЯЁ][а-яёА-ЯЁ-]{1,25}"
        r"(?![А-Яа-яЁёA-Za-z])")),
    ("fio_initials", re.compile(
        r"(?<![А-Яа-яЁёA-Za-z])[А-ЯЁ][а-яёА-ЯЁ-]{1,25}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\."
        r"(?![А-Яа-яЁёA-Za-z])")),
    ("fio_initials", re.compile(
        r"(?<![А-Яа-яЁёA-Za-z])[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яёА-ЯЁ-]{1,25}"
        r"(?![А-Яа-яЁёA-Za-z])")),
    ("fio_lat", re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]{1,20}\s+){2}[A-Z][a-z]{1,20}(?![A-Za-z])")),
    # --- Адрес: только связки «маркер + значение», чтобы не съесть весь текст ---
    ("address", re.compile(
        r"(?<![А-Яа-яЁёA-Za-z])(?:"
        r"(?:г\.|город|ул\.|улица|пр-т|проспект|пер\.|переулок|наб\.|набережная|"
        r"ш\.|шоссе|д\.|дом|кв\.|квартира|корп\.|корпус|стр\.|строение|обл\.|область|"
        r"респ\.|республика|п\.|пос\.|поселок|с\.|село|мкр\.|микрорайон)"
        r"\s*[^\s,;]{1,40}"
        r")(?:\s*,?\s*(?:"
        r"г\.|город|ул\.|улица|пр-т|проспект|пер\.|переулок|наб\.|набережная|"
        r"ш\.|шоссе|д\.|дом|кв\.|квартира|корп\.|корпус|стр\.|строение|обл\.|область|"
        r"респ\.|республика|п\.|пос\.|поселок|с\.|село|мкр\.|микрорайон)"
        r"\s*[^\s,;]{1,40}){0,5}")),
    ("address_lat", re.compile(
        r"(?<![A-Za-z])(?:st\.|street|ave\.|avenue|rd\.|road|blvd\.|boulevard|"
        r"apt\.|apartment|suite|bldg\.|building|city|town)\s+[^\n,;]{1,60}"
        r"(?:,\s*[^\n,;]{1,60}){0,3}", re.I)),
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
        r"Исполнитель|Заказчик|Ответственный|Гражданин|Гражданка|Физлицо|"
        r"Подпись|Расшифровка\s+подписи)\s*[:\-]?\s*"
        r"([А-ЯЁ][А-ЯЁа-яё.\-]*(?:\s+[А-ЯЁ][А-ЯЁа-яё.\-]*){0,3})"), 1),
    ("surname", re.compile(
        r"(?:Фамилия)\s*[:\-]?\s*([А-ЯЁ][А-ЯЁа-яё\-]{1,40})", re.I), 1),
    ("name", re.compile(
        r"(?:Имя)\s*[:\-]?\s*([А-ЯЁ][А-ЯЁа-яё\-]{1,40})", re.I), 1),
    ("patronymic", re.compile(
        r"(?:Отчество)\s*[:\-]?\s*([А-ЯЁ][А-ЯЁа-яё\-]{1,40})", re.I), 1),
    ("position", re.compile(
        r"(?:Должность|Профессия)\s*[:\-]?\s*([А-ЯЁ][^\n;]{2,80})", re.I), 1),
    ("fio_lat", re.compile(
        r"(?:Name|Full\s*name|Director|Owner|Employee|Customer|Borrower|Client|"
        r"Representative|Signatory)\s*[:\-]?\s*([A-Z][A-Za-z.\t -]{2,80})", re.I), 1),
    ("org", re.compile(
        r"(?:Организация|Наименование|Работодатель|Компания|Страхователь|Продавец|"
        r"Покупатель|Исполнитель|Заказчик)\s*[:\-]?\s*"
        r"((?:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО)?\s*[«\"]?"
        r"[А-ЯЁA-Z][\w\-]*(?:\s+[А-ЯЁA-Z][\w\-]*){0,5}[»\"]?)"), 1),
    ("address", re.compile(
        r"(?:Адрес|Место жительства|Место нахождения|Почтовый адрес|Место регистрации|"
        r"Address)\s*[:\-]?\s*([^\n;]{5,150})", re.I), 1),
    ("city", re.compile(
        r"(?:Город|Населённый\s+пункт|Населенный\s+пункт)\s*[:\-]?\s*"
        r"([А-ЯЁ][^\n;,]{1,60})", re.I), 1),
    ("street", re.compile(
        r"(?:Улица|Проспект|Переулок|Набережная|Шоссе|Бульвар|Площадь|Аллея|"
        r"Проезд|Тупик)\s*[:\-]?\s*([А-ЯЁ][^\n;,]{1,60})", re.I), 1),
    ("house", re.compile(
        r"(?:Дом|Корпус|Строение|Здание)\s*[:\-]?\s*([0-9][^\n;,]{0,20})", re.I), 1),
    ("apartment", re.compile(
        r"(?:Квартира|Офис|Помещение|Комната)\s*[:\-]?\s*"
        r"([0-9][^\n;,]{0,20})", re.I), 1),
    ("index", re.compile(
        r"(?:Почтовый\s+индекс|Индекс)\s*[:\-]?\s*(\d{6})", re.I), 1),
    ("birthdate", re.compile(
        r"(?:Дата\s+рождения|Д\.\s?р\.)\s*[:\-]?\s*"
        r"(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}[./-]\d{2}[./-]\d{2})", re.I), 1),
]

PRIORITY = {
    "email": 10, "phone": 10,
    "passport": 9, "snils": 9, "kpp": 9, "inn": 9, "ogrn": 9, "ogrn_bare": 9,
    "bik": 9, "rs": 9,
    "date": 8, "birthdate": 8,
    "fio": 7, "fio_initials": 7, "fio_lat": 7, "surname": 7, "name": 7, "patronymic": 7,
    "org": 7, "org_name": 7, "abbr": 7,
    "address": 7, "address_lat": 7, "city": 7, "street": 7, "house": 7,
    "apartment": 7, "index": 7, "position": 7, "signature": 7,
    "number": 1, "extra": 5,
}
EXTRA_PRIORITY = 11  # целые сущности от LLM важнее регулярных догадок
COMPONENT_KINDS = {"surname", "name", "patronymic", "fio_initials", "org_name",
                   "city", "street", "house", "apartment", "index", "abbr", "position"}


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
    if len(digits) == 9:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return str(sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10)
    if len(digits) == 11:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(int(d) * w for d, w in zip(digits, w1)) % 11 % 10
        c2 = sum(int(d) * w for d, w in zip(digits, w2)) % 11 % 10
        return f"{c1}{c2}"
    return ""


def _ogrn_checksum(digits: str) -> str:
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
    s = str(v or "").strip()
    low = s.lower()
    if "@" in s:
        return "email"
    if re.search(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}", s):
        return "phone"
    if re.search(r"[А-ЯЁ]\.\s?[А-ЯЁ]\.", s):
        return "fio_initials"
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
    if re.search(r"(?i:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО|"
                 r"Фонд|Министерство|Департамент|Управление|Компания)", s):
        return "org"
    if re.search(r"(?i:г\.|город|ул\.|улица|д\.|дом|кв\.|квартира|обл\.|область|"
                 r"пр-т|проспект|наб\.|ш\.|шоссе|мкр\.|пос\.|с\.|респ\.)", s):
        return "address"
    if re.search(r"(?i:st\.|street|ave\.|avenue|rd\.|road|blvd\.|boulevard|apt\.|suite|bldg\.)", s):
        return "address_lat"
    words = re.findall(r"[А-ЯЁA-Z][а-яёa-z\-]+", s)
    if 2 <= len(words) <= 4 and len(words) == len(s.split()):
        if re.search(r"[A-Za-z]", s) and not re.search(r"[А-Яа-яЁё]", s):
            return "fio_lat"
        return "fio"
    if s.isupper() and re.fullmatch(r"[А-ЯЁA-Z][А-ЯЁA-Z\-]{1,20}", s):
        return "abbr"
    if re.fullmatch(r"[\d\s.,-]+", s):
        return "number"
    return "extra"


def _apply_case(fake: str, original: str) -> str:
    letters = [c for c in original if c.isalpha()]
    if not letters:
        return fake
    if all(c.isupper() for c in letters):
        return fake.upper()
    if all(c.islower() for c in letters):
        return fake.lower()
    return fake


class Masker:
    """Генерирует и запоминает фейковые значения в рамках одного документа."""

    def __init__(self):
        self.fake = Faker("ru_RU")
        self.cache: Dict[Tuple[str, str], str] = {}

    def get(self, kind: str, original: str) -> str:
        kind = _map_kind(kind) or _infer_kind(original)
        original = str(original or "")
        base = self._component(kind, original)
        return _apply_case(base, original)

    def _component(self, kind: str, original: str) -> str:
        key = (kind, str(original or "").lower())
        if key not in self.cache:
            self.cache[key] = self._gen(kind, original)
        return self.cache[key]

    def _fio_parts(self, original: str) -> List[str]:
        return re.findall(r"[А-ЯЁA-Z][а-яёa-z\-]+", original)

    def _find_fio_by_surname(self, surname: str) -> str:
        key = surname.lower()
        for (kind, orig), val in self.cache.items():
            if kind == "fio" and orig.split()[:1] == [key]:
                return val
        return ""

    def _gen_initials(self, original: str) -> str:
        initials_first = bool(re.match(r"^\s*[А-ЯЁA-Z]\.", original))
        if initials_first:
            m = re.search(r"([А-ЯЁA-Z][а-яёa-z\-]+)\s*$", original)
            surname = m.group(1) if m else ""
        else:
            m = re.match(r"\s*([А-ЯЁA-Z][а-яёa-z\-]+)", original)
            surname = m.group(1) if m else ""
        cached = self._find_fio_by_surname(surname) if surname else ""
        if cached:
            parts = cached.split()
            if len(parts) >= 3:
                s, n, p = parts[0], parts[1], parts[2]
                return (f"{n[0]}.{p[0]}. {s}" if initials_first else f"{s} {n[0]}.{p[0]}.")
        s = self._component("surname", surname) if surname else self._component("surname", "")
        n = self._component("name", "")
        p = self._component("patronymic", "")
        return (f"{n[0]}.{p[0]}. {s}" if initials_first else f"{s} {n[0]}.{p[0]}.")

    def _gen_org(self, original: str) -> str:
        m = re.match(r"^\s*([А-ЯA-Z]+)\s+", original)
        prefix = m.group(1) if m and m.group(1).upper() in ORG_PREFIXES else ""
        if prefix.upper() == "ИП":
            rest = original[m.end():] if m else original
            return "ИП " + self._component("fio", rest)
        name = f"{random.choice(ORG_NAMES)} {random.choice(ORG_SUFFIXES)}"
        return f"{prefix} «{name}»" if prefix else f"«{name}»"

    def _street_name(self) -> str:
        """Название улицы без типа («ул.», «пр.», «алл.»), чтобы не было «ул. ул. ...»."""
        return re.sub(
            r"(?i)^(?:ул\.|улица|пр-т|пр\.|проспект|проезд|пер\.|переулок|наб\.|набережная|"
            r"ш\.|шоссе|бул\.|б-р|бульвар|пл\.|площадь|алл\.|аллея|туп\.|тупик|"
            r"мкр\.|микрорайон)\s*", "", self.fake.street_name())

    def _gen_address(self, original: str) -> str:
        marker = (r"(?i:г\.|город|ул\.|улица|пр-т|проспект|пер\.|переулок|наб\.|набережная|"
                  r"ш\.|шоссе|д\.|дом|кв\.|квартира|корп\.|корпус|стр\.|строение|обл\.|область|"
                  r"респ\.|республика|п\.|пос\.|поселок|с\.|село|мкр\.|микрорайон)")

        def repl(m: re.Match) -> str:
            label = m.group(1)
            low = label.lower().rstrip(".")
            if low in ("г", "город", "п", "пос", "поселок", "с", "село"):
                return f"{label} {self.fake.city_name()}"
            if low in ("ул", "улица", "пр-т", "проспект", "пер", "переулок",
                       "наб", "набережная", "ш", "шоссе", "мкр", "микрорайон"):
                return f"{label} {self._street_name()}"
            if low in ("д", "дом", "корп", "корпус", "стр", "строение"):
                return f"{label} {self.fake.building_number()}"
            if low in ("кв", "квартира"):
                return f"{label} {random.randint(1, 400)}"
            if low in ("обл", "область", "респ", "республика"):
                return f"{label} {self.fake.region()}"
            return m.group(0)

        out = re.sub(marker + r"\s+([^\s,;]+)", repl, original)
        if out == original:
            words = original.split()
            if len(words) >= 2:
                return (f"г. {self.fake.city_name()}, ул. {self._street_name()}, "
                        f"д. {self.fake.building_number()}")
            return self.fake.city_name()
        return out

    def _gen(self, kind: str, original: str) -> str:
        kind = _map_kind(kind) or _infer_kind(original)
        if kind == "fio":
            toks = self._fio_parts(original)
            if len(toks) >= 3:
                s = self._component("surname", toks[0])
                n = self._component("name", toks[1])
                p = self._component("patronymic", toks[2])
                return f"{s} {n} {p}"
            if len(toks) == 2:
                return f"{self._component('surname', toks[0])} {self._component('name', toks[1])}"
            return self._component("fio", self.fake.name())
        if kind == "surname":
            return _mix_letters(self.fake.last_name(), 0.06)
        if kind == "name":
            return self.fake.first_name()
        if kind == "patronymic":
            return self.fake.middle_name()
        if kind == "fio_initials":
            return self._gen_initials(original)
        if kind == "fio_lat":
            return self._fio_lat(original)
        if kind == "phone":
            compact = not any(c in original for c in " ()-")
            prefix = "8" if original.strip().startswith("8") else "+7"
            d = _rand_digits(10)
            return prefix + d if compact else f"{prefix} ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
        if kind == "email":
            return self.fake.email()
        if kind == "org":
            return self._gen_org(original)
        if kind == "org_name":
            name = f"{random.choice(ORG_NAMES)} {random.choice(ORG_SUFFIXES)}"
            if original.startswith("«") or original.startswith('"'):
                return f"«{name}»"
            return name
        if kind == "abbr":
            pool = CYR_UPPER if re.search(r"[А-ЯЁ]", original) else LAT_UPPER
            return "".join(random.choice(pool) if ch.isalpha() else ch for ch in original)
        if kind == "signature":
            if re.search(r"[А-ЯЁ]\.\s?[А-ЯЁ]\.", original):
                return self._gen_initials(original)
            if len(self._fio_parts(original)) >= 2:
                return self._gen("fio", original)
            return self._gen("fio", self.fake.name())
        if kind in ("address", "address_lat"):
            if kind == "address_lat":
                return (f"{self.fake.street_name()} St., Bldg. {self.fake.building_number()}, "
                        f"{self.fake.city()}")
            return self._gen_address(original)
        if kind == "city":
            m = re.match(r"^\s*((?:г\.|город)\s*)", original, re.I)
            return (m.group(1) if m else "") + self.fake.city_name()
        if kind == "street":
            m = re.match(
                r"^\s*((?:ул\.|улица|пр-т|пр\.|проспект|пер\.|переулок|наб\.|набережная|"
                r"ш\.|шоссе|бул\.|бульвар|пл\.|площадь|алл\.|аллея)\s*)", original, re.I)
            return (m.group(1) if m else "") + self._street_name()
        if kind == "house":
            m = re.match(r"^\s*((?:д\.|дом|корп\.|корпус|стр\.|строение)\s*)", original, re.I)
            return (m.group(1) if m else "") + self.fake.building_number()
        if kind == "apartment":
            m = re.match(r"^\s*((?:кв\.|квартира|офис|помещение)\s*)", original, re.I)
            return (m.group(1) if m else "") + str(random.randint(1, 400))
        if kind == "index":
            return _rand_digits(6)
        if kind == "position":
            return random.choice(POSITIONS)
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
        if kind in ("date", "birthdate"):
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
        if kind in ("extra", "other", ""):
            inferred = _infer_kind(original)
            if inferred not in ("extra", "other") and inferred != kind:
                return self._gen(inferred, original)
        return _mix_letters(" ".join(self.fake.word().capitalize()
                                     for _ in range(max(1, len(original.split())))), 0.05)

    def _fio_lat(self, original: str) -> str:
        first = self.fake.first_name_male() if random.random() < 0.5 else self.fake.first_name_female()
        last = self.fake.last_name_male() if random.random() < 0.5 else self.fake.last_name_female()
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

        return f"{translit(last)} {translit(first)}"

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


def _iter_extras(extras: Any):
    if not extras:
        return
    for x in extras:
        if isinstance(x, dict):
            v = x.get("text") or x.get("value") or x.get("name") or ""
            k = str(x.get("type") or x.get("kind") or "")
        else:
            v, k = str(x), ""
        v = _clean_value(v)
        if v and not _is_bad_value(v):
            yield v, k


def _add_candidate(cands: List[Tuple[int, int, str, str, int]], start: int, end: int,
                   kind: str, raw: str, priority: int | None = None) -> None:
    left = len(raw) - len(raw.lstrip())
    right = len(raw) - len(raw.rstrip())
    start += left
    end -= right
    value = raw.strip()
    # Название колонки/строки шапки («Наименование Товаров Цена» часто склеивается
    # регуляркой в одно ФИО/организацию) — служебный текст, менять его нельзя.
    if end - start >= 2 and not _is_header_like(value) and not _is_field_name(value):
        # Заголовок таблицы, склеенный со значением («Фамилия Иванов Имя»),
        # не должен превращаться в ФИО/организацию.
        if kind in _HEADER_SENSITIVE_KINDS and _has_header_word(value):
            return
        prio = PRIORITY.get(kind, 0) if priority is None else priority
        cands.append((start, end, kind, value, prio))


def iter_matches(text: str, extras: Any = ()) -> List[Tuple[int, int, str, str]]:
    """Возвращает непересекающиеся совпадения (start, end, тип, значение).

    Позиции — в исходной строке. Приоритет у сущностей от LLM: они выбираются
    первыми, регулярки добирают только то, что ИИ не назвал.
    """
    if not isinstance(text, str) or not text:
        return []
    norm, idx = _normalize_with_map(text)
    cands: List[Tuple[int, int, str, str, int]] = []
    for kind, rx, group in GROUP_PATTERNS:
        for m in rx.finditer(norm):
            _add_candidate(cands, m.start(group), m.end(group), kind, m.group(group))
    for kind, rx in PLAIN_PATTERNS:
        for m in rx.finditer(norm):
            _add_candidate(cands, m.start(), m.end(), kind, m.group(0))
    for value, raw_kind in _iter_extras(extras):
        kind = _map_kind(raw_kind) or _infer_kind(value)
        # части ФИО/адреса не должны перебивать более длинную целую сущность
        prio = PRIORITY.get(kind, 7) if kind in COMPONENT_KINDS else EXTRA_PRIORITY
        for m in _find_occurrences(norm, value):
            _add_candidate(cands, m.start(), m.end(), kind, m.group(0), prio)
    best: Dict[Tuple[int, int], Tuple[int, int, str, str, int]] = {}
    for c in cands:
        key = (c[0], c[1])
        prev = best.get(key)
        if prev is None or c[4] > prev[4]:
            best[key] = c
    selected: List[Tuple[int, int, str, str, int]] = []
    for c in sorted(best.values(), key=lambda x: (-x[4], -(x[1] - x[0]), x[0])):
        if all(c[1] <= s or c[0] >= e for s, e, _, _, _ in selected):
            selected.append(c)
    selected.sort(key=lambda x: x[0])
    result: List[Tuple[int, int, str, str]] = []
    for s, e, kind, value, _ in selected:
        if s >= len(idx) or e - 1 >= len(idx):
            continue
        os_, oe = idx[s], idx[e - 1] + 1
        result.append((os_, oe, kind, value))
    return result


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


def _keep_ws(piece: str, part: str) -> str:
    lead = piece[:len(piece) - len(piece.lstrip())]
    trail = piece[len(piece.rstrip()):]
    return lead + part + trail


def _distribute_fake(fake: str, pieces: List[str]) -> List[str]:
    """Распределяет одно фейковое значение по кускам, разбитым на несколько span/ячеек.

    Нужно, когда ФИО или организация разбиты жирным шрифтом/колонками таблицы.
    """
    if len(pieces) == 1:
        return [fake]
    toks = fake.split()
    counts = [len(p.split()) for p in pieces]
    if toks and all(c > 0 for c in counts) and sum(counts) == len(toks):
        out, pos = [], 0
        for p, c in zip(pieces, counts):
            part = " ".join(toks[pos:pos + c])
            pos += c
            out.append(_keep_ws(p, part))
        return out
    lens = [len(p.strip()) or 1 for p in pieces]
    total = sum(lens) or 1
    if not toks:
        return [None] * len(pieces)  # type: ignore[list-item]
    out: List[str] = []
    prev = 0
    acc = 0
    for i, (p, ln) in enumerate(zip(pieces, lens)):
        acc += ln
        if i == len(pieces) - 1:
            end = len(toks)
        else:
            end = max(prev + 1, int(round(acc / total * len(toks))))
            end = min(end, len(toks))
        part = " ".join(toks[prev:end])
        prev = end
        out.append(_keep_ws(p, part))
    if any(not p.strip() for p in out) and toks:
        # страховка: не оставляем пустых фрагментов
        out = [_keep_ws(pieces[i], toks[min(i, len(toks) - 1)]) for i in range(len(pieces))]
    return out

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
    "Заголовки колонок, граф и подписи полей НЕ обезличиваются: «Услуги», «Товары», «Назначение», "
    "«Наименование», «Количество», «Ед. изм.», «Цена», «Стоимость», «Сумма», «Итого», «Дата», "
    "«Номер», «Код», «Период», «Основание», «Категория», «Реквизиты», № п/п. В такой колонке "
    "меняют только конкретные значения — ФИО, организации, адреса и реквизиты.",
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
    cleaned = re.sub(r",\s*([\]}])", r"\1", raw)
    for candidate in (cleaned, raw):
        m = re.search(r"\[[\s\S]*\]", candidate)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        m = re.search(r"\{[\s\S]*\}", candidate)
        if m:
            try:
                data = json.loads(m.group(0))
                for key in ("items", "values", "pii", "result", "entities", "data"):
                    if isinstance(data.get(key), list):
                        return data[key]
            except Exception:
                pass
    # ответ невалидный JSON — вытаскиваем объекты и строки по отдельности
    objs = re.findall(r"\{[^{}]{0,500}\}", raw)
    if objs:
        out = []
        for o in objs:
            try:
                out.append(json.loads(o))
            except Exception:
                pass
        if out:
            return out
    quoted = re.findall(r'"([^"\n]{3,200})"', raw)
    if quoted:
        return quoted
    return [ln.strip(" -•\t") for ln in raw.splitlines() if 3 <= len(ln.strip(" -•\t")) <= 200]


LLM_SYSTEM = (
    "Ты — эксперт по обезличиванию документов. Найди в тексте ВСЕ фрагменты, которые "
    "позволяют идентифицировать человека, организацию или объект, и верни их ДОСЛОВНО, "
    "символ в символ, как в тексте.\n"
    "Что искать: ФИО полностью и по частям (фамилия, имя, отчество, инициалы); названия "
    "организаций (полные, краткие, аббревиатуры, бренды); должности и подписи; адреса "
    "полностью и по частям (город, улица, дом, корпус, квартира, индекс); телефоны, email; "
    "паспорт, СНИЛС, ИНН, ОГРН/ОГРНИП, КПП, БИК, счета; даты рождения; любые имена "
    "собственные, названия и номера, по которым можно идентифицировать человека или компанию.\n"
    "Ищи в любом оформлении: ЖИРНЫЙ, курсив, подчёркивание, ВЕРХНИЙ РЕГИСТР, разряженный "
    "текст, в кавычках, в таблицах, в реквизитах и в блоках электронной подписи.\n"
    "Если одно и то же название встречается в разных написаниях — верни каждое написание "
    "отдельно. Если сущность разбита переносом строки или колонками таблицы — верни её "
    "целиком одной строкой (пробелы между словами).\n"
    "Менять можно ТОЛЬКО ЗНАЧЕНИЯ полей, а не их подписи. НИКОГДА не возвращай "
    "заголовки колонок, граф, строк и разделов, названия полей и общие термины: "
    "«Договор», «Акт», «Стороны», «Приложение», «ИНН», «КПП», «Адрес», «Подпись», "
    "«Услуги», «Товары», «Назначение», «Наименование», «Количество», «Ед. изм.», "
    "«Цена», «Стоимость», «Сумма», «Итого», «Всего», «Дата», «Номер», «Код», "
    "«Период», «Основание», «Категория», «Подразделение», «Реквизиты», «Статус», "
    "«Показатель», «Характеристика», «Описание», № п/п. Замена такого текста ломает "
    "структуру документа.\n"
    "Заголовок и значение — разные фрагменты: если в шапке таблицы написано «Услуга», "
    "а в строке данных «Консультация аудитора», меняй только «Консультация аудитора».\n"
    "Не придумывай значений, которых нет в тексте. Если сущности нет — верни []. "
    "Ответ — ТОЛЬКО JSON-массив объектов "
    'вида [{"text":"...","type":"..."}], без markdown и пояснений.'
)

LLM_TYPES = (
    "type: fio | fio_initials | surname | name | patronymic | org | org_name | abbr | "
    "address | city | street | house | apartment | index | position | signature | "
    "email | phone | inn | ogrn | kpp | bik | rs | snils | passport | birthdate | date | "
    "number | other"
)


def _chunk_text(text: str, size: int, overlap: int) -> List[str]:
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    chunks = []
    for i in range(0, len(text), step):
        chunk = text[i:i + size]
        if chunk.strip():
            chunks.append(chunk)
        if i + size >= len(text):
            break
    return chunks


def detect_pii_llm(text: str) -> Tuple[List[Dict[str, str]], bool, str]:
    """Основной анализатор: LLM сама называет, что менять. Возвращает список сущностей."""
    s = load_settings()
    text = (text or "").strip()
    if not text:
        return [], False, "empty"
    if not s.get("llm_enabled", True):
        return [], False, "disabled"
    llm = get_llm(s)
    if llm is None:
        return [], False, "llm_init"
    limit = max(2000, int(s.get("max_llm_chars", 30000) or 30000))
    size = max(1500, int(s.get("llm_chunk_chars", 4000) or 4000))
    body = text[:limit]
    chunks = _chunk_text(body, size, 250)
    context = rag_search("ФИО организации адреса реквизиты подписи "
                         "заголовки колонок правила маскировки")
    found: Dict[str, Dict[str, str]] = {}
    ok = False
    error = ""
    for chunk in chunks:
        prompt = (
            f"Правила предметной области:\n{context}\n\n"
            f"Формат типа: {LLM_TYPES}\n\n"
            f"Текст документа:\n{chunk}\n\n"
            "Менять можно только значения. Не возвращай заголовки колонок и подписи "
            "полей («Услуги», «Товары», «Назначение», «Количество», «Цена», «Сумма»). "
            'Верни только JSON-массив: [{"text":"Иванов Иван Иванович","type":"fio"}, '
            '{"text":"ООО «Ромашка»","type":"org"}, {"text":"г. Москва, ул. Тверская, д. 1",'
            '"type":"address"}]. Если нечего менять — [].'
        )
        try:
            resp = llm.invoke([("system", LLM_SYSTEM), ("human", prompt)])
            raw = resp.content if hasattr(resp, "content") else str(resp)
            ok = True
        except Exception as e:
            error = str(e)
            logger.warning(f"LLM detect: {e}")
            break
        for item in _parse_json_array(str(raw)):
            if isinstance(item, dict):
                v = item.get("text") or item.get("value") or item.get("name") or ""
                k = str(item.get("type") or item.get("kind") or "")
            else:
                v, k = item, ""
            v = _clean_value(v) if isinstance(v, str) else ""
            if _is_bad_value(v):
                continue
            kind = _map_kind(k) or _infer_kind(v)
            if not _find_occurrences(chunk, v) and not _find_occurrences(body, v):
                continue  # модель переписала значение — не рискуем
            key = v.lower()
            prev = found.get(key)
            if prev is None or (prev.get("type") in ("", "extra", "other")
                                and kind not in ("", "extra", "other")):
                found[key] = {"text": v, "type": kind}
    return list(found.values()), ok, error


def expand_variants(extras: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Добавляет встречающиеся на практике варианты: фамилия, инициалы, короткое имя org."""
    out: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add(value: str, kind: str) -> None:
        v = _clean_value(value)
        if _is_bad_value(v):
            return
        key = v.lower()
        if key in seen:
            return
        seen.add(key)
        out.append({"text": v, "type": _map_kind(kind) or _infer_kind(v)})

    def add_fio_variants(value: str) -> None:
        toks = re.findall(r"[А-ЯЁA-Z][а-яёa-z\-]+", value)
        if len(toks) >= 3:
            surname, name, patr = toks[0], toks[1], toks[2]
            add(surname, "surname")
            add(f"{surname} {name[0]}.{patr[0]}.", "fio_initials")
            add(f"{surname} {name[0]}. {patr[0]}.", "fio_initials")
            add(f"{name[0]}.{patr[0]}. {surname}", "fio_initials")
        elif len(toks) == 2:
            add(toks[0], "surname")

    for e in extras or []:
        v = _clean_value(e.get("text", ""))
        kind = _map_kind(e.get("type", "")) or _infer_kind(v)
        if not v:
            continue
        add(v, kind)
        if kind in ("fio", "signature"):
            add_fio_variants(v)
        if kind in ("org", "org_name"):
            m = re.match(
                r"(?i)^(ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО|"
                r"Фонд|Министерство|Департамент|Управление|Компания)\s+(.+)$", v)
            if m and m.group(1).upper() == "ИП":
                # «ИП Иванов Иван Иванович» — это ФИО предпринимателя, а не бренд
                rest = m.group(2)
                add(rest, "fio")
                add_fio_variants(rest)
            else:
                inner = re.findall(r"[«\"]([^»\"]{2,80})[»\"]", v)
                if inner:
                    add(inner[0], "org_name")
                elif m:
                    add(m.group(2), "org_name")
        if kind == "address":
            m = re.search(r"(?i)(?:г\.|город)\s*([А-ЯЁ][\w\-]+)", v)
            if m:
                add(m.group(1), "city")
            m = re.search(r"(?i)(?:ул\.|улица|пр-т|проспект|пер\.|переулок|наб\.|"
                          r"набережная|ш\.|шоссе)\s*([А-ЯЁ][\w\-]+)", v)
            if m:
                add(m.group(1), "street")
    return out

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


_FONT_FILES: Dict[Tuple[bool, bool], str | None] = {}


def _font_path(bold: bool = False, italic: bool = False) -> str | None:
    """Ищет кириллический TTF нужного начертания в стандартных каталогах."""
    key = (bool(bold), bool(italic))
    if key in _FONT_FILES:
        return _FONT_FILES[key]
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if bold and italic:
        win_names = ["arialbi.ttf", "timesbi.ttf", "calibriz.ttf"]
    elif bold:
        win_names = ["arialbd.ttf", "timesbd.ttf", "calibrib.ttf", "tahomabd.ttf"]
    elif italic:
        win_names = ["ariali.ttf", "timesi.ttf", "calibrii.ttf"]
    else:
        win_names = ["arial.ttf", "segoeui.ttf", "calibri.ttf", "times.ttf", "tahoma.ttf"]
    candidates = [win / n for n in win_names] + [
        Path("/usr/share/fonts/truetype/dejavu") / n
        for n in (["DejaVuSans-BoldOblique.ttf"] if bold and italic else
                  ["DejaVuSans-Bold.ttf"] if bold else
                  ["DejaVuSans-Oblique.ttf"] if italic else ["DejaVuSans.ttf"])
    ] + [
        Path("/usr/share/fonts/truetype/liberation") / n
        for n in (["LiberationSans-BoldItalic.ttf"] if bold and italic else
                  ["LiberationSans-Bold.ttf"] if bold else
                  ["LiberationSans-Italic.ttf"] if italic else ["LiberationSans-Regular.ttf"])
    ] + [
        Path("/Library/Fonts") / n for n in ("Arial.ttf", "Times New Roman.ttf")
    ] + [
        Path("/System/Library/Fonts/Supplemental") / n for n in ("Arial.ttf", "Times New Roman.ttf")
    ]
    found = None
    for p in candidates:
        try:
            if p.exists() and p.stat().st_size > 0:
                found = str(p)
                break
        except Exception:
            continue
    _FONT_FILES[key] = found
    return found


def _cyr_font() -> str | None:
    return _font_path(False, False)


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


def _block_text(block: Dict[str, Any]) -> Tuple[str, List[Any], List[Dict[str, Any]]]:
    """Склеивает текст блока (включая несколько строк) и хранит карту до span/смещения."""
    spans: List[Dict[str, Any]] = []
    chars: List[str] = []
    cmap: List[Any] = []
    prev = ""
    for li, line in enumerate(block.get("lines", [])):
        rot = _rotation_from_dir(line.get("dir") or (1, 0))
        if li > 0:
            chars.append("\n")
            cmap.append(None)
            prev = "\n"
        for span in line.get("spans", []):
            text = span.get("text", "")
            if not text.strip() or span.get("_table_done") or span.get("_row_done"):
                continue
            span["_rot"] = rot
            si = len(spans)
            spans.append(span)
            first = text[0]
            if (chars and prev and not prev.isspace() and not first.isspace()
                    and prev.isalnum() and first.isalnum()):
                chars.append(" ")
                cmap.append(None)
            for ci, ch in enumerate(text):
                if ch in _ZERO_WIDTH:
                    continue
                nc = _norm_char(ch)
                chars.append(nc)
                cmap.append((si, ci))
                prev = nc
    return "".join(chars), cmap, spans


def _range_pieces(cmap: List[Any], start: int, end: int) -> List[Tuple[int, int, int]]:
    pieces: List[Tuple[int, int, int]] = []
    cur_si = None
    a = b = 0
    for i in range(start, end):
        ent = cmap[i] if i < len(cmap) else None
        if not ent:
            continue
        si, off = ent
        if cur_si == si and off == b:
            b = off + 1
        else:
            if cur_si is not None:
                pieces.append((cur_si, a, b))
            cur_si, a, b = si, off, off + 1
    if cur_si is not None:
        pieces.append((cur_si, a, b))
    return pieces


def _span_fully_covered(span_text: str, a: int, b: int) -> bool:
    stripped = span_text.strip()
    if not stripped:
        return False
    lead = len(span_text) - len(span_text.lstrip())
    trail = len(span_text) - len(span_text.rstrip())
    return a <= lead and b >= len(span_text) - trail


def _spans_same_line(group: List[Dict[str, Any]]) -> bool:
    try:
        ys = [float((s.get("origin") or (0, s["bbox"][3]))[1]) for s in group]
        sizes = [float(s.get("size") or 10.0) for s in group]
        return max(ys) - min(ys) <= max(2.0, 0.5 * max(sizes))
    except Exception:
        return False


def _rotation_from_dir(direction: Any) -> int:
    """Угол текста, кратный 90°: PyMuPDF insert_text умеет только такие."""
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except Exception:
        return 0
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0
    # В координатах PyMuPDF ось Y направлена вниз, поэтому знак dy меняем.
    angle = math.degrees(math.atan2(-dy, dx)) % 360.0
    nearest = int(round(angle / 90.0)) * 90 % 360
    diff = abs((angle - nearest + 180.0) % 360.0 - 180.0)
    # Небольшой наклон (сканы, «пьяный» текст) считаем горизонтальным.
    return nearest if diff <= 3.0 else 0


def _page_tables(page) -> List[Any]:
    """Найденные на странице таблицы (нужны и для ширины ячеек, и для строк)."""
    try:
        tables = []
        for table in page.find_tables().tables:
            # Одна ячейка на всю страницу — обычно ложное срабатывание рамки.
            if len(getattr(table, "cells", []) or []) >= 2:
                tables.append(table)
        return tables
    except Exception:
        return []


def _tables_to_cells(tables: List[Any]) -> List[Any]:
    try:
        import pymupdf
        return [pymupdf.Rect(cell) for table in tables
                for cell in getattr(table, "cells", []) if cell]
    except Exception:
        return []


def _containing_cell(cells: List[Any], rect) -> Any:
    if not cells:
        return None
    cx = (rect.x0 + rect.x1) / 2.0
    cy = (rect.y0 + rect.y1) / 2.0
    best = None
    for cell in cells:
        if (cell.x0 - 0.5 <= cx <= cell.x1 + 0.5
                and cell.y0 - 0.5 <= cy <= cell.y1 + 0.5):
            if best is None or abs(cell.get_area()) < abs(best.get_area()):
                best = cell
    return best


def _neighbor_limit(page_spans: List[Dict[str, Any]], span: Dict[str, Any],
                    bbox, rot: int, page_rect) -> float:
    """Ближайшая граница соседнего текста в направлении письма."""
    import pymupdf
    if rot == 90:  # текст идёт снизу вверх
        limit = page_rect.y0 + 6.0
        for other in page_spans:
            if other is span:
                continue
            ob = pymupdf.Rect(other["bbox"])
            if ob.y1 > bbox.y0 + 1.0:
                continue
            if min(bbox.x1, ob.x1) - max(bbox.x0, ob.x0) <= 0.2 * max(1.0, bbox.width):
                continue
            limit = max(limit, ob.y1)
        return limit
    if rot == 270:  # текст идёт сверху вниз
        limit = page_rect.y1 - 6.0
        for other in page_spans:
            if other is span:
                continue
            ob = pymupdf.Rect(other["bbox"])
            if ob.y0 < bbox.y1 - 1.0:
                continue
            if min(bbox.x1, ob.x1) - max(bbox.x0, ob.x0) <= 0.2 * max(1.0, bbox.width):
                continue
            limit = min(limit, ob.y0)
        return limit
    if rot == 180:  # текст идёт справа налево
        limit = page_rect.x0 + 6.0
        for other in page_spans:
            if other is span:
                continue
            ob = pymupdf.Rect(other["bbox"])
            if ob.x1 > bbox.x0 + 1.0:
                continue
            if min(bbox.y1, ob.y1) - max(bbox.y0, ob.y0) <= 0.2 * max(1.0, bbox.height):
                continue
            limit = max(limit, ob.x1)
        return limit
    limit = page_rect.x1 - 18.0
    for other in page_spans:
        if other is span:
            continue
        ob = pymupdf.Rect(other["bbox"])
        if ob.x0 <= bbox.x0 + 1.0:
            continue
        if min(bbox.y1, ob.y1) - max(bbox.y0, ob.y0) <= 0.4 * min(bbox.height, ob.height):
            continue
        limit = min(limit, ob.x0)
    return limit


def _avail_width(rot: int, origin: Tuple[float, float],
                 neighbor_limit: float, cell: Any) -> float:
    """Доступная длина строки с учётом соседей, ячейки таблицы и края страницы."""
    if rot == 90:
        boundary = neighbor_limit
        if cell is not None:
            boundary = max(boundary, cell.y0)
        return max(6.0, origin[1] - boundary - 2.0)
    if rot == 270:
        boundary = neighbor_limit
        if cell is not None:
            boundary = min(boundary, cell.y1)
        return max(6.0, boundary - origin[1] - 2.0)
    if rot == 180:
        boundary = neighbor_limit
        if cell is not None:
            boundary = max(boundary, cell.x0)
        return max(6.0, origin[0] - boundary - 2.0)
    boundary = neighbor_limit
    if cell is not None:
        boundary = min(boundary, cell.x1)
    return max(6.0, boundary - origin[0] - 2.0)


def _mask_table_rows(tables: List[Any], page_spans: List[Dict[str, Any]],
                     extras: Any, masker: Masker) -> int:
    """Склеивает ячейки одной строки таблицы: ФИО может быть разбито по колонкам."""
    import pymupdf
    if not tables or not page_spans:
        return 0
    count = 0
    used: set[int] = set()
    for table in tables:
        try:
            rows = table.rows
        except Exception:
            continue
        for row in rows:
            cell_spans: List[Tuple[Any, List[Dict[str, Any]]]] = []
            for cell in getattr(row, "cells", []) or []:
                if not cell:
                    continue
                rect = pymupdf.Rect(cell)
                inside: List[Dict[str, Any]] = []
                for span in page_spans:
                    if id(span) in used:
                        continue
                    r = pymupdf.Rect(span["bbox"])
                    cx = (r.x0 + r.x1) / 2.0
                    cy = (r.y0 + r.y1) / 2.0
                    if (rect.x0 - 0.5 <= cx <= rect.x1 + 0.5
                            and rect.y0 - 0.5 <= cy <= rect.y1 + 0.5):
                        inside.append(span)
                        used.add(id(span))
                if inside:
                    inside.sort(key=lambda s: (float(s["bbox"][1]),
                                               float(s["bbox"][0])))
                    cell_spans.append((rect, inside))
            if not cell_spans:
                continue
            chars: List[str] = []
            cmap: List[Any] = []
            flat_spans: List[Dict[str, Any]] = []
            last = ""
            for ci, (_, inside) in enumerate(cell_spans):
                if ci > 0:
                    chars.append(" ")
                    cmap.append(None)
                    last = " "
                for span in inside:
                    text = span.get("text", "")
                    if not text:
                        continue
                    si = len(flat_spans)
                    flat_spans.append(span)
                    if (chars and last and not last.isspace()
                            and not text[0].isspace()
                            and last.isalnum() and text[0].isalnum()):
                        chars.append(" ")
                        cmap.append(None)
                    for oi, ch in enumerate(text):
                        if ch in _ZERO_WIDTH:
                            continue
                        nc = _norm_char(ch)
                        chars.append(nc)
                        cmap.append((si, oi))
                        last = nc
            joined = "".join(chars)
            matches = iter_matches(joined, extras)
            if matches:
                edits: Dict[int, List[Tuple[int, int, str]]] = {}
                for start, end, kind, value in matches:
                    pieces = _range_pieces(cmap, start, end)
                    if not pieces:
                        continue
                    fake = masker.get(kind, value)
                    originals = [flat_spans[si]["text"][a:b] for si, a, b in pieces]
                    fakes = _distribute_fake(fake, originals)
                    for (si, a, b), fp in zip(pieces, fakes):
                        if fp is None:
                            continue
                        edits.setdefault(si, []).append((a, b, fp))
                    count += 1
                for si, items in edits.items():
                    span = flat_spans[si]
                    t = span["text"]
                    for a, b, fp in sorted(items, key=lambda x: -x[0]):
                        t = t[:a] + fp + t[b:]
                    if t != span["text"]:
                        span["_new_text"] = t
            # Помечаем все span строки, чтобы блочный проход не обработал их повторно.
            for _, inside in cell_spans:
                for span in inside:
                    span["_table_done"] = True
                    used.add(id(span))
    return count


def _mask_page_rows(page_spans: List[Dict[str, Any]],
                    span_block: Dict[int, int],
                    extras: Any, masker: Masker) -> int:
    """Страховка для таблиц без линий: склеивает рядом стоящие блоки одной строки.

    Обычный абзац целиком лежит в одном блоке и здесь пропускается — его
    обрабатывает _mask_block. Склеиваются только span из разных блоков,
    расположенные на одной высоте и с небольшим горизонтальным зазором.
    """
    import pymupdf
    items: List[Tuple[Dict[str, Any], Any, int]] = []
    for span in page_spans:
        if span.get("_drop") or span.get("_table_done") or span.get("_row_done"):
            continue
        try:
            rect = pymupdf.Rect(span["bbox"])
        except Exception:
            continue
        items.append((span, rect, span_block.get(id(span), -1)))
    if len(items) < 2:
        return 0
    items.sort(key=lambda x: (x[1].y0, x[1].x0))
    clusters: List[Dict[str, Any]] = []
    for item in items:
        placed = False
        for cluster in clusters:
            cr = cluster["rect"]
            overlap = min(cr.y1, item[1].y1) - max(cr.y0, item[1].y0)
            if overlap > 0.5 * min(cr.height, item[1].height):
                cluster["items"].append(item)
                cluster["rect"] = pymupdf.Rect(
                    min(cr.x0, item[1].x0), min(cr.y0, item[1].y0),
                    max(cr.x1, item[1].x1), max(cr.y1, item[1].y1))
                placed = True
                break
        if not placed:
            clusters.append({"items": [item], "rect": pymupdf.Rect(item[1])})
    count = 0
    for cluster in clusters:
        row = sorted(cluster["items"], key=lambda x: x[1].x0)
        runs: List[List[Tuple[Dict[str, Any], Any, int]]] = []
        cur: List[Tuple[Dict[str, Any], Any, int]] = []
        for item in row:
            if not cur:
                cur = [item]
                continue
            prev = cur[-1]
            gap = item[1].x0 - prev[1].x1
            size = max(float(prev[0].get("size") or 10.0),
                       float(item[0].get("size") or 10.0))
            if gap <= max(6.0, 3.0 * size):
                cur.append(item)
            else:
                runs.append(cur)
                cur = [item]
        if cur:
            runs.append(cur)
        for run in runs:
            if len(run) < 2:
                continue
            if len({b for _, _, b in run}) < 2:
                continue  # обычный абзац внутри одного блока
            chars: List[str] = []
            cmap: List[Any] = []
            flat_spans: List[Dict[str, Any]] = []
            last = ""
            for span, _, _ in run:
                text = span.get("text", "")
                if not text:
                    continue
                si = len(flat_spans)
                flat_spans.append(span)
                if (chars and last and not last.isspace()
                        and not text[0].isspace()
                        and last.isalnum() and text[0].isalnum()):
                    chars.append(" ")
                    cmap.append(None)
                for oi, ch in enumerate(text):
                    if ch in _ZERO_WIDTH:
                        continue
                    nc = _norm_char(ch)
                    chars.append(nc)
                    cmap.append((si, oi))
                    last = nc
            joined = "".join(chars)
            matches = iter_matches(joined, extras)
            if not matches:
                continue
            edits: Dict[int, List[Tuple[int, int, str]]] = {}
            for start, end, kind, value in matches:
                pieces = _range_pieces(cmap, start, end)
                if not pieces:
                    continue
                fake = masker.get(kind, value)
                originals = [flat_spans[si]["text"][a:b] for si, a, b in pieces]
                fakes = _distribute_fake(fake, originals)
                for (si, a, b), fp in zip(pieces, fakes):
                    if fp is None:
                        continue
                    edits.setdefault(si, []).append((a, b, fp))
                count += 1
            for si, items_ in edits.items():
                span = flat_spans[si]
                t = span["text"]
                for a, b, fp in sorted(items_, key=lambda x: -x[0]):
                    t = t[:a] + fp + t[b:]
                if t != span["text"]:
                    span["_new_text"] = t
            if edits:
                for span, _, _ in run:
                    span["_row_done"] = True
    return count


def _mask_block(block: Dict[str, Any], extras: Any, masker: Masker) -> int:
    """Меняет текст внутри блока, даже если сущность разбита на несколько span."""
    text, cmap, spans = _block_text(block)
    if not text.strip():
        return 0
    matches = iter_matches(text, extras)
    if not matches:
        return 0
    edits: Dict[int, List[Tuple[int, int, str]]] = {}
    merges: List[Tuple[List[Dict[str, Any]], str]] = []
    merged_ids: set[int] = set()
    count = 0
    for start, end, kind, value in matches:
        pieces = _range_pieces(cmap, start, end)
        if not pieces:
            continue
        fake = masker.get(kind, value)
        group = [spans[si] for si, _, _ in pieces]
        mergeable = (
            len(pieces) > 1
            and all(_span_fully_covered(spans[si]["text"], a, b) for si, a, b in pieces)
            and all(id(spans[si]) not in merged_ids for si, _, _ in pieces)
            and all(si not in edits for si, _, _ in pieces)
            and _spans_same_line(group)
        )
        if mergeable:
            # ФИО/организация разбиты на несколько span (жирный/курсив): рисуем
            # фейк одним куском, иначе короткий span сжимается до нечитаемого.
            merges.append((group, fake))
            merged_ids.update(id(s) for s in group)
        else:
            originals = [spans[si]["text"][a:b] for si, a, b in pieces]
            fakes = _distribute_fake(fake, originals)
            for (si, a, b), fp in zip(pieces, fakes):
                if fp is None:
                    continue
                edits.setdefault(si, []).append((a, b, fp))
        count += 1
    for si, items in edits.items():
        t = spans[si]["text"]
        for a, b, fp in sorted(items, key=lambda x: -x[0]):
            t = t[:a] + fp + t[b:]
        if t != spans[si]["text"]:
            spans[si]["_new_text"] = t
    for group, fake in merges:
        group[0]["_merge_spans"] = group
        group[0]["_merge_text"] = fake
        for span in group[1:]:
            span["_drop"] = True
    return count


def _scrub_pdf_annotations(page, extras: Any, masker: Masker) -> int:
    """Комментарии, подписи полей форм и ссылки тоже могут содержать ПДн."""
    masked = 0
    try:
        for annot in page.annots() or []:
            try:
                info = dict(annot.info or {})
                changed = False
                for key in ("content", "title", "subject"):
                    value = info.get(key) or ""
                    if not value:
                        continue
                    new = mask_text(value, extras, masker)
                    if new == value:
                        new = ""  # лучше потерять текст комментария, чем оставить ПДн
                    info[key] = new
                    changed = True
                if changed:
                    annot.set_info(info)
                    masked += 1
            except Exception:
                pass
    except Exception:
        pass
    try:
        for widget in page.widgets() or []:
            try:
                value = widget.field_value
                if isinstance(value, str) and value:
                    new = mask_text(value, extras, masker)
                    if new == value:
                        new = ""
                    widget.field_value = new
                    widget.update()
                    masked += 1
                label = getattr(widget, "field_label", None)
                if isinstance(label, str) and label:
                    new_label = mask_text(label, extras, masker)
                    if new_label == label:
                        new_label = ""
                    widget.field_label = new_label
                    widget.update()
                    masked += 1
            except Exception:
                pass
    except Exception:
        pass
    try:
        for link in page.get_links() or []:
            uri = link.get("uri")
            if isinstance(uri, str) and uri:
                kinds = {k for _, _, k, _ in iter_matches(uri, extras)}
                # Числа в URL не трогаем, иначе ссылка перестанет открываться.
                if kinds & {"email", "phone", "fio", "fio_lat", "org",
                            "address", "address_lat"}:
                    new = mask_text(uri, extras, masker)
                    if new != uri:
                        link["uri"] = new
                        page.update_link(link)
                        masked += 1
    except Exception:
        pass
    return masked


def _scrub_pdf_metadata(doc, extras: Any, masker: Masker) -> int:
    """Убирает ПДн из свойств документа, XMP и закладок."""
    masked = 0
    try:
        meta = doc.metadata or {}
        if any(meta.get(k) for k in ("title", "author", "subject", "keywords",
                                     "creator", "producer", "creationDate", "modDate")):
            masked += 1
        doc.set_metadata({
            "title": "",
            "author": "",
            "subject": "",
            "keywords": "",
            "creator": "PII Masker",
            "producer": "PII Masker",
            "creationDate": "",
            "modDate": "",
        })
    except Exception:
        pass
    try:
        doc.del_xml_metadata()
        masked += 1
    except Exception:
        try:
            doc.set_xml_metadata("")
            masked += 1
        except Exception:
            pass
    try:
        toc = doc.get_toc(simple=False)
        changed = False
        for item in toc:
            if len(item) >= 2 and item[1]:
                new_title = mask_text(str(item[1]), extras, masker)
                if new_title != item[1]:
                    item[1] = new_title
                    changed = True
                    masked += 1
        if changed:
            doc.set_toc(toc)
    except Exception:
        pass
    return masked


def mask_pdf_file(input_path: Path, output_path: Path, extras: Any = ()) -> Dict[str, Any]:
    """Заменяет ПДн в PDF: ищет сущности по всему блоку и перерисовывает затронутые span.

    Поиск идёт не по отдельному span, а по склеенному тексту блока/строки, поэтому
    ФИО и организации, разбитые жирным шрифтом или другим начертанием, тоже находятся.
    """
    import pymupdf
    doc = pymupdf.open(input_path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF защищён паролем")
    total = 0
    pages = len(doc)
    masker = Masker()
    for page in doc:
        page_dict = page.get_text("dict")
        blocks = page_dict.get("blocks", [])
        page_spans: List[Dict[str, Any]] = []
        span_block: Dict[int, int] = {}
        for bi, block in enumerate(blocks):
            for line in block.get("lines", []):
                rot = _rotation_from_dir(line.get("dir") or (1, 0))
                for span in line.get("spans", []):
                    span["_rot"] = rot
                    span_block[id(span)] = bi
                    if span.get("text", "").strip():
                        page_spans.append(span)
        tables = _page_tables(page)
        table_cells = _tables_to_cells(tables)
        if tables:
            total += _mask_table_rows(tables, page_spans, extras, masker)
        total += _mask_page_rows(page_spans, span_block, extras, masker)
        for block in blocks:
            total += _mask_block(block, extras, masker)
        jobs: List[Tuple[List[Any], Tuple[float, float], float, str,
                         Tuple[float, float, float], str | None, int]] = []
        consumed: set[int] = set()
        for span in page_spans:
            if span.get("_drop") or id(span) in consumed:
                continue
            group = span.get("_merge_spans")
            if group:
                rects = [pymupdf.Rect(s["bbox"]) for s in group]
                x0 = min(r.x0 for r in rects)
                x1 = max(r.x1 for r in rects)
                y0 = min(r.y0 for r in rects)
                y1 = max(r.y1 for r in rects)
                origin = span.get("origin") or (x0, y1)
                size = float(span.get("size") or 10.0)
                flags = int(span.get("flags") or 0)
                rot = int(span.get("_rot") or 0)
                path = _font_path(bool(flags & 16), bool(flags & 2))
                draw_text = str(span.get("_merge_text") or "")
                cell = _containing_cell(table_cells, pymupdf.Rect(x0, y0, x1, y1))
                fs = size
                if rot in (90, 270):
                    avail = max(12.0, y1 - y0 - 2.0)
                elif rot == 180:
                    avail = max(12.0, x1 - x0 - 2.0)
                else:
                    avail = max(12.0, x1 - x0 - 2.0)
                if cell is not None:
                    if rot in (90, 270):
                        avail = min(avail, max(6.0, cell.y1 - cell.y0 - 2.0))
                    else:
                        avail = min(avail, max(6.0, cell.x1 - cell.x0 - 2.0))
                if path:
                    font = pymupdf.Font(fontfile=path)
                    width = _text_width(font, draw_text, fs)
                    if width > avail > 0:
                        fs = max(4.0, fs * avail / width)
                else:
                    fs = max(4.0, min(11.0, size))
                    draw_text = MASK
                jobs.append((rects, (float(origin[0]), float(origin[1])), fs,
                             draw_text, _span_color(span), path, rot))
                consumed.update(id(s) for s in group)
                continue
            new_text = span.get("_new_text")
            if not new_text or new_text == span.get("text"):
                continue
            bbox = pymupdf.Rect(span["bbox"])
            origin = span.get("origin") or (bbox.x0, bbox.y1)
            size = float(span.get("size") or 10.0)
            rot = int(span.get("_rot") or 0)
            cell = _containing_cell(table_cells, bbox)
            limit = _neighbor_limit(page_spans, span, bbox, rot, page.rect)
            avail = _avail_width(rot, (float(origin[0]), float(origin[1])),
                                 limit, cell)
            flags = int(span.get("flags") or 0)
            path = _font_path(bool(flags & 16), bool(flags & 2))
            fs = size
            draw_text = new_text
            if path:
                font = pymupdf.Font(fontfile=path)
                width = _text_width(font, draw_text, fs)
                if width > avail > 0:
                    fs = max(3.5, fs * avail / width)
            else:
                fs = max(4.0, min(11.0, size))
                draw_text = MASK
            jobs.append(([bbox], (float(origin[0]), float(origin[1])), fs,
                         draw_text, _span_color(span), path, rot))
        if not jobs:
            total += _scrub_pdf_annotations(page, extras, masker)
            continue
        for rects, _, _, _, _, _, _ in jobs:
            for bbox in rects:
                page.add_redact_annot(bbox, fill=None)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        fontnames: Dict[str, str] = {}
        for path in {j[5] for j in jobs if j[5]}:
            fn = f"PF{len(fontnames)}"
            try:
                page.insert_font(fontname=fn, fontfile=path)
                fontnames[path] = fn
            except Exception:
                pass
        for _, origin, fs, draw_text, color, path, rot in jobs:
            if path and path in fontnames:
                page.insert_text(origin, draw_text, fontname=fontnames[path],
                                 fontsize=fs, color=color, rotate=rot)
            else:
                page.insert_text(origin, MASK, fontname="helv", fontsize=fs,
                                 color=color, rotate=rot)
        total += _scrub_pdf_annotations(page, extras, masker)
    total += _scrub_pdf_metadata(doc, extras, masker)
    doc.save(output_path, garbage=3, deflate=True)
    doc.close()
    return {"masked": total, "pages": pages}

def _excel_cell_text(v: Any) -> str | None:
    """Текст ячейки, включая rich text; None для чисел/дат/формул."""
    if isinstance(v, str):
        return v
    try:
        from openpyxl.cell.rich_text import CellRichText
        if isinstance(v, CellRichText):
            return "".join(getattr(block, "text", str(block)) for block in v)
    except Exception:
        pass
    return None


def _header_footer_parts(ws):
    for attr in ("oddHeader", "evenHeader", "firstHeader",
                 "oddFooter", "evenFooter", "firstFooter"):
        hf = getattr(ws, attr, None)
        if hf is None:
            continue
        for name in ("left", "center", "right"):
            part = getattr(hf, name, None)
            if part is not None:
                yield part


def _mask_formula_strings(formula: str, extras: Any, masker: Masker) -> str:
    """Маскирует ПДн в строковых литералах формул, не трогая ссылки и числа."""
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        new = mask_text(inner, extras, masker)
        return '"' + (new if new != inner else inner) + '"'
    try:
        return re.sub(r'"([^"]*)"', repl, formula)
    except Exception:
        return formula


def _to_number(text: str, as_int: bool):
    """Преобразует фейковую строку с любыми разделителями в число для ячейки."""
    s = str(text).replace("\u00a0", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    value = float(s)
    return int(round(value)) if as_int else value


def extract_excel_text(path: Path, limit: int = 30000) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts: List[str] = []
    size = 0
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                # Склеиваем ячейки строки через пробел: ФИО/организация могут быть
                # разбиты по колонкам, и LLM должна видеть их как одну сущность.
                vals: List[str] = []
                for v in row:
                    if v is None:
                        continue
                    s = _excel_cell_text(v)
                    if s is None:
                        s = str(v)
                    if s.strip():
                        vals.append(s.strip())
                if vals:
                    line = " ".join(vals)
                    parts.append(line)
                    size += len(line) + 1
                    if size >= limit:
                        return "\n".join(parts)
            try:
                for part in _header_footer_parts(ws):
                    text = getattr(part, "text", "") or ""
                    if text.strip():
                        parts.append(text)
                        size += len(text) + 1
            except Exception:
                pass
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


def _scrub_excel_properties(wb, masker: Masker, extras: Any) -> int:
    """Чистит свойства документа: автор, тема, описание и пользовательские поля."""
    masked = 0
    try:
        props = wb.properties
        for attr in ("creator", "lastModifiedBy"):
            if getattr(props, attr, None):
                setattr(props, attr, "PII Masker")
                masked += 1
        for attr in ("title", "subject", "description", "keywords", "category", "identifier"):
            if getattr(props, attr, None):
                setattr(props, attr, "")
                masked += 1
    except Exception:
        pass
    try:
        for prop in getattr(wb, "custom_doc_props", []) or []:
            value = getattr(prop, "value", None)
            if isinstance(value, str) and value:
                new = mask_text(value, extras, masker)
                if new != value:
                    prop.value = new
                    masked += 1
    except Exception:
        pass
    return masked


def mask_excel_file(input_path: Path, output_path: Path, extras: Any = ()) -> Dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(input_path)
    masker = Masker()
    masked = 0
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                # Комментарии и цели гиперссылок — отдельно от значений.
                for cell in row:
                    try:
                        comment = cell.comment
                        if comment is not None and comment.text:
                            new = mask_text(comment.text, extras, masker)
                            if new != comment.text:
                                comment.text = new
                                try:
                                    comment.author = "PII Masker"
                                except Exception:
                                    pass
                                masked += 1
                    except Exception:
                        pass
                    try:
                        link = cell.hyperlink
                        target = getattr(link, "target", None) if link is not None else None
                        if isinstance(target, str) and target:
                            kinds = {k for _, _, k, _ in iter_matches(target, extras)}
                            if kinds & {"email", "phone", "fio", "fio_lat", "org",
                                        "address", "address_lat"}:
                                new = mask_text(target, extras, masker)
                                if new != target:
                                    link.target = new
                                    masked += 1
                    except Exception:
                        pass
                entries: List[Any] = []
                for cell in row:
                    v = cell.value
                    if v is None or isinstance(v, bool):
                        entries.append(None)
                        continue
                    text = _excel_cell_text(v)
                    if text is not None:
                        if text and text.startswith("="):
                            # Формулу не ломаем, но строковые литералы внутри неё чистим.
                            new_formula = _mask_formula_strings(text, extras, masker)
                            if new_formula != text:
                                cell.value = new_formula
                                masked += 1
                            entries.append(None)
                        elif text:
                            entries.append((cell, text))
                        else:
                            entries.append(None)
                        continue
                    entries.append(None)
                    if isinstance(v, (datetime_cls, date_cls)):
                        cell.value = v + timedelta(days=random.randint(-3650, 3650))
                        masked += 1
                    elif isinstance(v, (int, float)):
                        raw = str(int(v)) if isinstance(v, int) else str(v)
                        kind = _infer_kind(raw)
                        if kind in ("inn", "ogrn", "ogrn_bare", "bik", "rs", "kpp", "snils"):
                            fake = masker.get(kind, raw)
                        else:
                            fake = masker.get("number", raw)
                        try:
                            new_value = _to_number(fake, isinstance(v, int))
                            if new_value != v:
                                cell.value = new_value
                                masked += 1
                        except Exception:
                            try:
                                if isinstance(v, int):
                                    cell.value = random.randint(
                                        1, max(1, 10 ** len(str(abs(v)))) - 1)
                                else:
                                    cell.value = round(
                                        random.uniform(0, max(1.0, abs(v) * 10)), 2)
                                masked += 1
                            except Exception:
                                pass
                # Склеиваем все непустые текстовые ячейки строки через пробел:
                # ФИО/организация могут быть разбиты по колонкам, в том числе
                # через пустые ячейки. Ложные срабатывания безопаснее пропуска ПДн.
                cells: List[Tuple[Any, str]] = []
                chars: List[str] = []
                cmap: List[Any] = []
                for entry in entries:
                    if entry is None:
                        continue
                    cell, value = entry
                    ci = len(cells)
                    cells.append((cell, value))
                    if chars:
                        chars.append(" ")
                        cmap.append(None)
                    for oi, ch in enumerate(value):
                        if ch in _ZERO_WIDTH:
                            continue
                        chars.append(_norm_char(ch))
                        cmap.append((ci, oi))
                if not cells:
                    continue
                joined = "".join(chars)
                matches = iter_matches(joined, extras)
                if not matches:
                    continue
                edits: Dict[int, List[Tuple[int, int, str]]] = {}
                for start, end, kind, value in matches:
                    pieces = _range_pieces(cmap, start, end)
                    if not pieces:
                        continue
                    fake = masker.get(kind, value)
                    originals = [cells[ci][1][a:b] for ci, a, b in pieces]
                    fakes = _distribute_fake(fake, originals)
                    for (ci, a, b), fp in zip(pieces, fakes):
                        if fp is None:
                            continue
                        edits.setdefault(ci, []).append((a, b, fp))
                for ci, items in edits.items():
                    cell, value = cells[ci]
                    t = value
                    for a, b, fp in sorted(items, key=lambda x: -x[0]):
                        t = t[:a] + fp + t[b:]
                    if t != value:
                        cell.value = t
                        try:
                            if cell.hyperlink is not None:
                                cell.hyperlink = None  # ссылка может содержать исходный email/URL
                        except Exception:
                            pass
                        masked += 1
            # Колонтитулы листа тоже могут содержать ФИО/название организации.
            for part in _header_footer_parts(ws):
                text = getattr(part, "text", "") or ""
                if text:
                    new = mask_text(text, extras, masker)
                    if new != text:
                        part.text = new
                        masked += 1
        masked += _scrub_excel_properties(wb, masker, extras)
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
    extras, llm_ok, llm_error = detect_pii_llm(text)
    extras = expand_variants(extras)
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
                              "extras": [e["text"] for e in extras[:80]], **stats}
    if warning:
        result["warning"] = warning
    if llm_error:
        result["llm_error"] = llm_error[:300]
    return result


def process_excel(input_path: Path, output_path: Path) -> Dict[str, Any]:
    text = extract_excel_text(input_path)
    extras, llm_ok, llm_error = detect_pii_llm(text)
    extras = expand_variants(extras)
    stats = mask_excel_file(input_path, output_path, extras)
    result: Dict[str, Any] = {"preview_type": "excel", "preview": stats.pop("preview_rows", []),
                              "llm_used": llm_ok,
                              "extras": [e["text"] for e in extras[:80]], **stats}
    if llm_error:
        result["llm_error"] = llm_error[:300]
    return result


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