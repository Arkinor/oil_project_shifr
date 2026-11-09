"""
main.py — веб-сервис обезличивания PDF/Excel с локальной LLM (LM Studio).

Запуск:  python main.py
Сборка:  build.bat  ->  dist/PIIMasker.exe
"""
import hashlib
import json
import logging
import re
import socket
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

DEFAULT_SETTINGS: Dict[str, Any] = {
    "lm_studio_url": "http://127.0.0.1:1234/v1",
    "model": "google/gemma-4-e2b",
    "api_key": "lm-studio",
    "temperature": 0.0,
    "max_tokens": 1024,
    "request_timeout": 90,
    "max_llm_chars": 12000,
    "host": "0.0.0.0",
    "port": 9321,
    "open_browser": True,
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
    return any(u.get("login") == login and u.get("password") == password for u in load_users())


def get_current_user(request: Request) -> str:
    login = request.headers.get("X-Login", "")
    password = request.headers.get("X-Password", "")
    if not login or not password or not authenticate(login, password):
        raise HTTPException(401, "Требуется авторизация")
    return login


# ---------------------------------------------------------------- PII-правила
MASK = "***"

# (имя, regex). Всё совпадение заменяется на ***.
PLAIN_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")),
    ("phone", re.compile(r"(?<!\d)(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")),
    ("snils", re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{3}[-\s]\d{2}(?!\d)")),
    ("snils_plain", re.compile(r"(?<!\d)\d{11}(?!\d)")),
    ("passport", re.compile(r"(?<!\d)\d{2}\s+\d{2}\s+№?\s*\d{6}(?!\d)")),
    ("passport", re.compile(r"(?<!\d)\d{4}\s+№\s*\d{6}(?!\d)")),
    ("account", re.compile(r"(?<!\d)\d{20}(?!\d)")),
    ("ogrn", re.compile(r"(?<!\d)(?:\d{13}|\d{15})(?!\d)")),
    ("inn", re.compile(r"(?<!\d)(?:\d{10}|\d{12})(?!\d)")),
    ("kpp", re.compile(r"(?<!\d)\d{4}[0-9A-ZА-Я]{2}\d{3}(?!\d)")),
    ("bik", re.compile(r"(?<!\d)\d{9}(?!\d)")),
    ("fio", re.compile(r"(?<![А-Яа-яЁё])(?:[А-ЯЁ][а-яё]{1,20}\s+){2}[А-ЯЁ][а-яё]{1,20}(?![А-Яа-яЁё])")),
    ("fio_initials", re.compile(r"(?<![А-Яа-яЁё])[А-ЯЁ][а-яё]{1,20}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.(?![А-Яа-яЁё])")),
    ("org", re.compile(
        r"(?<![А-Яа-яЁё])(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|ГУП|МУП|ГБУ|ГАУ|АНО|НКО|Фонд|"
        r"Министерство|Департамент|Управление|Компания)\s+[«\"]?[А-ЯЁA-Z0-9][^\n,;]{1,100}", re.I)),
    ("address", re.compile(
        r"(?<![А-Яа-яЁё])(?:г\.|город|ул\.|улица|пр-т|проспект|пер\.|переулок|наб\.|набережная|"
        r"ш\.|шоссе|д\.|дом|кв\.|квартира|обл\.|область|респ\.|республика|п\.|пос\.|поселок|"
        r"с\.|село|мкр\.|микрорайон)\s*[^\n,;]{1,80}(?:,\s*[^\n;]{1,80}){0,4}", re.I)),
]

# (regex, номер группы): заменяется только группа, префикс-метка сохраняется.
GROUP_PATTERNS: List[Tuple[re.Pattern, int]] = [
    (re.compile(r"((?:паспорт|серия|серии)\s*[:\-]?\s*)"
                r"(\d{2}\s?\d{2}\s*(?:№|номер)?\s*\d{6})", re.I), 2),
    (re.compile(r"((?:СНИЛС|страховой номер)\s*[:\-]?\s*)"
                r"(\d{3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{2})", re.I), 2),
    (re.compile(r"((?:ФИО|Ф\.И\.О\.|Сотрудник|Работник|Владелец|Директор|Главный бухгалтер)\s*[:\-]?\s*)"
                r"([А-ЯЁ][^\n;]{2,120})", re.I), 2),
    (re.compile(r"((?:Организация|Наименование|Работодатель|Компания)\s*[:\-]?\s*)"
                r"([^\n;]{2,150})", re.I), 2),
    (re.compile(r"((?:Адрес|Место жительства|Место нахождения|Почтовый адрес)\s*[:\-]?\s*)"
                r"([^\n;]{5,200})", re.I), 2),
]


def _clean_value(v: str) -> str:
    v = str(v).replace("\u00ad", "-").replace("\xa0", " ").replace("\u2011", "-")
    return v.strip(" \t\r\n,;.\"'«»")


def _sub_group(m: re.Match, group: int) -> str:
    start, end = m.span(group)
    whole = m.group(0)
    return whole[: start - m.start()] + MASK + whole[end - m.start():]


def mask_text(text: Any, extras: Any = ()) -> Any:
    if not isinstance(text, str) or not text:
        return text
    for v in sorted({_clean_value(x) for x in extras if len(_clean_value(x)) >= 3}, key=len, reverse=True):
        if v and v != MASK:
            text = re.sub(re.escape(v), MASK, text, flags=re.I)
    for rx, group in GROUP_PATTERNS:
        text = rx.sub(lambda m: _sub_group(m, group), text)
    for _, rx in PLAIN_PATTERNS:
        text = rx.sub(MASK, text)
    return text


def find_pii(text: str, extras: Any = ()) -> List[str]:
    text = (text or "").replace("\u00ad", "-").replace("\xa0", " ")
    values = set()
    for x in extras:
        v = _clean_value(x)
        if len(v) >= 3 and v != MASK:
            values.add(v)
    for rx, group in GROUP_PATTERNS:
        for m in rx.finditer(text):
            v = _clean_value(m.group(group))
            if len(v) >= 3:
                values.add(v)
    for _, rx in PLAIN_PATTERNS:
        for m in rx.finditer(text):
            v = m.group(0).strip()
            if v:
                values.add(v)
    return sorted(values, key=len, reverse=True)


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
    "Также искать значение после меток «ФИО», «Сотрудник», «Владелец», «Директор».",
    "Организации — названия после ООО, АО, ПАО, ЗАО, ИП, ГУП, МУП, ГБУ, АНО, НКО, Фонд, "
    "а также после меток «Организация», «Наименование», «Работодатель».",
    "Адреса — строки после «Адрес», «Место жительства», «Место нахождения», а также конструкции "
    "с «г.», «ул.», «д.», «кв.», «обл.», «пр-т», «ш.», «мкр.». Маскировать адрес целиком.",
    "Все найденные значения заменяются ровно на три звёздочки ***. Структура документа сохраняется: "
    "таблицы, страницы, шрифты и расположение текста не меняются.",
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
    # запасной вариант: строки в кавычках или маркированный список
    quoted = re.findall(r'"([^"\n]{3,200})"', raw)
    if quoted:
        return quoted
    return [ln.strip(" -•\t") for ln in raw.splitlines() if 3 <= len(ln.strip(" -•\t")) <= 200]


def detect_extra_pii(text: str) -> Tuple[List[str], bool]:
    """LLM ищет неструктурированные ПДн (ФИО, организации, адреса)."""
    s = load_settings()
    llm = get_llm(s)
    text = (text or "").strip()
    if llm is None or not text:
        return [], False
    context = rag_search("ФИО организации адреса правила маскировки")
    limit = int(s.get("max_llm_chars", 12000))
    chunks = [text[i:i + 6000] for i in range(0, min(len(text), limit), 6000)]
    text_norm = text.replace("\u00ad", "-").replace("\xa0", " ").lower()
    found: set[str] = set()
    ok = False
    system = ("Ты — анализатор персональных данных. Отвечай только JSON-массивом строк, без пояснений. "
              "Из текста выпиши дословно: ФИО, полные названия организаций, почтовые адреса. "
              "Не выписывай ИНН, КПП, ОГРН, СНИЛС, паспорт, телефоны, email, счета и БИК — их обрабатывает программа. "
              "Если ничего нет — верни []. Не придумывай значения.")
    for chunk in chunks:
        prompt = (f"Правила:\n{context}\n\nТекст:\n{chunk}\n\n"
                  "Верни только JSON-массив найденных строк. Пример: [\"Иванов Иван Иванович\", \"ООО Ромашка\"]")
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


try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover
    def tool(fn):
        return fn


@tool
def search_rules(query: str) -> str:
    """Найти в RAG-базе знаний правила и шаблоны поиска персональных данных."""
    return rag_search(query) or "Правила не найдены."


@tool
def detect_pii(text: str) -> str:
    """Найти ФИО, названия организаций и адреса в тексте локальной LLM. Возвращает JSON-массив строк."""
    values, _ = detect_extra_pii(text)
    return json.dumps(values, ensure_ascii=False)


@tool
def mask_text_tool(text: str, extra_values: str = "[]") -> str:
    """Замаскировать ПДн в тексте на ***. extra_values — JSON-массив дополнительных значений."""
    try:
        extras = json.loads(extra_values) if extra_values else []
    except Exception:
        extras = []
    return mask_text(text, extras)


@tool
def extract_pdf_text_tool(path: str) -> str:
    """Извлечь весь текст из PDF-файла по указанному пути."""
    return extract_pdf_text(Path(path))


@tool
def extract_excel_text_tool(path: str) -> str:
    """Извлечь текст из Excel-файла (.xlsx) по указанному пути."""
    return extract_excel_text(Path(path))


@tool
def mask_pdf_tool(path: str, out_path: str, extra_values: str = "[]") -> str:
    """Обезличить PDF: заменить ПДн на ***, сохранив страницы и расположение текста."""
    try:
        extras = json.loads(extra_values) if extra_values else []
    except Exception:
        extras = []
    r = mask_pdf_file(Path(path), Path(out_path), extras)
    return json.dumps(r, ensure_ascii=False)


@tool
def mask_excel_tool(path: str, out_path: str, extra_values: str = "[]") -> str:
    """Обезличить Excel (.xlsx): заменить ПДн в ячейках на ***, сохранив структуру книги."""
    try:
        extras = json.loads(extra_values) if extra_values else []
    except Exception:
        extras = []
    r = mask_excel_file(Path(path), Path(out_path), extras)
    return json.dumps(r, ensure_ascii=False)


TOOLS = [search_rules, detect_pii, mask_text_tool, extract_pdf_text_tool,
         extract_excel_text_tool, mask_pdf_tool, mask_excel_tool]


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


def _pdf_search_forms(value: str) -> List[str]:
    base = value.replace("\u00ad", "").replace("\xa0", " ").replace("\u2011", "-")
    forms = [
        value, base, " ".join(base.split()), base.replace(" ", ""), base.replace("-", " "),
        base.replace("-", "\u00ad"), base.replace(" ", "\xa0"),
        base.replace("-", "\u00ad").replace(" ", "\xa0"),
    ]
    return [f for f in dict.fromkeys(forms) if len(f) >= 3]


def mask_pdf_file(input_path: Path, output_path: Path, extras: Any = ()) -> Dict[str, Any]:
    import pymupdf
    doc = pymupdf.open(input_path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF защищён паролем")
    total = 0
    pages = len(doc)
    for page in doc:
        page_text = page.get_text("text")
        seen: set[Tuple[float, float, float, float]] = set()
        for value in find_pii(page_text, extras):
            rects = []
            for form in _pdf_search_forms(value):
                try:
                    rects = page.search_for(form)
                except Exception:
                    rects = []
                if rects:
                    break
            for rect in rects:
                key = (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                if key in seen:
                    continue
                seen.add(key)
                fs = max(5, min(11, rect.height * 0.8))
                page.add_redact_annot(rect, text=MASK, fontsize=fs,
                                      fill=(1, 1, 1), text_color=(0, 0, 0))
                total += 1
        page.apply_redactions(images=getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0))
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


def mask_excel_file(input_path: Path, output_path: Path, extras: Any = ()) -> Dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(input_path)
    masked = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v and not v.startswith("="):
                    nv = mask_text(v, extras)
                    if nv != v:
                        cell.value = nv
                        masked += 1
    wb.save(output_path)
    # предпросмотр первого листа
    preview: List[List[str]] = []
    try:
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 8:
                break
            preview.append([("" if c is None else str(c))[:60] for c in row[:8]])
    except Exception:
        pass
    wb.close()
    return {"masked": masked, "pages": 1, "preview_rows": preview}


def process_pdf(input_path: Path, output_path: Path) -> Dict[str, Any]:
    text = extract_pdf_text(input_path)
    extras, llm_ok = detect_extra_pii(text)
    stats = mask_pdf_file(input_path, output_path, extras)
    preview = ""
    try:
        import pymupdf
        doc = pymupdf.open(output_path)
        if len(doc):
            preview = doc[0].get_text("text")[:1800]
        doc.close()
    except Exception:
        pass
    return {"preview_type": "pdf", "preview": preview, "llm_used": llm_ok,
            "extras": extras[:50], **stats}


def process_excel(input_path: Path, output_path: Path) -> Dict[str, Any]:
    text = extract_excel_text(input_path)
    extras, llm_ok = detect_extra_pii(text)
    stats = mask_excel_file(input_path, output_path, extras)
    return {"preview_type": "excel", "preview": stats.pop("preview_rows", []),
            "llm_used": llm_ok, "extras": extras[:50], **stats}


# ---------------------------------------------------------------- LangChain-агент
_AGENT: Dict[str, Any] = {"key": None, "agent": None}


def get_agent():
    s = load_settings()
    key = (s["lm_studio_url"], s["model"])
    if _AGENT["key"] != key:
        agent = None
        llm = get_llm(s)
        if llm is not None:
            try:
                from langchain.agents import create_agent
                agent = create_agent(llm, TOOLS, system_prompt=(
                    "Ты — помощник по обезличиванию документов. Используй инструменты для поиска правил, "
                    "извлечения текста и маскировки PDF/Excel. Все ПДн заменяются на ***. Отвечай кратко."))
            except Exception as e:
                logger.warning(f"agent init: {e}")
        _AGENT.update(key=key, agent=agent)
    return _AGENT["agent"]


def run_agent(question: str) -> str:
    agent = get_agent()
    if agent is not None:
        try:
            res = agent.invoke({"messages": [{"role": "user", "content": question}]})
            msgs = res.get("messages") if isinstance(res, dict) else None
            if msgs:
                content = msgs[-1].content
                if isinstance(content, list):
                    return "".join(x.get("text", "") for x in content if isinstance(x, dict))
                return str(content)
        except Exception as e:
            logger.warning(f"agent run: {e}")
    llm = get_llm()
    if llm is not None:
        try:
            return str(llm.invoke(question).content)
        except Exception as e:
            logger.warning(f"llm fallback: {e}")
    return "LM Studio недоступен. Проверьте settings.json и запущенную модель."


# ---------------------------------------------------------------- FastAPI
app = FastAPI(title="PII Masker + RAG")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class LoginRequest(BaseModel):
    login: str
    password: str


class ChatRequest(BaseModel):
    message: str


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
    return {"ok": True, "model": s["model"], "lm_studio_url": s["lm_studio_url"],
            "llm_ok": check_llm(s), "rag": RETRIEVER is not None, "tools": len(TOOLS)}


@app.get("/api/tools")
def tools(user: str = Depends(get_current_user)):
    return {"tools": [{"name": t.name, "description": (t.description or "").strip()} for t in TOOLS]}


@app.post("/api/chat")
def chat(req: ChatRequest, user: str = Depends(get_current_user)):
    return {"answer": run_agent(req.message)}


@app.post("/api/process")
def process(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    original = Path(file.filename or "document").name
    ext = Path(original).suffix.lower()
    if ext not in (".pdf", ".xlsx"):
        raise HTTPException(400, "Поддерживаются только PDF и Excel (.xlsx)")
    content = file.file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")

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
    path = OUTPUT_DIR / name
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    media = ("application/pdf" if name.lower().endswith(".pdf")
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    logger.info(f"DOWNLOAD | {user} | {name}")
    return FileResponse(path, media_type=media, filename=name)


# ---------------------------------------------------------------- запуск
class _NoConsoleStream:
    def write(self, *args, **kwargs):
        return 0

    def flush(self, *args, **kwargs):
        return 0

    def isatty(self) -> bool:
        return False


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_server():
    ensure_settings()
    ensure_users_file()
    build_rag()
    if sys.stdout is None:
        sys.stdout = _NoConsoleStream()
    if sys.stderr is None:
        sys.stderr = _NoConsoleStream()
    s = load_settings()
    port = int(s.get("port", 9321))
    host = str(s.get("host", "0.0.0.0"))
    print(f"Сайт: http://127.0.0.1:{port}  |  в сети: http://{_lan_ip()}:{port}")
    if s.get("open_browser", True):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
