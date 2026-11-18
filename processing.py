"""processing.py — задания, обезличивание и запись результата в файлы.

Весь текст документа отправляется в LM Studio по частям с картой мест, модель
возвращает пары original/replacement, а замены применяются к исходной разметке
PDF, Word, Excel и изображений.
"""
import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import date as date_cls, datetime as datetime_cls
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

from config import (IMAGE_EXTS, OUTPUT_DIR, STATE_DIR, UPLOAD_DIR,
                    load_settings, logger)


# ================================================================
# 1. ЗАДАНИЯ И СОСТОЯНИЕ НА ДИСКЕ
# ================================================================
# LM Studio обрабатывает один документ за раз: очередь из интерфейса отправляет
# файлы последовательно, а этот замок страхует от параллельных запросов из
# нескольких вкладок/клиентов.
PROCESS_LOCK = threading.Lock()

# Задания живут в памяти процесса, а их состояние дублируется в
# data/state/<user>/jobs/<job_id>.json — чтобы история и прогресс переживали
# перезагрузку страницы и перезапуск сервера.
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.RLock()
JOBS_FLUSH_SECONDS = 2.0
JOB_RETENTION_SECONDS = 30 * 24 * 60 * 60
_ACTIVE_STATUSES = ("queued", "processing")


def _user_tag(login: str) -> str:
    """Безопасное имя папки пользователя: логин + короткий хэш от коллизий."""
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", str(login or "user"))[:64] or "user"
    digest = hashlib.sha1(str(login or "").encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{digest}"


def _user_dir(base: Path, login: str) -> Path:
    path = base / _user_tag(login)
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_uploads(login: str) -> Path:
    return _user_dir(UPLOAD_DIR, login)


def user_outputs(login: str) -> Path:
    return _user_dir(OUTPUT_DIR, login)


def user_jobs_dir(login: str) -> Path:
    return _user_dir(STATE_DIR, login) / "jobs"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"job save {path.name}: {e}")


def _job_file(job: Dict[str, Any]) -> Path:
    return user_jobs_dir(str(job.get("user") or "")) / f"{job.get('id')}.json"


def _save_job(job: Dict[str, Any]) -> None:
    data = {k: v for k, v in job.items() if not k.startswith("_")}
    _atomic_write_json(_job_file(job), {"version": 1, **data})


def _update_job(job_id: str, **fields: Any) -> None:
    """Обновляет задание и пишет его на диск (прогресс — не чаще JOBS_FLUSH_SECONDS)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        # Смена статуса, этапа, ошибки и финал пишутся сразу; проценты — по таймеру.
        stage_changed = "stage" in fields and fields["stage"] != job.get("stage")
        force = stage_changed or any(
            k in fields for k in ("status", "error", "result", "finished"))
        job.update(fields)
        now = time.time()
        if not force and now - float(job.get("_flushed") or 0) < JOBS_FLUSH_SECONDS:
            return
        job["_flushed"] = now
        snapshot = {k: v for k, v in job.items() if not k.startswith("_")}
    _atomic_write_json(_job_file(snapshot), {"version": 1, **snapshot})


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    data = {
        "id": job.get("id"),
        "filename": job.get("filename"),
        "size": job.get("size"),
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "stage": job.get("stage", ""),
        "elapsed": job.get("elapsed"),
        "error": job.get("error", ""),
        "created": job.get("created"),
    }
    if job.get("status") == "done" and isinstance(job.get("result"), dict):
        data.update(job["result"])
        data["download_name"] = job.get("download_name", "")
        data["download_url"] = "/api/download/" + urllib.parse.quote(
            str(job.get("out_name") or ""))
        data["filename"] = job.get("filename")
    return data


def _list_jobs(login: str) -> List[Dict[str, Any]]:
    with JOBS_LOCK:
        items = [_public_job(job) for job in JOBS.values() if job.get("user") == login]
    items.sort(key=lambda item: item.get("created") or 0)
    return items


def _find_job(login: str, job_id: str) -> Dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("user") != login:
            return None
        return _public_job(job)


def _owns_output(login: str, name: str) -> bool:
    with JOBS_LOCK:
        return any(job.get("user") == login and job.get("out_name") == name
                   for job in JOBS.values())


def _load_jobs() -> None:
    for path in STATE_DIR.glob("*/jobs/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            data.pop("version", None)
            job_id = path.stem
            login = str(data.get("user") or "")
            if not login:
                continue
            data["id"] = job_id
            data["user"] = login
            JOBS[job_id] = data
        except Exception as e:
            logger.warning(f"job file {path.name}: {e}")


def recover_state() -> None:
    """Загружает историю, помечает прерванные задания, чистит исходники."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _load_jobs()
    now = time.time()
    interrupted = 0
    for job in list(JOBS.values()):
        if job.get("status") not in _ACTIVE_STATUSES:
            continue
        job.update(status="error", progress=0, stage="Ошибка", finished=now,
                   error="Сервер был перезапущен — обработка прервана")
        _save_job(job)
        interrupted += 1
    try:
        for folder in UPLOAD_DIR.iterdir():
            if not folder.is_dir():
                try:
                    folder.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            for item in folder.iterdir():
                try:
                    item.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        pass
    prune_history()
    logger.info(f"История загружена: {len(JOBS)} заданий"
                + (f", прервано: {interrupted}" if interrupted else ""))


def prune_history() -> None:
    """Удаляет завершённые задания и их файлы старше JOB_RETENTION_SECONDS."""
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with JOBS_LOCK:
        expired = [job for job in JOBS.values()
                   if job.get("status") not in _ACTIVE_STATUSES
                   and float(job.get("finished") or 0) < cutoff]
        for job in expired:
            JOBS.pop(str(job.get("id")), None)
    for job in expired:
        try:
            _job_file(job).unlink(missing_ok=True)
        except Exception:
            pass
        out_name = job.get("out_name")
        if out_name:
            try:
                (user_outputs(str(job.get("user") or "")) / Path(out_name).name
                 ).unlink(missing_ok=True)
            except Exception:
                pass


def _run_job(job_id: str, in_path: Path, out_path: Path, ext: str) -> None:
    started = time.time()
    with PROCESS_LOCK:
        _update_job(job_id, status="processing", progress=1,
                    stage="Подготовка документа", started=started)

        def report(progress: int, stage: str) -> None:
            _update_job(job_id, progress=max(0, min(99, int(progress))),
                        stage=stage, elapsed=round(time.time() - started, 1))

        try:
            logger.info(f"PROCESS_START | job={job_id} | {in_path.name} | ext={ext}")
            if ext == ".pdf":
                result = process_pdf(in_path, out_path, progress=report)
            elif ext in IMAGE_EXTS:
                result = process_image(in_path, out_path, progress=report)
            elif ext in (".docx", ".doc"):
                result = (process_doc if ext == ".doc" else process_docx)(
                    in_path, out_path, progress=report)
            else:
                result = process_excel(in_path, out_path, progress=report)
            logger.info(f"PROCESS_DONE | job={job_id} | {in_path.name} | "
                        f"{round(time.time() - started, 1)} s | "
                        f"masked={result.get('masked')} | llm_used={result.get('llm_used')}")
        except Exception as e:
            logger.error(f"PROCESS_ERROR | job={job_id} | {in_path.name} | {e}")
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            _update_job(job_id, status="error", error=str(e), progress=0,
                        stage="Ошибка", finished=time.time(),
                        elapsed=round(time.time() - started, 1))
        else:
            _update_job(job_id, status="done", progress=100, stage="Готово",
                        result=result, finished=time.time(),
                        elapsed=round(time.time() - started, 1))
        finally:
            try:
                in_path.unlink(missing_ok=True)
            except Exception:
                pass


# ================================================================
# 2. ТЕКСТ: НОРМАЛИЗАЦИЯ, ПОИСК, ЗАМЕНА
# ================================================================
MASK = "***"
_ZERO_WIDTH = "\u00ad\u200b\u200c\u200d\ufeff"


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


def _clean_value(v: Any) -> str:
    """Убирает невидимые символы, лишние пробелы и парные обрамляющие кавычки."""
    v = str(v or "")
    for ch in _ZERO_WIDTH:
        v = v.replace(ch, "")
    v = v.replace("\xa0", " ").replace("\u2011", "-")
    v = v.strip(" \t\r\n,;")
    for left, right in (("«", "»"), ('"', '"'), ("'", "'")):
        if len(v) >= 2 and v.startswith(left) and v.endswith(right):
            v = v[1:-1].strip()
            break
    return re.sub(r"\s+", " ", v).strip()


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
    # Кавычки могут стоять вокруг всего значения или каждого слова
    # («ООО «Ромашка»» в тексте против «ООО Ромашка» в ответе модели).
    wrapped = [r'["«»„“”]?' + part + r'[»"]?' for part in parts]
    return r"(?<!\w)" + r"\s+".join(wrapped) + r"(?!\w)"


def _find_occurrences(text: str, value: str) -> List[re.Match]:
    pat = _flex_pattern(value)
    if not pat:
        return []
    try:
        return list(re.finditer(pat, text, re.I))
    except re.error:
        return []


@lru_cache(maxsize=4096)
def _prefilter_needle(value: str) -> str:
    """Самое длинное слово значения — быстрый отсев перед регуляркой."""
    tokens = re.findall(r"[0-9a-zа-я]+", _clean_value(value).lower().replace("ё", "е"))
    if not tokens:
        return ""
    needle = max(tokens, key=len)
    return needle if len(needle) >= 3 else ""


def _prefilter_text(text: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", text.lower().replace("ё", "е"))


def _is_bad_value(v: str) -> bool:
    """Механическая проверка пригодности строки для поиска. Смысл значений решает модель."""
    v = str(v or "").strip()
    if len(v) < 2 or len(v) > 300 or v == MASK:
        return True
    return len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]", v)) < 2


def _apply_case(fake: str, original: str) -> str:
    letters = [c for c in original if c.isalpha()]
    if not letters:
        return fake
    if all(c.isupper() for c in letters):
        return fake.upper()
    if all(c.islower() for c in letters):
        return fake.lower()
    return fake


_QUOTE_PAIRS = (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'"))


def _is_quoted(value: str) -> bool:
    return len(value) >= 2 and any(
        value.startswith(left) and value.endswith(right) for left, right in _QUOTE_PAIRS)


def _match_quotes(fake: str, original: str) -> str:
    """Сохраняет кавычки исходника: «Ромашка» -> «Лютик»."""
    fake = str(fake or "")
    original = str(original or "").strip()
    if not fake or _is_quoted(fake):
        return fake
    for left, right in _QUOTE_PAIRS:
        if original.startswith(left) and original.endswith(right) and len(original) >= 2:
            return f"{left}{fake}{right}"
    return fake


class Masker:
    """Хранит замены, полученные от модели, и отдаёт их с учётом регистра и кавычек.

    Ничего не придумывает: если модель не дала замену, текст остаётся как был.
    """

    def __init__(self) -> None:
        self.exact: Dict[str, str] = {}

    @staticmethod
    def _key(original: str) -> str:
        return _clean_value(original).lower()

    def set_exact(self, original: str, fake: str) -> None:
        original = str(original or "").strip()
        fake = str(fake or "").strip()
        if not original or not fake:
            return
        for key in (original.lower(), self._key(original),
                    original.strip(" «»\"'“”").lower()):
            if key:
                self.exact.setdefault(key, fake)

    def lookup(self, original: str) -> str:
        original = str(original or "")
        for key in (original.strip().lower(), self._key(original),
                    original.strip(" «»\"'“”").lower()):
            fake = self.exact.get(key)
            if fake:
                return fake
        return ""

    def get(self, kind: str, original: str) -> str:
        fake = self.lookup(original)
        if not fake:
            return str(original or "")
        if re.search(r"[А-Яа-яЁёA-Za-z]", original):
            return _match_quotes(_apply_case(fake, original), original)
        return fake


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


def iter_matches(text: str, extras: Any = ()) -> List[Tuple[int, int, str, str]]:
    """Возвращает совпадения (start, end, тип, значение) по списку замен LLM.

    Никаких регулярных шаблонов и словарей предметной области: что менять,
    решает модель. Программа лишь находит указанные ею строки в тексте.
    """
    if not isinstance(text, str) or not text:
        return []
    norm, idx = _normalize_with_map(text)
    haystack = _prefilter_text(norm)
    cands: List[Tuple[int, int, str, str]] = []
    for value, raw_kind in _iter_extras(extras):
        needle = _prefilter_needle(value)
        if needle and needle not in haystack:
            continue
        kind = str(raw_kind or "").strip().lower()
        for m in _find_occurrences(norm, value):
            # Берём реальный срез текста: в нём могут быть кавычки/метка,
            # которые Masker вернёт в замену (_match_quotes), а точную пару
            # он найдёт по очищенному ключу.
            cands.append((m.start(), m.end(), kind, m.group(0)))
    # Одинаковые диапазоны схлопываем; при пересечении побеждает более длинное
    # значение: «Иванов Иван Иванович» важнее отдельной «Иванов».
    best: Dict[Tuple[int, int], Tuple[int, int, str, str]] = {}
    for c in cands:
        best.setdefault((c[0], c[1]), c)
    selected: List[Tuple[int, int, str, str]] = []
    for c in sorted(best.values(), key=lambda x: (-(x[1] - x[0]), x[0])):
        if all(c[1] <= s or c[0] >= e for s, e, _, _ in selected):
            selected.append(c)
    selected.sort(key=lambda x: x[0])
    result: List[Tuple[int, int, str, str]] = []
    for s, e, kind, value in selected:
        if s >= len(idx) or e - 1 >= len(idx):
            continue
        result.append((idx[s], idx[e - 1] + 1, kind, value))
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


def _distribute_replacement(fake: str, pieces: List[str]) -> List[str]:
    """Распределяет одну замену по кускам, разбитым на несколько span/ячеек.

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

# ================================================================
# 3. LM STUDIO: ЧАНКИ, ЗАПРОС, РАЗБОР ОТВЕТА
# ================================================================
# Весь текст документа уходит в модель порциями. Каждая порция помнит, из
# какого места документа она взята (страница/лист/часть), поэтому замены
# потом применяются к исходной разметке, а не к переписанному тексту.
CHUNK_OVERLAP = 400
_JSON_MODE_SUPPORTED = True


def get_llm(settings: Dict[str, Any] | None = None, json_mode: bool = False):
    s = settings or load_settings()
    kwargs: Dict[str, Any] = {}
    if json_mode and _JSON_MODE_SUPPORTED:
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            base_url=s["lm_studio_url"], api_key=s.get("api_key", "lm-studio"),
            model=s["model"], temperature=float(s.get("temperature", 0)),
            timeout=float(s.get("request_timeout", 120)), max_retries=0,
            max_tokens=int(s.get("max_tokens", 4096)), **kwargs)
    except Exception as e:
        logger.error(f"LLM init: {e}")
        return None


def _lm_base_url(settings: Dict[str, Any]) -> str:
    url = str(settings.get("lm_studio_url") or "").rstrip("/")
    return re.sub(r"/v1$", "", url)


def _lm_models(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = _lm_base_url(settings)
    headers = {"Authorization": f"Bearer {settings.get('api_key', 'lm-studio')}"}
    for path in ("/api/v0/models", "/v1/models", "/models"):
        try:
            req = urllib.request.Request(base + path, headers=headers)
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
            items = data.get("data") if isinstance(data, dict) else data
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
        except Exception:
            continue
    return []


def model_state(settings: Dict[str, Any] | None = None) -> Tuple[str, str]:
    """(state, type) настроенной модели: loaded / not-loaded / unknown."""
    s = settings or load_settings()
    wanted = str(s.get("model") or "").lower()
    for item in _lm_models(s):
        if str(item.get("id") or "").lower() == wanted:
            return str(item.get("state") or "unknown"), str(item.get("type") or "")
    return "unknown", ""


def check_llm(settings: Dict[str, Any] | None = None) -> bool:
    """Сервер доступен и модель есть в списке. Загрузку LM Studio сделает сама (JIT)."""
    s = settings or load_settings()
    items = _lm_models(s)
    if not items:
        logger.warning(f"LLM недоступна: {s.get('lm_studio_url')}")
        return False
    wanted = str(s.get("model") or "").lower()
    if wanted and not any(str(i.get("id") or "").lower() == wanted for i in items):
        logger.error(f"LLM: модель {s.get('model')} не найдена в LM Studio")
        return False
    return True


def _try_lm_load(settings: Dict[str, Any]) -> None:
    """Best-effort загрузка модели, если JIT в LM Studio выключен."""
    base = _lm_base_url(settings)
    model = str(settings.get("model") or "")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {settings.get('api_key', 'lm-studio')}"}
    for url, body in (
        (f"{base}/api/v0/models/{urllib.parse.quote(model, safe='')}/load", {}),
        (f"{base}/api/v0/models/load", {"model": model}),
    ):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                if 200 <= r.status < 300:
                    logger.info(f"LLM: отправлена команда загрузки модели {model}")
                    return
        except Exception:
            continue


def warm_up_llm(settings: Dict[str, Any] | None = None, report: Any = None) -> bool:
    """Дожидается загрузки модели по требованию и делает пробный запрос."""
    s = settings or load_settings()
    state, _ = model_state(s)
    if state == "loaded":
        return True
    _try_lm_load(s)
    timeout = max(30, int(s.get("model_load_timeout", 600) or 600))
    llm = get_llm({**s, "request_timeout": timeout})
    if llm is None:
        return False
    deadline = time.time() + timeout
    attempt = 0
    last = ""
    while True:
        attempt += 1
        if report:
            report(1, "Загрузка модели в LM Studio" if attempt == 1
                   else f"Загрузка модели в LM Studio (попытка {attempt})")
        try:
            llm.invoke([("system", "Ответь одним словом."), ("human", "ping")])
            logger.info(f"LLM готова: {s.get('model')} (попытка {attempt})")
            return True
        except Exception as e:
            last = str(e)
            logger.warning(f"LLM загрузка, попытка {attempt}: {last[:200]}")
            if time.time() >= deadline:
                logger.error(f"LLM не загрузилась за {timeout} с: {last[:300]}")
                return False
            time.sleep(min(15, 2 * attempt))


def _is_model_loading_error(message: str) -> bool:
    m = str(message or "").lower()
    return any(k in m for k in ("loading", "not loaded", "no model", "model is not",
                                "timed out", "timeout", "503", "connection", "refused",
                                "context length", "context window", "too large"))


def _prepare_llm(report: Any = None) -> None:
    if not warm_up_llm(report=report):
        raise RuntimeError("Модель в LM Studio не загрузилась — обработка отменена")


ANON_SYSTEM = (
    "Ты — система обезличивания документов. Тебе дают ОДИН фрагмент документа "
    "(договор, счёт, акт, таблица, письмо) и метку, откуда он взят. "
    "Найди в нём чувствительные данные и верни замены.\n"
    "Формат ответа — ровно один JSON-объект, без markdown и пояснений:\n"
    '{"replacements":[{"original":"точная подстрока фрагмента",'
    '"replacement":"другое правдоподобное значение","type":"тип"}]}\n'
    'Если менять нечего — верни {"replacements": []}.\n\n'
    "ЧУВСТВИТЕЛЬНЫЕ ДАННЫЕ:\n"
    "— ФИО полностью и по частям: фамилия, имя, отчество, инициалы в любом порядке; "
    "ФИО в верхнем регистре, в кавычках, латиницей;\n"
    "— организации: полные юридические формы и краткие названия, бренды, "
    "аббревиатуры (ООО, АО, ПАО, ИП и т.п. вместе с названием), банки;\n"
    "— адреса целиком и по частям: страна, регион, город, улица, дом, корпус, "
    "квартира/офис, индекс; адреса латиницей;\n"
    "— телефоны, email, сайты, мессенджеры;\n"
    "— ИНН, КПП, ОГРН/ОГРНИП, СНИЛС, паспорт, БИК, расчётные и корреспондентские счета;\n"
    "— суммы, цены, НДС, авансы, штрафы, пени — цифрами и прописью;\n"
    "— даты (документа, рождения, поставки, оплаты) и сроки/гарантии;\n"
    "— должности, подписи и расшифровки подписей;\n"
    "— конкретные наименования товаров, работ и услуг в таблицах и тексте.\n\n"
    "НЕ МЕНЯЙ:\n"
    "— подписи полей и заголовки: «Поставщик», «Покупатель», «Заказчик», "
    "«Исполнитель», «ИНН», «КПП», «БИК», «Расчётный счёт», «Банк получателя», "
    "«Адрес», «Телефон», «Наименование», «Количество», «Ед. изм.», «Цена», "
    "«Сумма», «Итого», «Дата», «Номер», «Период», «Подпись», «М.П.»;\n"
    "— места для подписи, прочерки, линии и пустые графы: если значения нет — "
    "ничего не возвращай;\n"
    "— служебные слова, номера пунктов, названия разделов и колонок.\n\n"
    "КАК ЗАПОЛНЯТЬ ЗАМЕНУ:\n"
    "1. original — подстрока фрагмента символ в символ, без подписи поля: "
    "«7701234567», а не «ИНН 7701234567». Одно значение — одна пара. "
    "Если подпись всё же вошла в original, начни replacement с той же подписи.\n"
    "2. replacement — обязательно ДРУГОЕ правдоподобное значение того же вида: "
    "у чисел та же длина и разделители, у дат тот же формат, у телефона и счёта "
    "тот же шаблон, у названия та же юридическая форма и кавычки, у ФИО тот же "
    "порядок слов и пол. Сохраняй регистр, валюту и единицы измерения.\n"
    "3. Думай о роли значения. Подпись руководителя — другое ФИО; название "
    "товара — другое название; город — другой город. Не ставь имя на место "
    "должности или организации и наоборот.\n"
    "4. Указывай все варианты одного значения во фрагменте: полное ФИО и "
    "отдельную фамилию, полное и краткое название, адрес и его части, сумму "
    "цифрами и прописью.\n"
    "5. original должен быть достаточно длинным и однозначным. Не возвращай "
    "отдельные цифры, буквы и общие слова.\n"
    "6. Если фрагмент начинается или заканчивается посреди значения — не угадывай: "
    "верни только то, что видно целиком и однозначно.\n"
    "7. Если в сообщении есть список согласованных замен — используй для этих "
    "значений те же replacement.\n"
    "8. Экранируй кавычки и переводы строк по правилам JSON."
)


def _natural_cut(text: str, lo: int, hi: int) -> int:
    """Граница реза не дальше hi: абзац, строка, предложение, пробел."""
    for pattern in (r"\n[ \t]*\n", r"\n", r"(?<=[.!?;:])\s+"):
        last = None
        for last in re.finditer(pattern, text[lo:hi]):
            pass
        if last is not None:
            return lo + last.end()
    space = text.rfind(" ", lo, hi)
    return space + 1 if space > lo else hi


def _split_long(text: str, start: int, end: int, label: str,
                size: int, overlap: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    i = start
    while i < end:
        j = min(end, i + size)
        if j < end:
            j = _natural_cut(text, i + max(1, size // 2), j)
        out.append({"start": i, "end": j, "labels": [label]})
        if j >= end:
            break
        nxt = j - overlap
        i = nxt if nxt > i else j
    return out


def _chunk_document(text: str, segments: List[Dict[str, Any]],
                    size: int, overlap: int) -> List[Dict[str, Any]]:
    """Режет документ на фрагменты по границам страниц/листов/частей.

    Возвращает фрагменты с диапазоном в исходном тексте и меткой места.
    """
    chunks: List[Dict[str, Any]] = []
    cur: Dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur["end"] > cur["start"]:
            chunks.append(cur)
        cur = None

    for seg in segments:
        start = max(0, int(seg.get("start") or 0))
        end = min(len(text), int(seg.get("end") or 0))
        label = str(seg.get("label") or "документ")
        if end <= start:
            continue
        if cur is not None and end - cur["start"] > size:
            flush()
        if end - start > size:
            flush()
            chunks.extend(_split_long(text, start, end, label, size, overlap))
            continue
        if cur is None:
            cur = {"start": start, "end": end, "labels": [label]}
        else:
            cur["end"] = end
            if label not in cur["labels"]:
                cur["labels"].append(label)
    flush()

    total = len(chunks)
    for index, chunk in enumerate(chunks):
        chunk["text"] = text[chunk["start"]:chunk["end"]]
        labels = chunk.pop("labels")
        where = ", ".join(labels[:3]) + (" …" if len(labels) > 3 else "")
        chunk["label"] = f"Фрагмент {index + 1} из {total} ({where})"
        chunk["index"] = index
    return chunks


def _strip_llm_wrappers(raw: str) -> str:
    s = str(raw or "").replace("\ufeff", "")
    fenced = re.match(r"^\s*```[A-Za-z0-9_-]*\s*\n?([\s\S]*?)\n?```\s*$", s)
    if fenced:
        s = fenced.group(1)
    return re.sub(
        r"^\s*(?:вот|готово|измен[её]нный\s+текст|результат|ответ|текст)\s*[:\-]\s*",
        "", s, count=1, flags=re.I)


def _load_llm_json(raw: str) -> Any:
    s = _strip_llm_wrappers(str(raw or ""))
    if not s:
        return None
    candidates = [s]
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        candidates.append(s[start:])
        close = max(s.rfind("}"), s.rfind("]"))
        if close > start:
            candidates.append(s[start:close + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _locate_value(text: str, value: str, replacement: str = "") -> str:
    """Возвращает реальную подстроку документа для строки от модели.

    Если модель захватила подпись поля («ИНН 7701234567»), подпись отрезается:
    заменяется только значение. Если замена сама начинается с этой подписи,
    значит это часть названия/значения — берём строку целиком.
    """
    value = str(value or "").strip()
    if not value:
        return ""
    cleaned = _clean_value(value)
    norm, idx = _normalize_with_map(text)

    def find(candidate: str) -> str:
        if not candidate:
            return ""
        if candidate in text:
            return candidate
        for m in _find_occurrences(norm, candidate):
            if m.end() > m.start():
                return text[idx[m.start()]: idx[m.end() - 1] + 1]
        return ""

    found = ""
    for candidate in (value, cleaned, value.rstrip(".,;:!?»\"'"),
                      cleaned.rstrip(".,;:!?»\"'")):
        found = find(candidate)
        if found:
            break
    if found:
        return found
    words = value.split()
    head_replacement = _clean_value(replacement).lower()
    for k in range(1, min(4, len(words))):
        head = " ".join(words[:k]).lower()
        if head and head_replacement.startswith(head):
            continue
        found = find(" ".join(words[k:]).strip(" ,;:"))
        if found:
            return found
    return ""


def _same_value(left: str, right: str) -> bool:
    return _clean_value(left).lower() == _clean_value(right).lower()


def _parse_replacements(raw: str, chunk: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]] | None:
    """Разбирает ответ модели.

    Возвращает (принятые замены, значения без корректной замены) или None,
    если ответ вообще не похож на JSON нужной формы.
    """
    data = _load_llm_json(raw)
    if data is None:
        return None
    if isinstance(data, dict):
        items = None
        for key in ("replacements", "changes", "pairs", "items", "data"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None:
            # Модель вернула одну пару без обёртки.
            if any(k in data for k in ("original", "source", "text")):
                items = [data]
            else:
                return None
    elif isinstance(data, list):
        items = data
    else:
        return None

    accepted: List[Dict[str, str]] = []
    invalid: List[Dict[str, str]] = []
    seen: set[str] = set()
    not_found = bad = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original") or item.get("source") or item.get("text") or "").strip()
        replacement = str(item.get("replacement") or item.get("target")
                          or item.get("fake") or "").strip()
        kind = str(item.get("type") or item.get("kind") or "").strip().lower()
        located = _locate_value(chunk, original, replacement)
        if not located:
            not_found += 1
            continue
        if _is_bad_value(located):
            bad += 1
            continue
        key = located.lower()
        if key in seen:
            continue
        seen.add(key)
        if (not replacement or _same_value(replacement, located)
                or replacement == MASK or len(replacement) > 300):
            invalid.append({"original": located, "type": kind})
            continue
        accepted.append({"original": located, "replacement": replacement, "type": kind})
    if not_found or bad:
        logger.info(f"LLM разбор: принято {len(accepted)}, нет в тексте {not_found}, "
                    f"непригодно {bad}, без замены {len(invalid)}")
    return accepted, invalid


def _invoke(llm, messages: List[Any], retries: int) -> Tuple[str | None, str, bool]:
    """Запрос к модели. Третий элемент — «нужно повторить без json-режима»."""
    global _JSON_MODE_SUPPORTED
    last = ""
    for attempt in range(2 + max(0, retries)):
        try:
            resp = llm.invoke(messages)
            return (resp.content if hasattr(resp, "content") else str(resp)), "", False
        except Exception as e:
            last = str(e)
            logger.warning(f"LLM запрос: {last[:200]}")
            if "response_format" in last or "json_object" in last:
                _JSON_MODE_SUPPORTED = False
                return None, last, True
            if "context" in last.lower():
                # Переполнение контекста лечится дроблением фрагмента, а не повтором.
                return None, last, False
            if not _is_model_loading_error(last) and attempt >= 1:
                break
            if attempt >= 1 + retries:
                break
            time.sleep(min(15, 2 * (attempt + 1)))
    return None, last, False


def _known_hint(known: Dict[str, Tuple[str, str]], chunk: str) -> str:
    if not known:
        return ""
    norm, _ = _normalize_with_map(chunk)
    haystack = _prefilter_text(norm)
    pairs: List[Dict[str, str]] = []
    budget = 2000
    for original, fake in known.values():
        if not fake:
            continue
        needle = _prefilter_needle(original)
        if needle and needle not in haystack:
            continue
        item = {"original": original, "replacement": fake}
        cost = len(json.dumps(item, ensure_ascii=False)) + 2
        if cost > budget:
            break
        budget -= cost
        pairs.append(item)
        if len(pairs) >= 25:
            break
    if not pairs:
        return ""
    return ("Уже согласованные замены (для этих значений используй те же "
            "replacement):\n" + json.dumps(pairs, ensure_ascii=False) + "\n\n")


def _repair_replacements(llm, settings: Dict[str, Any], chunk: str,
                         invalid: List[Dict[str, str]], retries: int) -> List[Dict[str, str]]:
    """Просит модель отдельно заполнить replacement для значений без замены."""
    if not invalid:
        return []
    listing = json.dumps([{"original": i["original"], "type": i.get("type", "")}
                          for i in invalid], ensure_ascii=False)
    prompt = (
        "Для каждого значения ниже верни пару original/replacement. replacement — "
        "другое правдоподобное значение того же вида, не равное original.\n"
        '{"replacements":[{"original":"...","replacement":"...","type":"..."}]}\n\n'
        f"{listing}"
    )
    raw, _, plain = _invoke(llm, [("system", ANON_SYSTEM), ("human", prompt)], retries)
    if plain:
        llm = get_llm(settings, json_mode=False) or llm
        raw, _, _ = _invoke(llm, [("system", ANON_SYSTEM), ("human", prompt)], retries)
    if not raw:
        return []
    parsed = _parse_replacements(raw, chunk)
    if parsed is None:
        return []
    accepted, _ = parsed
    wanted = {i["original"].lower() for i in invalid}
    return [item for item in accepted if item["original"].lower() in wanted]


def _request_replacements(llm, settings: Dict[str, Any], chunk: Dict[str, Any],
                          known: Dict[str, Tuple[str, str]], retries: int,
                          depth: int = 0) -> Tuple[List[Dict[str, str]] | None, str]:
    """Замены для одного фрагмента. None — фрагмент обработать не удалось."""
    text = chunk["text"]
    label = chunk["label"]
    prompt = (f"{_known_hint(known, text)}{label}. Ниже текст фрагмента. "
              f"Верни JSON с заменами:\n\n{text}")
    messages = [("system", ANON_SYSTEM), ("human", prompt)]
    raw, error, plain = _invoke(llm, messages, retries)
    if plain:
        llm = get_llm(settings, json_mode=False) or llm
        raw, error, _ = _invoke(llm, messages, retries)
    had_response = raw is not None
    parsed = _parse_replacements(raw, text) if raw else None
    if parsed is None and raw is not None:
        raw, error, _ = _invoke(
            llm, [("system", ANON_SYSTEM),
                  ("human", prompt + "\n\nОтвет — только JSON-объект, без пояснений.")], 0)
        parsed = _parse_replacements(raw, text) if raw else None
    if parsed is None:
        context_error = "context" in str(error or "").lower()
        if depth < 2 and len(text) > 1200 and (had_response or context_error):
            halves = _split_long(text, 0, len(text), chunk.get("place") or label,
                                 max(800, len(text) // 2), CHUNK_OVERLAP)
            combined: List[Dict[str, str]] = []
            for part_index, half in enumerate(halves):
                part = {"text": text[half["start"]:half["end"]],
                        "label": f"{label}.{part_index + 1}", "place": chunk.get("place")}
                got, error = _request_replacements(llm, settings, part, known, retries, depth + 1)
                if got is None:
                    return None, error
                combined.extend(got)
                for item in got:
                    known.setdefault(item["original"].lower(), (item["original"], item["replacement"]))
            return combined, ""
        logger.warning(f"LLM {label}: ответ без корректных замен")
        return None, error or "invalid_json"

    accepted, invalid = parsed
    if invalid:
        repaired = _repair_replacements(llm, settings, text, invalid, retries)
        accepted.extend(repaired)
        missing = len(invalid) - len(repaired)
        if missing > 0:
            logger.warning(f"LLM {label}: {missing} значений остались без замены")
            return None, "bad_replacement"
    return accepted, ""


def anonymize_text(text: str,
                   segments: List[Dict[str, Any]] | None = None,
                   progress: Any = None) -> Tuple[List[Dict[str, str]], bool, str]:
    """Прогоняет через модель весь текст документа и собирает согласованные замены.

    Возвращает (замены, успех, ошибка). Если хотя бы один фрагмент не удалось
    обработать, успех = False, и документ не выдаётся.
    """
    s = load_settings()
    text = text or ""
    if not text.strip():
        return [], False, "empty"
    if not s.get("llm_enabled", True):
        return [], False, "disabled"

    size = max(1000, int(s.get("llm_chunk_chars", 3500) or 3500))
    # Документ уходит в модель целиком: ограничение длины убрано, иначе часть
    # текста осталась бы необезличенной.
    segments = segments or [{"start": 0, "end": len(text), "label": "документ"}]
    bounded = []
    for seg in segments:
        start = max(0, min(int(seg.get("start") or 0), len(text)))
        stop = max(start, min(int(seg.get("end") or 0), len(text)))
        if stop > start:
            bounded.append({"start": start, "end": stop,
                            "label": seg.get("label") or "документ"})
    if not bounded:
        return [], False, "empty"

    chunks = _chunk_document(text, bounded, size, CHUNK_OVERLAP)
    retries = max(0, int(s.get("llm_retries", 3) or 0))
    known: Dict[str, Tuple[str, str]] = {}
    replacements: List[Dict[str, str]] = []
    processed = 0
    total = len(chunks)
    logger.info(f"LLM | документ: {len(text)} символов, {total} фрагментов, "
                f"чанк {size}, перекрытие {CHUNK_OVERLAP}")

    for index, chunk in enumerate(chunks):
        if progress:
            progress(index, total)
        llm = get_llm(s, json_mode=True)
        if llm is None:
            return [], False, "llm_init"
        chunk["place"] = chunk["label"].split("(")[-1].rstrip(")")
        got, error = _request_replacements(llm, s, chunk, known, retries)
        if got is None:
            logger.error(f"LLM | {chunk['label']}: {error}")
            if progress:
                progress(total, total)
            return [], False, error or "llm_failed"
        processed += len(chunk["text"])
        added = 0
        for item in got:
            key = item["original"].lower()
            if key in known:
                continue
            known[key] = (item["original"], item["replacement"])
            replacements.append(item)
            added += 1
        logger.info(f"LLM | {chunk['label']} | символов {len(chunk['text'])} | "
                    f"новых замен {added} | пропущено дублей {len(got) - added}")
    if progress:
        progress(total, total)
    logger.info(f"LLM | готово: {len(replacements)} замен, обработано {processed} символов")
    return replacements, True, ""


def _llm_failure_message(error: str) -> str:
    messages = {
        "disabled": "LLM отключена — обезличивание невозможно",
        "llm_init": "LLM недоступна — обработка отменена",
        "empty": "В документе не найден текст для обезличивания",
        "invalid_json": "LLM вернула некорректный ответ — обработка отменена",
        "bad_replacement": "LLM не предложила замену для части данных — обработка отменена",
    }
    if error in messages:
        return messages[error]
    return "LLM не отвечает — обработка отменена" + (f": {error[:200]}" if error else "")


def _build_replacement_masker(replacements: Any) -> Masker:
    masker = Masker()
    for item in replacements or []:
        if isinstance(item, dict):
            masker.set_exact(item.get("original") or "", item.get("replacement") or "")
    return masker


def _replacement_extras(replacements: Any) -> List[Dict[str, str]]:
    extras: List[Dict[str, str]] = []
    for item in replacements or []:
        original = (item.get("original") or "") if isinstance(item, dict) else ""
        if original:
            extras.append({"text": original, "type": str(item.get("type") or "")})
    return extras


# ================================================================
# 4. ОБРАБОТКА ФАЙЛОВ
# ================================================================
# ---------------------------------------------------------------- извлечение текста
def extract_pdf_text(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """Весь текст постранично + карта страниц для последующей замены."""
    import pymupdf
    doc = pymupdf.open(path)
    parts: List[str] = []
    segments: List[Dict[str, Any]] = []
    position = 0
    try:
        if doc.needs_pass:
            raise ValueError("PDF защищён паролем")
        # sort=True даёт порядок чтения, иначе подписи полей и значения расходятся.
        for number, page in enumerate(doc, start=1):
            page_text = page.get_text("text", sort=True)
            extra: List[str] = []
            try:
                extra.extend(str(w.field_value) for w in page.widgets() or ()
                             if getattr(w, "field_value", None))
            except Exception:
                pass
            try:
                for annot in page.annots() or []:
                    info = dict(annot.info or {})
                    for key in ("content", "title", "subject"):
                        value = str(info.get(key) or "").strip()
                        if value:
                            extra.append(value)
            except Exception:
                pass
            if extra:
                page_text = "\n".join([page_text, *extra])
            if not page_text.strip():
                continue
            segments.append({"start": position, "end": position + len(page_text),
                             "label": f"стр. {number}"})
            parts.append(page_text)
            position += len(page_text) + 1
        return "\n".join(parts), segments
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
                    if fake == value:
                        continue
                    originals = [flat_spans[si]["text"][a:b] for si, a, b in pieces]
                    fakes = _distribute_replacement(fake, originals)
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
                if fake == value:
                    continue
                originals = [flat_spans[si]["text"][a:b] for si, a, b in pieces]
                fakes = _distribute_replacement(fake, originals)
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
        if fake == value:
            continue
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
            # Замена одним куском, иначе короткий span сжимается до нечитаемого.
            merges.append((group, fake))
            merged_ids.update(id(s) for s in group)
        else:
            originals = [spans[si]["text"][a:b] for si, a, b in pieces]
            fakes = _distribute_replacement(fake, originals)
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
                    if new != value:
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
                    if new != value:
                        widget.field_value = new
                        widget.update()
                        masked += 1
                label = getattr(widget, "field_label", None)
                if isinstance(label, str) and label:
                    new_label = mask_text(label, extras, masker)
                    if new_label != label:
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


def mask_pdf_file(input_path: Path, output_path: Path, extras: Any = (),
                  masker: Masker | None = None,
                  progress: Any = None) -> Dict[str, Any]:
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
    masker = masker or Masker()
    for page_index, page in enumerate(doc):
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
            if progress:
                progress(page_index + 1, pages)
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
        if progress:
            progress(page_index + 1, pages)
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
    """Преобразует строку с любыми разделителями в число для ячейки."""
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


def _numeric_fake(masker: Masker, value: Any):
    """Замена для числовой или датовой ячейки — только если её дала модель."""
    if isinstance(value, bool):
        return None

    def lookup(*candidates: str) -> str:
        for candidate in candidates:
            fake = masker.lookup(candidate)
            if fake:
                return fake
        return ""

    if isinstance(value, (datetime_cls, date_cls)):
        is_datetime = isinstance(value, datetime_cls)
        fake = lookup(value.isoformat(sep=" ") if is_datetime else value.isoformat(),
                      value.strftime("%d.%m.%Y %H:%M:%S" if is_datetime else "%d.%m.%Y"),
                      value.strftime("%d.%m.%Y"), value.strftime("%d/%m/%Y"))
        if not fake:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                parsed = datetime_cls.strptime(fake, fmt)
            except Exception:
                continue
            return parsed if is_datetime else parsed.date()
        return None

    if isinstance(value, (int, float)):
        text = str(value)
        decimal = str(value).replace(".", ",")
        fake = lookup(text, decimal, f"{value:,}".replace(",", " "),
                      f"{value:,}".replace(",", "\u00a0"))
        if not fake:
            return None
        try:
            return _to_number(fake, isinstance(value, int))
        except Exception:
            return None
    return None


def extract_excel_text(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """Текст листов + карта листов. Строки склеены: ФИО может быть по колонкам."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    parts: List[str] = []
    segments: List[Dict[str, Any]] = []
    position = 0
    try:
        for ws in wb.worksheets:
            lines: List[str] = []
            for row in ws.iter_rows(values_only=True):
                vals: List[str] = []
                for v in row:
                    if v is None:
                        continue
                    text = _excel_cell_text(v)
                    if text is None:
                        text = str(v)
                    if text.strip():
                        vals.append(text.strip())
                if vals:
                    lines.append(" ".join(vals))
            # Примечания и ссылки не видны в значениях ячеек, но тоже содержат ПДн.
            for row in ws.iter_rows():
                for cell in row:
                    try:
                        comment = cell.comment
                        if comment is not None and comment.text:
                            lines.append(str(comment.text))
                    except Exception:
                        pass
                    try:
                        link = cell.hyperlink
                        target = getattr(link, "target", None) if link is not None else None
                        if isinstance(target, str) and target.strip():
                            lines.append(target)
                    except Exception:
                        pass
            try:
                for part in _header_footer_parts(ws):
                    text = (getattr(part, "text", "") or "").strip()
                    if text:
                        lines.append(text)
            except Exception:
                pass
            sheet_text = "\n".join(lines)
            if not sheet_text.strip():
                continue
            segments.append({"start": position, "end": position + len(sheet_text),
                             "label": f"лист «{ws.title}»"})
            parts.append(sheet_text)
            position += len(sheet_text) + 1
        return "\n".join(parts), segments
    finally:
        wb.close()


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


def mask_excel_file(input_path: Path, output_path: Path, extras: Any = (),
                    masker: Masker | None = None,
                    progress: Any = None) -> Dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(input_path)
    masker = masker or Masker()
    masked = 0
    try:
        sheets = list(wb.worksheets)
        for sheet_index, ws in enumerate(sheets):
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
                    # Числовые и датовые ячейки меняются только по решению модели:
                    # если для значения есть её замена — ставим, иначе не трогаем.
                    fake_value = _numeric_fake(masker, v)
                    if fake_value is not None:
                        cell.value = fake_value
                        masked += 1
                # Склеиваем текстовые ячейки строки через пробел: ФИО, организация
                # или адрес могут быть разбиты по колонкам, и модель должна видеть
                # их одним значением.
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
                    if fake == value:
                        continue
                    originals = [cells[ci][1][a:b] for ci, a, b in pieces]
                    fakes = _distribute_replacement(fake, originals)
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
            if progress:
                progress(sheet_index + 1, len(sheets))
        masked += _scrub_excel_properties(wb, masker, extras)
        wb.save(output_path)
        preview = _excel_preview(wb.active)
    finally:
        wb.close()
    return {"masked": masked, "pages": 1, "preview_rows": preview}

DOCX_PARTS = ("word/document.xml", "word/header", "word/footer",
              "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml",
              "docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")


def _register_docx_namespaces(raw: bytes) -> None:
    try:
        head = raw[:8000].decode("utf-8", "ignore")
    except Exception:
        return
    for prefix, uri in re.findall(r'xmlns:([A-Za-z0-9_.-]+)="([^"]+)"', head):
        if prefix.lower() in ("xml", "xmlns") or prefix.startswith("ns"):
            continue
        try:
            ET.register_namespace(prefix, uri)
        except Exception:
            pass


def _docx_own_runs(paragraph) -> List[Any]:
    """w:t этого абзаца, без вложенных абзацев (текстовые поля/надписи)."""
    tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: List[Any] = []

    def walk(el) -> None:
        for child in el:
            if child.tag == tag + "p":
                continue
            if child.tag == tag + "t":
                out.append(child)
            else:
                walk(child)

    walk(paragraph)
    return out


def _docx_part_paragraphs(root) -> List[Any]:
    tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return [p for p in root.iter(tag + "p") if _docx_own_runs(p)]


def _mask_docx_paragraph(paragraph, extras: Any, masker: Masker) -> bool:
    runs = _docx_own_runs(paragraph)
    texts = [r.text or "" for r in runs]
    chars: List[str] = []
    cmap: List[Tuple[int, int]] = []
    for ri, text in enumerate(texts):
        for oi, ch in enumerate(text):
            if ch in _ZERO_WIDTH:
                continue
            chars.append(_norm_char(ch))
            cmap.append((ri, oi))
    joined = "".join(chars)
    if not joined.strip():
        return False
    matches = iter_matches(joined, extras)
    if not matches:
        return False
    edits: Dict[int, List[Tuple[int, int, str]]] = {}
    for start, end, kind, value in matches:
        pieces = _range_pieces(cmap, start, end)
        if not pieces:
            continue
        fake = masker.get(kind, value)
        if fake == value:
            continue
        originals = [texts[ri][a:b] for ri, a, b in pieces]
        fakes = _distribute_replacement(fake, originals)
        for (ri, a, b), fp in zip(pieces, fakes):
            if fp is not None:
                edits.setdefault(ri, []).append((a, b, fp))
    changed = False
    for ri, items in edits.items():
        text = texts[ri]
        for a, b, fp in sorted(items, key=lambda x: -x[0]):
            text = text[:a] + fp + text[b:]
        if text != texts[ri]:
            runs[ri].text = text
            changed = True
    return changed


def _docx_parts(path: Path) -> List[str]:
    """Части .docx в порядке чтения: документ, колонтитулы/сноски, свойства."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception:
        return []
    found = [n for n in names if any(n == p or n.startswith(p) for p in DOCX_PARTS)
             and n.endswith(".xml")]

    def order(name: str) -> int:
        if name.startswith("word/document"):
            return 0
        return 2 if name.startswith("docProps") else 1

    return sorted(found, key=order)


def _docx_part_label(name: str) -> str:
    if name.startswith("word/document"):
        return "текст документа"
    if name.startswith("word/header"):
        return "верхний колонтитул"
    if name.startswith("word/footer"):
        return "нижний колонтитул"
    if name.startswith("word/footnotes"):
        return "сноски"
    if name.startswith("word/endnotes"):
        return "концевые сноски"
    if name.startswith("word/comments"):
        return "комментарии"
    if name.startswith("docProps"):
        return "свойства документа"
    return name


def extract_docx_segments(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """Текст всех частей .docx + карта частей (документ, колонтитулы, сноски)."""
    parts: List[str] = []
    segments: List[Dict[str, Any]] = []
    position = 0
    try:
        with zipfile.ZipFile(path) as z:
            for name in _docx_parts(path):
                raw = z.read(name)
                _register_docx_namespaces(raw)
                try:
                    root = ET.fromstring(raw)
                except Exception:
                    continue
                lines: List[str] = []
                if name.startswith("docProps/"):
                    # Свойства документа: dc:title, dc:creator, настраиваемые поля.
                    for el in root.iter():
                        if len(el) == 0 and el.text and el.text.strip():
                            lines.append(el.text.strip())
                for p in _docx_part_paragraphs(root):
                    text = "".join(t.text or "" for t in _docx_own_runs(p))
                    if text.strip():
                        lines.append(text)
                part_text = "\n".join(lines)
                if not part_text.strip():
                    continue
                segments.append({"start": position, "end": position + len(part_text),
                                 "label": _docx_part_label(name)})
                parts.append(part_text)
                position += len(part_text) + 1
    except Exception as e:
        logger.warning(f"DOCX extract: {e}")
    return "\n".join(parts), segments


def extract_docx_text(path: Path) -> str:
    return extract_docx_segments(path)[0]


def mask_docx_file(input_path: Path, output_path: Path, extras: Any = (),
                   masker: Masker | None = None, progress: Any = None) -> Dict[str, Any]:
    masker = masker or Masker()
    parts = _docx_parts(input_path)
    masked = 0
    changed_parts: Dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(input_path) as zin:
            for index, name in enumerate(parts):
                raw = zin.read(name)
                _register_docx_namespaces(raw)
                try:
                    root = ET.fromstring(raw)
                except Exception:
                    continue
                changed = False
                for p in _docx_part_paragraphs(root):
                    if _mask_docx_paragraph(p, extras, masker):
                        masked += 1
                        changed = True
                if name.startswith("docProps/"):
                    for el in root.iter():
                        if len(el) == 0 and el.text:
                            new = mask_text(el.text, extras, masker)
                            if new != el.text:
                                el.text = new
                                masked += 1
                                changed = True
                if changed:
                    changed_parts[name] = ET.tostring(root, encoding="UTF-8",
                                                      xml_declaration=True)
                if progress:
                    progress(index + 1, len(parts))
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = changed_parts.get(item.filename)
                    zout.writestr(item, data if data is not None else zin.read(item.filename))
    except Exception as e:
        logger.error(f"DOCX mask: {e}")
        raise
    preview = extract_docx_text(output_path)[:1800]
    return {"masked": masked, "pages": 1, "preview": preview}


def _convert_with_word(src: Path, dst: Path) -> bool:
    script = (
        "$ErrorActionPreference='Stop';"
        "$app=New-Object -ComObject Word.Application;"
        "$app.Visible=$false;$app.DisplayAlerts=0;"
        f"$doc=$app.Documents.Open('{str(src).replace(chr(39), chr(39) * 2)}',$false,$true);"
        "try{"
        f"$doc.SaveAs([ref]'{str(dst).replace(chr(39), chr(39) * 2)}',[ref]16)"
        "}finally{$doc.Close(0);$app.Quit()}"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=180)
        return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception as e:
        logger.warning(f"DOC Word convert: {e}")
        return False


def _convert_with_libreoffice(src: Path, dst: Path) -> bool:
    candidates = ["soffice", "soffice.exe",
                  r"C:\Program Files\LibreOffice\program\soffice.exe",
                  r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
    for exe in candidates:
        try:
            proc = subprocess.run(
                [exe, "--headless", "--convert-to", "docx", "--outdir",
                 str(dst.parent), str(src)],
                capture_output=True, timeout=180)
        except Exception:
            continue
        made = dst.parent / (src.stem + ".docx")
        if proc.returncode == 0 and made.exists() and made.stat().st_size > 0:
            if made != dst:
                shutil.move(str(made), str(dst))
            return True
    return False


def _doc_to_docx(src: Path, dst: Path) -> None:
    """Старый .doc -> .docx. Сначала проверяем, что это не переименованный docx."""
    try:
        with open(src, "rb") as f:
            head = f.read(8)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать .doc: {e}")
    if head[:2] == b"PK":
        shutil.copyfile(src, dst)
        return
    if _convert_with_word(src, dst) or _convert_with_libreoffice(src, dst):
        return
    raise RuntimeError("Не удалось преобразовать .doc. Установите Microsoft Word "
                       "или LibreOffice, либо сохраните файл как .docx")


def process_doc(input_path: Path, output_path: Path,
                progress: Any = None) -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Преобразование .doc")
    tmp = input_path.with_name(f"{input_path.stem}.converted.docx")
    try:
        _doc_to_docx(input_path, tmp)
        result = process_docx(tmp, output_path, progress=progress)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    result["warning"] = "Файл .doc преобразован в .docx"
    return result


def process_docx(input_path: Path, output_path: Path,
                 progress: Any = None) -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Извлечение текста")
    text, segments = extract_docx_segments(input_path)
    if not text.strip():
        raise RuntimeError("В документе Word не найден текст для обезличивания")
    report(3, "Загрузка модели")
    _prepare_llm(report)
    report(5, "Поиск персональных данных")
    replacements, llm_ok, llm_error = anonymize_text(
        text, segments, _progress_callback(progress, 5, 67, "Обезличивание текста"))
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_docx_file(
        input_path, output_path, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Правка документа"))
    report(100, "Готово")
    result: Dict[str, Any] = {"preview_type": "docx", **stats, "llm_used": llm_ok}
    return result


def _ocr_tesseract(path: Path) -> List[Dict[str, Any]]:
    try:
        import pytesseract
        from PIL import Image
        from pytesseract import Output
    except Exception:
        return []
    s = load_settings()
    try:
        with Image.open(path) as img:
            data = pytesseract.image_to_data(
                img.convert("RGB"), lang=str(s.get("ocr_lang", "rus+eng")),
                output_type=Output.DICT)
    except Exception as e:
        logger.warning(f"OCR tesseract: {e}")
        return []
    lines: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    for i, text in enumerate(data.get("text", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            x, y = int(data["left"][i]), int(data["top"][i])
            w, h = int(data["width"][i]), int(data["height"][i])
        except Exception:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]),
               int(data["line_num"][i]))
        item = lines.get(key)
        if item is None:
            lines[key] = {"text": text, "box": [x, y, x + w, y + h]}
        else:
            item["text"] += " " + text
            b = item["box"]
            b[0], b[1] = min(b[0], x), min(b[1], y)
            b[2], b[3] = max(b[2], x + w), max(b[3], y + h)
    return list(lines.values())


IMAGE_OCR_PROMPT = (
    "Распознай весь текст на изображении документа и верни координаты строк. "
    "Ответ — строго JSON без markdown:\n"
    '{"lines":[{"text":"точный текст строки","box":[x0,y0,x1,y1]}]}\n'
    "box — прямоугольник строки в координатах 0..1000 относительно ширины и "
    "высоты изображения. Сохраняй порядок строк сверху вниз, не пропускай "
    "таблицы, реквизиты, подписи и мелкий текст."
)


def _ocr_vision(path: Path) -> List[Dict[str, Any]]:
    """OCR через мультимодальную модель LM Studio (если Tesseract недоступен)."""
    s = load_settings()
    try:
        from PIL import Image
        with Image.open(path) as img:
            width, height = img.size
            img = img.convert("RGB")
            img.thumbnail((2400, 2400))
            tw, th = img.size
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
    except Exception as e:
        logger.warning(f"OCR image open: {e}")
        return []
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    payload = {
        "model": s.get("model"),
        "temperature": 0,
        "max_tokens": max(4096, int(s.get("max_tokens", 4096) or 4096)),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": IMAGE_OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    url = str(s.get("lm_studio_url") or "").rstrip("/") + "/chat/completions"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {s.get('api_key', 'lm-studio')}"})
        timeout = max(60, int(s.get("model_load_timeout", 600) or 600))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        parsed = _load_llm_json(content)
    except Exception as e:
        logger.warning(f"OCR vision: {e}")
        return []
    items = parsed.get("lines") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        logger.warning("OCR vision: нет JSON со строками")
        return []
    lines: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        box = item.get("box") or item.get("bbox")
        if not text or not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = [float(v) for v in box]
        except (TypeError, ValueError):
            continue
        if max(x0, x1, y0, y1) <= 1.5:
            x0, x1, y0, y1 = x0 * width, x1 * width, y0 * height, y1 * height
        elif max(x0, x1, y0, y1) <= 1005:
            # Модель просили вернуть координаты 0..1000.
            x0, x1 = x0 / 1000 * width, x1 / 1000 * width
            y0, y1 = y0 / 1000 * height, y1 / 1000 * height
        else:
            # Координаты в пикселях уменьшенной копии.
            x0, x1 = x0 * width / max(1, tw), x1 * width / max(1, tw)
            y0, y1 = y0 * height / max(1, th), y1 * height / max(1, th)
        x0, x1 = sorted((max(0, x0), min(width, x1)))
        y0, y1 = sorted((max(0, y0), min(height, y1)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        lines.append({"text": text, "box": [x0, y0, x1, y1]})
    if lines:
        logger.info(f"OCR vision: {len(lines)} строк")
    return lines


def extract_image_text(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """OCR: сначала Tesseract, иначе мультимодальная модель LM Studio."""
    lines = _ocr_tesseract(path)
    if not lines:
        lines = _ocr_vision(path)
    return "\n".join(str(line.get("text") or "") for line in lines
                     if str(line.get("text") or "").strip()), lines


def _ocr_segments(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Карта строк OCR: каждая распознанная строка — отдельное место документа."""
    segments: List[Dict[str, Any]] = []
    position = 0
    for index, line in enumerate(lines, start=1):
        text = str(line.get("text") or "")
        if not text.strip():
            continue
        segments.append({"start": position, "end": position + len(text),
                         "label": f"строка {index}"})
        position += len(text) + 1
    return segments


def mask_image_file(input_path: Path, output_path: Path, lines: List[Dict[str, Any]],
                    extras: Any = (), masker: Masker | None = None,
                    progress: Any = None) -> Dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont
    masker = masker or Masker()
    masked = 0
    preview: List[str] = []
    font_path = _font_path(False, False)
    with Image.open(input_path) as src:
        img = src.convert("RGB")
    draw = ImageDraw.Draw(img)
    total = max(1, len(lines))
    for index, line in enumerate(lines):
        text = str(line.get("text") or "")
        new = mask_text(text, extras, masker)
        preview.append(new)
        if new != text:
            x0, y0, x1, y1 = [int(round(v)) for v in line["box"]]
            x0, y0 = max(0, x0), max(0, y0)
            x1 = min(img.width, max(x1, x0 + 1))
            y1 = min(img.height, max(y1, y0 + 1))
            draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255))
            # Фейковый текст той же строки, чтобы документ остался читаемым.
            size = max(8, int((y1 - y0) * 0.8))
            font = None
            if font_path:
                try:
                    font = ImageFont.truetype(font_path, size)
                except Exception:
                    font = None
            if font is not None:
                try:
                    while size > 7 and draw.textlength(new, font=font) > (x1 - x0):
                        size -= 1
                        font = ImageFont.truetype(font_path, size)
                    draw.text((x0, y0), new, fill=(0, 0, 0), font=font)
                except Exception:
                    pass
            masked += 1
        if progress:
            progress(index + 1, total)
    suffix = output_path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        img.save(output_path, format="JPEG", quality=95)
    else:
        img.save(output_path, format="PNG")
    return {"masked": masked, "pages": 1, "preview": "\n".join(preview)[:1800]}


def process_image(input_path: Path, output_path: Path,
                  progress: Any = None) -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Распознавание текста")
    report(2, "Загрузка модели")
    _prepare_llm(report)
    report(4, "Распознавание текста")
    text, lines = extract_image_text(input_path)
    if not lines or not text.strip():
        raise RuntimeError("На изображении не найден текст. Установите Tesseract "
                           "(rus+eng) или используйте мультимодальную модель LM Studio")
    report(8, "Поиск персональных данных")
    replacements, llm_ok, llm_error = anonymize_text(
        text, _ocr_segments(lines), _progress_callback(progress, 8, 64, "Обезличивание текста"))
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_image_file(
        input_path, output_path, lines, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Перерисовка изображения"))
    report(100, "Готово")
    result: Dict[str, Any] = {"preview_type": "image", **stats, "llm_used": llm_ok}
    return result


def _progress_callback(progress: Any, start: int, span: int, stage: str):
    """Приводит прогресс обработки кусков/страниц к общему проценту."""
    def report(done: int, total: int) -> None:
        if not progress:
            return
        total = max(1, int(total or 1))
        pct = start + int(span * min(max(done, 0), total) / total)
        progress(min(99, pct), stage)
    return report


def process_pdf(input_path: Path, output_path: Path,
                progress: Any = None) -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Извлечение текста")
    warning = None
    ratio = _pdf_text_ratio(input_path)
    if ratio < 0.5:
        warning = "PDF похож на скан: часть страниц без текстового слоя. OCR не поддерживается — эти страницы не будут обезличены."
        logger.warning(f"PDF scan-like: {input_path.name}, text_pages_ratio={ratio:.2f}")
    text, segments = extract_pdf_text(input_path)
    report(3, "Загрузка модели")
    _prepare_llm(report)
    report(5, "Поиск персональных данных")
    replacements, llm_ok, llm_error = anonymize_text(
        text, segments, _progress_callback(progress, 5, 67, "Обезличивание текста"))
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_pdf_file(
        input_path, output_path, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Перерисовка документа"))
    report(98, "Сохранение")
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
    report(100, "Готово")
    result: Dict[str, Any] = {"preview_type": "pdf", "preview": preview,
                              "llm_used": llm_ok, **stats}
    if warning:
        result["warning"] = warning
    return result


def process_excel(input_path: Path, output_path: Path,
                  progress: Any = None) -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Извлечение текста")
    text, segments = extract_excel_text(input_path)
    report(3, "Загрузка модели")
    _prepare_llm(report)
    report(5, "Поиск персональных данных")
    replacements, llm_ok, llm_error = anonymize_text(
        text, segments, _progress_callback(progress, 5, 67, "Обезличивание текста"))
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_excel_file(
        input_path, output_path, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Перерисовка документа"))
    report(98, "Сохранение")
    result: Dict[str, Any] = {"preview_type": "excel", "preview": stats.pop("preview_rows", []),
                              "llm_used": llm_ok, **stats}
    return result
