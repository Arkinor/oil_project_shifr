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

from config import (IMAGE_EXTS, MASK_LOG_FILE, OUTPUT_DIR, STATE_DIR,
                    UPLOAD_DIR, load_settings, logger)


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


def _atomic_write_json(path: Path, data: Dict[str, Any],
                       indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent),
                       encoding="utf-8")
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
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
    owner = str(job.get("user") or "")
    source_name = str(job.get("filename") or in_path.name)
    with PROCESS_LOCK:
        _update_job(job_id, status="processing", progress=1,
                    stage="Подготовка документа", started=started)

        def report(progress: int, stage: str) -> None:
            _update_job(job_id, progress=max(0, min(99, int(progress))),
                        stage=stage, elapsed=round(time.time() - started, 1))

        try:
            logger.info(f"PROCESS_START | job={job_id} | {in_path.name} | ext={ext}")
            if ext == ".pdf":
                result = process_pdf(in_path, out_path, progress=report,
                                     user=owner, filename=source_name)
            elif ext in IMAGE_EXTS:
                result = process_image(in_path, out_path, progress=report,
                                       user=owner, filename=source_name)
            elif ext in (".docx", ".doc"):
                result = (process_doc if ext == ".doc" else process_docx)(
                    in_path, out_path, progress=report,
                    user=owner, filename=source_name)
            else:
                result = process_excel(in_path, out_path, progress=report,
                                       user=owner, filename=source_name)
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
# 1b. ЖУРНАЛ ОБЕЗЛИЧИВАНИЯ
# ================================================================
# data/masking_log.json — человекочитаемый журнал:
#   { "<логин>": { "<имя файла>": [ {"time", "page", "original",
#                                    "replacement", "type", "status"}, ... ] } }
# Пишется по мере обработки фрагментов, чтобы было видно, что именно
# заменяется, даже если документ обработается не полностью. Одинаковые пары
# повторно не добавляются, поэтому журнал можно безопасно дополнять.
# status = "принято" — пара согласована моделью; "не найдено в файле" —
# значение так и не применилось (это ошибка обезличивания, её видно сразу).
MASK_LOG_LOCK = threading.Lock()


def _load_mask_log() -> Dict[str, Any]:
    if not MASK_LOG_FILE.exists():
        return {}
    try:
        data = json.loads(MASK_LOG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"MASK_LOG | чтение {MASK_LOG_FILE.name}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _append_mask_log(user: str, filename: str, items: List[Dict[str, str]],
                     page: str = "") -> None:
    """Дописывает найденные пары в data/masking_log.json.

    Ключи — логин и исходное имя файла. Сбой журнала не должен прерывать
    обезличивание, поэтому все ошибки только логируются.
    """
    if not user or not filename or not items:
        return
    stamp = datetime_cls.now().isoformat(timespec="seconds")
    try:
        with MASK_LOG_LOCK:
            data = _load_mask_log()
            files = data.get(user)
            if not isinstance(files, dict):
                files = {}
                data[user] = files
            rows = files.get(filename)
            if not isinstance(rows, list):
                rows = []
                files[filename] = rows
            seen = {(str(row.get("original")), str(row.get("replacement")),
                     str(row.get("status") or ""))
                    for row in rows if isinstance(row, dict)}
            added = 0
            for item in items:
                original = str(item.get("original") or "")
                replacement = str(item.get("replacement") or "")
                status = str(item.get("status") or "принято")
                if not original or not replacement:
                    continue
                key = (original, replacement, status)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "time": stamp,
                    "page": page,
                    "original": original,
                    "replacement": replacement,
                    "type": str(item.get("type") or ""),
                    "source": str(item.get("source") or "llm"),
                    "status": status,
                })
                added += 1
            if added:
                _atomic_write_json(MASK_LOG_FILE, data, indent=2)
                logger.info(f"MASK_LOG | {user} | {filename} | +{added} замен")
    except Exception as e:
        logger.warning(f"MASK_LOG | запись {filename}: {e}")


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


_QUOTE_MARKS = "\u00ab\u00bb\u201e\u201c\u201d\u2018\u2019\"'`"


def _mask_key(value: Any) -> str:
    """Ключ, по которому значение ищется в заменах: без кавычек и регистра.

    Модель может вернуть «Черноземье», а в документе стоять \"Черноземье\"
    или Черноземье без кавычек. Шаблон поиска кавычки уже допускает, а ключ
    замены раньше требовал их точного совпадения — и замена молча не
    применялась. Для названий из нескольких слов это критично: кавычки стоят
    внутри значения, поэтому _clean_value их не снимает.
    """
    text = _clean_value(value).lower().replace("ё", "е")
    for ch in _QUOTE_MARKS:
        text = text.replace(ch, "")
    return re.sub(r"\s+", " ", text).strip()


def _flex_pattern(value: str) -> str | None:
    """Гибкий шаблон: любые пробелы/переносы, кавычки, тире, ё/е."""
    v = _clean_value(value)
    if not v:
        return None
    v = (v.replace("«", '"').replace("»", '"').replace("„", '"')
         .replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'"))
    parts: List[str] = []
    # «М. В.» и «М.В.» — одно и то же: склеиваем соседние инициалы в один токен.
    tokens: List[str] = []
    for tok in v.split(" "):
        if not tok:
            continue
        if (tokens
                and re.fullmatch(r"[А-ЯЁA-Z]\.?", tok, re.I)
                and re.fullmatch(r"[А-ЯЁA-Z](?:\.[А-ЯЁA-Z])*\.?",
                                 tokens[-1], re.I)):
            tokens[-1] += tok
        else:
            tokens.append(tok)
    for tok in tokens:
        # Инициалы: «М.В.» и «М. В.» — один и тот же человек.
        if re.fullmatch(r"[А-ЯЁA-Z](?:\.[А-ЯЁA-Z])*\.?", tok, re.I):
            letters = re.findall(r"[А-ЯЁA-Z]", tok, re.I)
            e = r"\.?\s*".join(re.escape(ch) for ch in letters) + r"\.?"
            parts.append(e)
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
    # Подчёркивание — разделитель, а не буква: «___Иванов И.И.» должно
    # находиться. Внутри слов границы по-прежнему запрещены.
    return r"(?<![^\W_])" + r"\s+".join(wrapped) + r"(?![^\W_])"


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
    hits считает, сколько раз замена реально применилась: по нему видно, какие
    согласованные значения в файле так и не нашлись.
    """

    def __init__(self) -> None:
        self.exact: Dict[str, str] = {}
        self.hits: Dict[str, int] = {}

    @staticmethod
    def _key(original: str) -> str:
        return _mask_key(original)

    def set_exact(self, original: str, fake: str) -> None:
        original = str(original or "").strip()
        fake = str(fake or "").strip()
        key = self._key(original)
        if key and fake:
            self.exact.setdefault(key, fake)

    def _find(self, original: str) -> str:
        key = self._key(original)
        return self.exact.get(key, "") if key else ""

    def _count(self, original: str) -> None:
        key = self._key(original)
        if key:
            self.hits[key] = self.hits.get(key, 0) + 1

    def lookup(self, original: str) -> str:
        """Замена для значения; попадание засчитывается как применённое."""
        fake = self._find(original)
        if fake:
            self._count(original)
        return fake

    def peek(self, original: str) -> str:
        """Замена без зачёта попадания — для предварительного поиска."""
        return self._find(original)

    def mark(self, original: str) -> None:
        """Зачитывает замену, когда она действительно записана в файл."""
        self._count(original)

    def get(self, kind: str, original: str) -> str:
        original = str(original or "")
        fake = self._find(original)
        if not fake:
            return original
        if re.search(r"[А-Яа-яЁёA-Za-z]", original):
            fake = _match_quotes(_apply_case(fake, original), original)
        if fake == original:
            return original
        self._count(original)
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
# LM Studio (как и OpenAI) понимает response_format.type = "json_schema", но
# отклоняет устаревший "json_object" с HTTP 400. Схема дублирует формат, который
# разбирает _parse_replacements; strict-режим не даёт модели добавить лишние поля.
_REPLACEMENTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "replacement": {"type": "string"},
                    "type": {"type": "string"},
                },
                "required": ["original", "replacement", "type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["replacements"],
    "additionalProperties": False,
}
_JSON_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "pii_replacements",
        "strict": True,
        "schema": _REPLACEMENTS_SCHEMA,
    },
}
# JSON-режим и успешный прогрев хранятся не глобально, а для пары
# «сервер + модель»: при смене модели в settings.json не нужно перезапускать
# процесс, а отказ одной модели не выключает JSON-режим для другой.
_JSON_MODE_OFF: set[Tuple[str, str]] = set()
_JSON_MODE_LOCK = threading.Lock()
_WARMED_MODELS: set[Tuple[str, str]] = set()
_WARM_ERRORS: Dict[Tuple[str, str], str] = {}
_WARM_LOCK = threading.Lock()


def _llm_key(settings: Dict[str, Any]) -> Tuple[str, str]:
    return (str(settings.get("lm_studio_url") or "").rstrip("/"),
            str(settings.get("model") or ""))


def _json_mode_enabled(settings: Dict[str, Any]) -> bool:
    with _JSON_MODE_LOCK:
        return _llm_key(settings) not in _JSON_MODE_OFF


def _disable_json_mode(key: Tuple[str, str]) -> None:
    with _JSON_MODE_LOCK:
        first = key not in _JSON_MODE_OFF
        _JSON_MODE_OFF.add(key)
    if first:
        logger.info(f"LLM: {key[1] or 'модель'} не поддерживает JSON-режим — "
                    "дальше обычный ответ без response_format")


def _model_is_warm(settings: Dict[str, Any]) -> bool:
    with _WARM_LOCK:
        return _llm_key(settings) in _WARMED_MODELS


def _mark_model_warm(settings: Dict[str, Any]) -> None:
    key = _llm_key(settings)
    with _WARM_LOCK:
        _WARMED_MODELS.add(key)
        _WARM_ERRORS.pop(key, None)


def _set_warm_error(settings: Dict[str, Any], text: str) -> None:
    with _WARM_LOCK:
        _WARM_ERRORS[_llm_key(settings)] = str(text or "")[:500]


def _warm_error(settings: Dict[str, Any]) -> str:
    with _WARM_LOCK:
        return _WARM_ERRORS.get(_llm_key(settings), "")


def _forget_model_warm(key: Tuple[str, str]) -> None:
    with _WARM_LOCK:
        _WARMED_MODELS.discard(key)


def get_llm(settings: Dict[str, Any] | None = None, json_mode: bool = False):
    s = settings or load_settings()
    kwargs: Dict[str, Any] = {}
    if json_mode and _json_mode_enabled(s):
        kwargs["model_kwargs"] = {"response_format": _JSON_RESPONSE_FORMAT}
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
    if not wanted:
        return True
    for item in items:
        if str(item.get("id") or "").lower() != wanted:
            continue
        mtype = str(item.get("type") or "").lower()
        if "embed" in mtype:
            logger.error(f"LLM: {s.get('model')} — модель эмбеддингов, "
                         "для обезличивания нужна чат-модель")
            return False
        return True
    logger.error(f"LLM: модель {s.get('model')} не найдена в LM Studio")
    return False


def available_models(settings: Dict[str, Any] | None = None) -> List[str]:
    """ID моделей, которые LM Studio показывает в своём списке."""
    s = settings or load_settings()
    return [str(item.get("id")) for item in _lm_models(s) if item.get("id")]


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
    """Дожидается загрузки модели и проверяет её пробным запросом.

    Состояние loaded само по себе не доказательство: LM Studio могла выгрузить
    модель. Успешный прогрев запоминается для пары «сервер + модель», поэтому
    повторно на каждый документ пробный запрос не уходит.
    """
    s = settings or load_settings()
    if _model_is_warm(s):
        return True
    state, _ = model_state(s)
    if state != "loaded":
        _try_lm_load(s)
    timeout = max(30, int(s.get("model_load_timeout", 600) or 600))
    # Для пробы хватает короткого ответа: не упираемся в max_tokens и лимиты
    # конкретной модели, если в settings.json стоит слишком большое значение.
    probe_tokens = min(64, max(8, int(s.get("max_tokens", 4096) or 4096)))
    llm = get_llm({**s, "request_timeout": timeout, "max_tokens": probe_tokens})
    if llm is None:
        _set_warm_error(s, "не удалось создать клиент ChatOpenAI "
                           "(проверьте пакет langchain-openai)")
        return False
    deadline = time.time() + timeout
    attempt = 0
    last = ""
    # Если состояние врало про loaded, после первой ошибки попросим загрузку.
    load_retry = state == "loaded"
    while True:
        attempt += 1
        if report:
            report(1, "Загрузка модели в LM Studio" if attempt == 1
                   else f"Загрузка модели в LM Studio (попытка {attempt})")
        try:
            llm.invoke([("system", "Ответь одним словом."), ("human", "ping")])
            _mark_model_warm(s)
            logger.info(f"LLM готова: {s.get('model')} (попытка {attempt})")
            return True
        except Exception as e:
            last = _llm_error_text(e)
            logger.warning(f"LLM загрузка, попытка {attempt}: {last[:300]}")
            status = _http_status_code(e)
            # Переполнение контекста и «слишком большой» запрос — не загрузка:
            # повтор не поможет, причину показываем сразу.
            context_error = any(k in last.lower() for k in ("context", "too large"))
            retryable = ((_is_model_loading_error(last) and not context_error)
                         or status in (408, 429)
                         or 500 <= status < 600)
            if not retryable:
                # 400/401/403/404/422 и т.п. повтором не лечатся: не ждём
                # model_load_timeout впустую.
                _set_warm_error(s, last)
                logger.error(f"LLM не готова: {last[:300]}")
                return False
            if load_retry:
                load_retry = False
                _try_lm_load(s)
            if time.time() >= deadline:
                _set_warm_error(s, f"не загрузилась за {timeout} с: {last}")
                logger.error(f"LLM не загрузилась за {timeout} с: {last[:300]}")
                return False
            time.sleep(min(15, 2 * attempt))


def _is_model_loading_error(message: str) -> bool:
    m = str(message or "").lower().replace("_", " ")
    return any(k in m for k in ("loading", "not loaded", "unloaded", "no model",
                                "no models", "model is not", "not ready",
                                "failed to load", "not running", "timed out",
                                "timeout", "503", "connection", "refused",
                                "unreachable", "context length", "context window",
                                "too large"))


def _prepare_llm(report: Any = None) -> None:
    s = load_settings()
    if not warm_up_llm(s, report=report):
        detail = _warm_error(s)
        where = f"{s.get('lm_studio_url')} / {s.get('model')}"
        message = f"Модель в LM Studio не загрузилась ({where}) — обработка отменена"
        if detail:
            message += f": {detail[:300]}"
        raise RuntimeError(message)


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
    "ФИО в верхнем регистре, в кавычках, латиницей; ФИО в подписях и расшифровках, "
    "в том числе сразу после линии подчёркивания («___Иванов И.И.») и рядом с «М.П.»;\n"
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
    "краткое «Фамилия И.О.», отдельную фамилию, полное и краткое название, "
    "адрес и его части, сумму цифрами и прописью.\n"
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


# Подписи полей, которые модель иногда захватывает вместе со значением.
# Список закрытый: произвольные слова и юридические формы (ООО, АО, ИП)
# не отрезаются, иначе можно испортить название организации.
_FIELD_LABELS = (
    "инн/кпп", "огрн/инн", "огрнип", "огрн", "инн", "кпп", "бик", "снилс",
    "расчетный счет", "корреспондентский счет", "корр. счет", "корсчет",
    "банк получателя",
    "паспорт гражданина рф", "паспорт",
    "кем выдан", "дата выдачи", "код подразделения",
    "адрес для корреспонденции", "юридический адрес", "почтовый адрес", "адрес",
    "р/сч", "р/с", "к/сч", "к/с", "счет",
    "серия", "номер документа", "номер",
    "телефон", "тел.", "тел", "моб.", "моб", "факс",
    "e-mail", "email", "сайт", "telegram", "whatsapp",
    "сумма ндс", "сумма налога", "сумма", "стоимость", "цена", "ндс", "ндфл",
    "дата документа", "дата рождения", "дата поставки", "дата оплаты", "дата",
    "подпись", "№", "#",
)
_FIELD_LABELS = tuple(sorted(_FIELD_LABELS, key=len, reverse=True))
# У этих подписей после отрезания остаётся не значение, а описание:
# «дата договора 01.01.2020», «номер договора 15», «адрес места жительства».
# Их отрезаем только если остаток действительно похож на значение (цифра,
# «+», «@») или подпись отделена двоеточием. Иначе меняем строку целиком.
_WEAK_LABELS = frozenset((
    "дата", "дата документа", "дата рождения", "дата поставки", "дата оплаты",
    "дата выдачи", "номер", "номер документа", "серия", "счет",
    "сумма", "сумма ндс", "сумма налога", "стоимость", "цена", "ндс", "ндфл",
    "адрес", "юридический адрес", "почтовый адрес", "адрес для корреспонденции",
    "подпись", "код подразделения",
))


def _looks_like_value(core: str) -> bool:
    head = core.lstrip()
    return bool(head) and (head[0].isdigit() or head[0] in "+@" or head[0] == "(")
# Роли и «банк» отрезаем только при явном двоеточии: «Поставщик: ООО …» —
# подпись, а «Поставщик Сервис» — уже название организации.
_COLON_LABELS = tuple(sorted(
    ("поставщик", "покупатель", "заказчик", "исполнитель",
     "получатель", "плательщик", "банк"), key=len, reverse=True))
_FIELD_SEP = " \t\u00a0:№#."


def _match_field_label(raw: str, low: str, labels: Tuple[str, ...],
                       require_colon: bool) -> Tuple[str, str]:
    for label in labels:
        if not low.startswith(label):
            continue
        rest = raw[len(label):]
        if require_colon:
            after = rest.lstrip(" \t\u00a0")
            if not after.startswith(":"):
                continue
            core = after[1:].lstrip(" \t\u00a0")
        else:
            # У «№» и «#» значение может идти сразу: «№15».
            if (label not in ("№", "#") and rest
                    and rest[0] not in _FIELD_SEP):
                continue
            core = rest.lstrip(_FIELD_SEP)
            if (label in _WEAK_LABELS and not _looks_like_value(core)
                    and not rest.lstrip(" \t\u00a0").startswith(":")):
                continue
        if core and not _is_bad_value(core):
            return raw[:len(label)], core
    return "", raw


def _split_field_label(value: str) -> Tuple[str, str]:
    """Отделяет известную подпись поля: «ИНН 7701234567» -> («ИНН», «7701234567»)."""
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    low = raw.lower().replace("ё", "е")
    label, core = _match_field_label(raw, low, _FIELD_LABELS, False)
    if label:
        return label, core
    return _match_field_label(raw, low, _COLON_LABELS, True)


def _replacement_keeps_label(replacement: str, label: str) -> bool:
    """Начинается ли замена с той же подписи (тогда меняем строку целиком)."""
    head = _clean_value(replacement).lower().replace("ё", "е")
    label = label.lower().replace("ё", "е")
    if not head:
        return False
    if head.startswith(label):
        return True
    if label.endswith(".") and head.startswith(label[:-1]):
        return True
    if "-" in label and head.replace("-", "").startswith(label.replace("-", "")):
        return True
    first = re.split(r"[\s/]+", label, maxsplit=1)[0]
    return len(first) >= 3 and head.startswith(first)


def _locate_value(text: str, value: str, replacement: str = "") -> str:
    """Возвращает реальную подстроку документа для строки от модели.

    Если модель захватила известную подпись поля («ИНН 7701234567»), подпись
    отрезается: заменяется только значение. Если замена сама начинается с этой
    подписи, значит это часть названия/значения — берём строку целиком.
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

    label, core = _split_field_label(cleaned or value)
    # Слишком короткое значение после подписи («Номер 15») не отрезаем:
    # иначе «15» начнёт заменяться по всему документу.
    if (label and core and len(core) >= 3
            and not _replacement_keeps_label(replacement, label)):
        found = find(core)
        if found:
            return found

    for candidate in (value, cleaned, value.rstrip(".,;:!?»\"'"),
                      cleaned.rstrip(".,;:!?»\"'")):
        found = find(candidate)
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
    missing: List[str] = []
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
            if original:
                missing.append(original)
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
        sample = "; ".join(f"«{_clean_value(v)[:60]}»" for v in missing[:5])
        if len(missing) > 5:
            sample += f" и ещё {len(missing) - 5}"
        logger.warning(f"LLM разбор: принято {len(accepted)}, нет в тексте {not_found}, "
                       f"непригодно {bad}, без замены {len(invalid)}"
                       + (f" | модель вернула, но в тексте не найдено: {sample}"
                          if sample else ""))
    return accepted, invalid


def _invoke_uses_json_mode(llm: Any) -> bool:
    kwargs = getattr(llm, "model_kwargs", None) or {}
    return bool(kwargs.get("response_format"))


def _http_status_code(exc: BaseException) -> int:
    for source in (exc, getattr(exc, "response", None)):
        code = getattr(source, "status_code", None)
        try:
            if code is not None:
                return int(code)
        except (TypeError, ValueError):
            continue
    return 0


def _llm_error_text(exc: BaseException) -> str:
    """Текст ошибки вместе с телом ответа сервера.

    str() у ошибок OpenAI SDK содержит только «Error code: 400», поэтому
    причина (например, отклонённый response_format) без тела ответа теряется.
    """
    text = str(exc).strip()
    details: List[str] = []
    body = getattr(exc, "body", None)
    if body:
        if isinstance(body, str):
            details.append(body)
        else:
            try:
                details.append(json.dumps(body, ensure_ascii=False))
            except (TypeError, ValueError):
                details.append(str(body))
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            details.append(response.text)
        except Exception:
            pass
    for detail in details:
        detail = str(detail or "").strip()
        if detail and detail not in text:
            text = f"{text}: {detail}" if text else detail
            break
    return text[:2000]


def _invoke(llm, messages: List[Any], retries: int,
            json_key: Tuple[str, str] | None = None) -> Tuple[str | None, str, bool]:
    """Запрос к модели. Третий элемент — «нужно повторить без json-режима»."""
    last = ""
    key = json_key or ("", "")
    for attempt in range(2 + max(0, retries)):
        try:
            resp = llm.invoke(messages)
            return (resp.content if hasattr(resp, "content") else str(resp)), "", False
        except Exception as e:
            last = _llm_error_text(e)
            status = _http_status_code(e)
            logger.warning(f"LLM запрос: {last[:300]}")
            # Сервер отклонил сам формат ответа — это не сбой модели и не повод
            # повторять тот же запрос: переключаемся на обычный текстовый ответ.
            format_rejected = any(key_part in last for key_part in
                                  ("response_format", "json_object", "json_schema"))
            if format_rejected or (_invoke_uses_json_mode(llm) and status in (400, 415, 422)):
                _disable_json_mode(key)
                return None, last, True
            if _is_model_loading_error(last) and not any(
                    part in last.lower() for part in ("context", "too large")):
                # Модель могла быть выгружена после прогрева: в следующий раз
                # warm_up_llm сделает пробный запрос заново.
                _forget_model_warm(key)
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
    key = _llm_key(settings)
    raw, _, plain = _invoke(llm, [("system", ANON_SYSTEM), ("human", prompt)],
                            retries, key)
    if plain:
        llm = get_llm(settings, json_mode=False) or llm
        raw, _, _ = _invoke(llm, [("system", ANON_SYSTEM), ("human", prompt)],
                            retries, key)
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
    key = _llm_key(settings)
    raw, error, plain = _invoke(llm, messages, retries, key)
    if plain:
        llm = get_llm(settings, json_mode=False) or llm
        raw, error, _ = _invoke(llm, messages, retries, key)
    had_response = raw is not None
    parsed = _parse_replacements(raw, text) if raw else None
    if parsed is None and raw is not None:
        raw, error, _ = _invoke(
            llm, [("system", ANON_SYSTEM),
                  ("human", prompt + "\n\nОтвет — только JSON-объект, без пояснений.")],
            0, key)
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
                   progress: Any = None,
                   user: str = "",
                   filename: str = "") -> Tuple[List[Dict[str, str]], bool, str]:
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
        new_items: List[Dict[str, str]] = []
        for item in got:
            key = item["original"].lower()
            if key in known:
                continue
            known[key] = (item["original"], item["replacement"])
            replacements.append(item)
            new_items.append(item)
            added += 1
        log_items: List[Dict[str, str]] = list(new_items)
        for item in new_items:
            for short, short_fake in _signature_pairs(
                    item["original"], item["replacement"],
                    str(item.get("type") or "")):
                log_items.append({"original": short, "replacement": short_fake,
                                  "type": str(item.get("type") or ""),
                                  "source": "signature"})
        _append_mask_log(user, filename, log_items, page=chunk["label"])
        logger.info(f"LLM | {chunk['label']} | символов {len(chunk['text'])} | "
                    f"новых замен {added} | пропущено дублей {len(got) - added}")
    if progress:
        progress(total, total)
    if not replacements:
        # Пустой ответ модели — не успех: файл ушёл бы наружу без единой
        # замены, а пользователь увидел бы «Готово». Отказ безопаснее.
        logger.error(f"LLM | {filename or 'документ'}: модель не нашла ни одной "
                     f"замены ({len(text)} символов, {total} фрагментов)")
        return [], False, "no_replacements"
    logger.info(f"LLM | готово: {len(replacements)} замен, обработано {processed} символов")
    return replacements, True, ""


def _llm_failure_message(error: str) -> str:
    messages = {
        "disabled": "LLM отключена — обезличивание невозможно",
        "llm_init": "LLM недоступна — обработка отменена",
        "empty": "В документе не найден текст для обезличивания",
        "invalid_json": "LLM вернула некорректный ответ — обработка отменена",
        "bad_replacement": "LLM не предложила замену для части данных — обработка отменена",
        "no_replacements": "Модель не нашла ни одного значения для замены — "
                           "обезличивание не выполнено, файл не выдан. "
                           "Проверьте документ и повторите обработку",
    }
    if error in messages:
        return messages[error]
    lowered = str(error or "").lower()
    if any(key in lowered for key in ("error code: 4", "response_format",
                                      "json_object", "json_schema")):
        return ("LLM отклонила запрос (ошибка параметров) — обработка отменена"
                + (f": {error[:200]}" if error else ""))
    return "LLM не отвечает — обработка отменена" + (f": {error[:200]}" if error else "")


# Окончания фамилий, которые можно отрезать, чтобы получить основу:
# «Капишниковой» -> «Капишников», «Зинурова» -> «Зинуров».
_SURNAME_ENDINGS = (
    "ого", "его", "ому", "ему", "ыми", "ими", "ой", "ей", "ый", "ий",
    "ом", "ем", "ым", "им", "ам", "ям", "ах", "ях",
    "а", "я", "у", "ю", "ы", "и", "е",
)


def _surname_stem(surname: str) -> str:
    value = _clean_value(surname)
    low = value.lower().replace("ё", "е")
    for end in _SURNAME_ENDINGS:
        if low.endswith(end) and len(value) - len(end) >= 4:
            return value[:len(value) - len(end)]
    return value


def _name_initials(parts: List[str]) -> str:
    letters = [p[0].upper() for p in parts[:2] if p and p[0].isalpha()]
    return ".".join(letters) + "." if len(letters) == 2 else ""


def _signature_pairs(original: str, replacement: str,
                     kind: str = "") -> List[Tuple[str, str]]:
    """Короткие ФИО для подписей: «Капишникова М.В.» из полного ФИО.

    Модель видит полное ФИО в тексте, но может не вернуть его краткую форму
    в блоке подписей. Пары строятся из уже согласованной замены: основа
    фамилии берётся из original/replacement, инициалы — из имени и отчества.
    """
    if not any(tag in str(kind).lower()
               for tag in ("фио", "физ", "person", "name")):
        return []
    orig = _clean_value(original).split()
    fake = _clean_value(replacement).split()
    if len(orig) < 3 or len(fake) < 3:
        return []
    o_init = _name_initials(orig[1:])
    f_init = _name_initials(fake[1:])
    if not o_init or not f_init:
        return []
    o_stem = _surname_stem(orig[0])
    f_stem = _surname_stem(fake[0])
    if not o_stem or not f_stem:
        return []
    # Женские формы идут первыми: подпись «Капишникова М.В.» должна получить
    # женскую замену. Мужская форма — основа без окончания.
    forms = [
        (f"{o_stem}а {o_init}", f"{f_stem}а {f_init}"),
        (f"{o_stem}ая {o_init}", f"{f_stem}а {f_init}"),
        (f"{o_stem} {o_init}", f"{f_stem} {f_init}"),
        (f"{orig[0]} {o_init}", f"{fake[0]} {f_init}"),
    ]
    result: List[Tuple[str, str]] = []
    seen = set()
    for short, short_fake in forms:
        if short not in seen:
            seen.add(short)
            result.append((short, short_fake))
    return result


def _not_applied(masker: Masker, replacements: Any) -> List[Dict[str, str]]:
    """Согласованные замены, которые в файле так и не применились.

    Если модель нашла значение, а в документе оно не заменилось, это ошибка
    обезличивания. Раньше она проходила молча: журнал показывал пару как
    принятую, а в файле оставался оригинал. Теперь такие значения видно
    и в логе, и в интерфейсе.
    """
    left: List[Dict[str, str]] = []
    hit_keys = [key for key, count in masker.hits.items() if count]
    for item in replacements or []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original") or "")
        keys = {_mask_key(original)}
        for short, _ in _signature_pairs(original,
                                         str(item.get("replacement") or ""),
                                         str(item.get("type") or "")):
            keys.add(_mask_key(short))
        keys.discard("")
        if not keys:
            continue
        if any(masker.hits.get(k) for k in keys):
            continue
        # Короткий вариант мог уйти в составе длинного: «Черноземье» внутри
        # «Общество с ограниченной ответственностью «Черноземье»».
        if any(len(k) >= 3 and k in hit for k in keys for hit in hit_keys):
            continue
        left.append(item)
    return left


def _unused_warning(left: List[Dict[str, str]], total: int,
                    filename: str) -> str:
    """Текст предупреждения о значениях, которых не оказалось в файле."""
    if not left:
        return ""
    shown = "; ".join(f"«{_clean_value(i.get('original'))[:70]}»" for i in left[:8])
    if len(left) > 8:
        shown += f" и ещё {len(left) - 8}"
    logger.warning(f"MASK | {filename}: не применено {len(left)} из {total}: {shown}")
    return (f"Не удалось заменить {len(left)} из {total} значений — "
            f"их нет в тексте файла: {shown}")


def _build_replacement_masker(replacements: Any) -> Masker:
    masker = Masker()
    for item in replacements or []:
        if isinstance(item, dict):
            original = item.get("original") or ""
            replacement = item.get("replacement") or ""
            masker.set_exact(original, replacement)
            for short, short_fake in _signature_pairs(
                    original, replacement, str(item.get("type") or "")):
                masker.set_exact(short, short_fake)
    return masker


def _replacement_extras(replacements: Any) -> List[Dict[str, str]]:
    extras: List[Dict[str, str]] = []
    for item in replacements or []:
        if not isinstance(item, dict):
            continue
        original = item.get("original") or ""
        if not original:
            continue
        kind = str(item.get("type") or "")
        extras.append({"text": original, "type": kind})
        for short, _ in _signature_pairs(original,
                                         item.get("replacement") or "", kind):
            extras.append({"text": short, "type": kind})
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
            try:
                # Адреса ссылок не видны в тексте страницы, но остаются в PDF.
                for link in page.get_links() or []:
                    uri = link.get("uri")
                    if isinstance(uri, str) and uri.strip():
                        extra.append(uri.strip())
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


def _mask_page_flow(page_spans: List[Dict[str, Any]], extras: Any,
                    masker: Masker) -> int:
    """Страховка для значений, разорванных между блоками по вертикали.

    _mask_block видит только один блок, _mask_page_rows — только одну строку.
    Если название организации перенесено на следующую строку и попало в
    отдельный блок (частая выгрузка из PDF и правовых систем), совпадения
    не находилось вовсе. Здесь строки страницы склеиваются сверху вниз, а
    замена возвращается в те span, где стоял оригинал.
    """
    import pymupdf
    items: List[Tuple[Dict[str, Any], Any]] = []
    for span in page_spans:
        if (span.get("_drop") or span.get("_new_text")
                or span.get("_merge_spans")):
            continue
        try:
            rect = pymupdf.Rect(span["bbox"])
        except Exception:
            continue
        if rect.is_empty or rect.height <= 0:
            continue
        items.append((span, rect))
    if len(items) < 2:
        return 0

    lines: List[Dict[str, Any]] = []
    for span, rect in sorted(items, key=lambda x: (x[1].y0, x[1].x0)):
        for line in lines:
            lr = line["rect"]
            overlap = min(lr.y1, rect.y1) - max(lr.y0, rect.y0)
            if overlap > 0.5 * min(lr.height, rect.height):
                line["items"].append((span, rect))
                line["rect"] = pymupdf.Rect(
                    min(lr.x0, rect.x0), min(lr.y0, rect.y0),
                    max(lr.x1, rect.x1), max(lr.y1, rect.y1))
                break
        else:
            lines.append({"items": [(span, rect)], "rect": pymupdf.Rect(rect)})
    lines.sort(key=lambda line: (line["rect"].y0, line["rect"].x0))

    # Колонки: строка с реквизитами справа не должна разрывать название,
    # перенесённое на следующую строку. Span прикрепляется к колонке, с
    # которой у него больше всего общего по горизонтали.
    columns: List[Dict[str, Any]] = []
    for li, line in enumerate(lines):
        for span, rect in sorted(line["items"], key=lambda x: x[1].x0):
            best = None
            best_overlap = 0.0
            for col in columns:
                overlap = min(col["x1"], rect.x1) - max(col["x0"], rect.x0)
                if overlap > best_overlap:
                    best, best_overlap = col, overlap
            if best is not None and best_overlap >= 0.5 * max(
                    1.0, min(best["x1"] - best["x0"], rect.width)):
                col = best
            else:
                col = {"items": [], "x0": rect.x0, "x1": rect.x1}
                columns.append(col)
            col["items"].append((span, li))
            col["x0"] = min(col["x0"], rect.x0)
            col["x1"] = max(col["x1"], rect.x1)

    count = 0
    for col in columns:
        count += _mask_column_flow(col["items"], extras, masker)
    return count


def _mask_column_flow(items: List[Tuple[Dict[str, Any], int]], extras: Any,
                      masker: Masker) -> int:
    """Склеивает колонку сверху вниз и применяет найденные замены."""
    chars: List[str] = []
    cmap: List[Any] = []
    flat: List[Dict[str, Any]] = []
    last = ""
    prev_line = None
    for span, line_index in items:
        text = span.get("text", "")
        if not text:
            continue
        if prev_line is not None and line_index != prev_line:
            chars.append("\n")
            cmap.append(None)
            last = "\n"
        prev_line = line_index
        si = len(flat)
        flat.append(span)
        if (chars and last and not last.isspace() and not text[0].isspace()
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
    if not joined.strip():
        return 0
    matches = iter_matches(joined, extras)
    if not matches:
        return 0

    edits: Dict[int, List[Tuple[int, int, str]]] = {}
    count = 0
    for start, end, kind, value in matches:
        pieces = _range_pieces(cmap, start, end)
        # Значение внутри одного span блок уже обработал.
        if len(pieces) < 2:
            continue
        fake = masker.get(kind, value)
        if fake == value:
            continue
        originals = [flat[si]["text"][a:b] for si, a, b in pieces]
        fakes = _distribute_replacement(fake, originals)
        changed = False
        for (si, a, b), old, fp in zip(pieces, originals, fakes):
            if fp is None or fp == old:
                continue
            edits.setdefault(si, []).append((a, b, fp))
            changed = True
        if changed:
            count += 1
    for si, changes in edits.items():
        span = flat[si]
        text = span["text"]
        for a, b, fp in sorted(changes, key=lambda x: -x[0]):
            text = text[:a] + fp + text[b:]
        if text != span["text"]:
            span["_new_text"] = text
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
        total += _mask_page_flow(page_spans, extras, masker)
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
    """Замена для числовой или датовой ячейки — только если её дала модель.

    Попадание засчитывается лишь после успешного преобразования замены:
    иначе значение, которое не удалось записать (не распарсилось число или
    дата), выглядело бы обезличенным и не попадало в список «не применено».
    """
    if isinstance(value, bool):
        return None

    def find(*candidates: str) -> Tuple[str, str]:
        for candidate in candidates:
            fake = masker.peek(candidate)
            if fake:
                return candidate, fake
        return "", ""

    if isinstance(value, (datetime_cls, date_cls)):
        is_datetime = isinstance(value, datetime_cls)
        candidate, fake = find(
            value.isoformat(sep=" ") if is_datetime else value.isoformat(),
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
            masker.mark(candidate)
            return parsed if is_datetime else parsed.date()
        return None

    if isinstance(value, (int, float)):
        text = str(value)
        decimal = str(value).replace(".", ",")
        candidate, fake = find(text, decimal,
                               f"{value:,}".replace(",", " "),
                               f"{value:,}".replace(",", "\u00a0"))
        if not fake:
            return None
        try:
            number = _to_number(fake, isinstance(value, int))
        except Exception:
            return None
        masker.mark(candidate)
        return number
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


def _cached_cell_value(cached_ws, cell) -> Any:
    """Вычисленное значение той же ячейки из книги с data_only=True."""
    if cached_ws is None:
        return None
    try:
        return cached_ws.cell(row=cell.row, column=cell.column).value
    except Exception:
        return None


def _cached_fake(masker: Masker, value: Any):
    """Замена для результата формулы — только если её дала модель."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        fake = masker.peek(value)
        if not fake:
            return None
        masker.mark(value)
        return fake
    return _numeric_fake(masker, value)


def mask_excel_file(input_path: Path, output_path: Path, extras: Any = (),
                    masker: Masker | None = None,
                    progress: Any = None) -> Dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(input_path)
    masker = masker or Masker()
    masked = 0
    # Результат формулы не виден в тексте формулы, но именно он попадает
    # в документ. Книга с data_only=True читается только если в файле есть
    # формулы, и даёт вычисленные значения для поиска замены.
    cached_wb: Any = None
    cached_tried = False
    cached_sheets: Dict[str, Any] = {}
    formula_replaced = 0
    formula_unchecked = 0

    def cached_sheet(title: str):
        nonlocal cached_wb, cached_tried
        if title in cached_sheets:
            return cached_sheets[title]
        if not cached_tried:
            cached_tried = True
            try:
                cached_wb = openpyxl.load_workbook(input_path, data_only=True)
            except Exception as e:
                logger.warning(f"XLSX cached values: {e}")
                cached_wb = None
        sheet = None
        if cached_wb is not None and title in cached_wb.sheetnames:
            try:
                sheet = cached_wb[title]
            except Exception:
                sheet = None
        cached_sheets[title] = sheet
        return sheet

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
                            # Если модель дала замену для вычисленного значения —
                            # ставим её вместо формулы: иначе результат с ПДн
                            # остался бы в книге и в кэше файла.
                            cached = _cached_cell_value(cached_sheet(ws.title), cell)
                            if cached is None:
                                formula_unchecked += 1
                            else:
                                fake_value = _cached_fake(masker, cached)
                                if fake_value is not None:
                                    cell.value = fake_value
                                    masked += 1
                                    formula_replaced += 1
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
        if cached_wb is not None:
            try:
                cached_wb.close()
            except Exception:
                pass
    stats: Dict[str, Any] = {"masked": masked, "pages": 1, "preview_rows": preview}
    if formula_replaced:
        logger.info(f"XLSX: {formula_replaced} формул заменены значениями — "
                    "результат вычислений содержал данные под замену")
    if formula_unchecked:
        # Книга не пересчитывалась (нет кэша): что именно считает формула,
        # неизвестно, поэтому честно предупреждаем, а не выдаём за проверенное.
        stats["warning"] = (f"{formula_unchecked} ячеек с формулами без вычисленного "
                            "значения — их результат не проверен")
    return stats

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


def _docx_root_namespaces(raw: bytes) -> Dict[str, str]:
    """Объявления xmlns:prefix из корневого тега исходной части документа."""
    try:
        head = raw[:16000].decode("utf-8", "ignore")
        match = re.search(r"<[A-Za-z_][^>]*>", head)
        if not match:
            return {}
        return dict(re.findall(r'xmlns:([A-Za-z0-9_.-]+)="([^"]+)"', match.group(0)))
    except Exception:
        return {}


def _docx_keep_namespaces(raw: bytes, data: bytes) -> bytes:
    """Возвращает в корневой тег объявления namespace, потерянные ElementTree.

    ET сериализует только префиксы, встречающиеся в именах элементов и
    атрибутов. В word/document.xml от Word префиксы w14/wp14/w15 объявлены
    только ради `mc:Ignorable`, поэтому после разбора и обратной сборки они
    исчезают, а `mc:Ignorable="w14 wp14"` остаётся — Word считает файл
    повреждённым. Здесь недостающие объявления дописываются обратно.
    """
    try:
        declared = _docx_root_namespaces(raw)
        if not declared:
            return data
        text = data.decode("utf-8")
        match = re.search(r"<[A-Za-z_][^>]*>", text)
        if not match:
            return data
        tag = match.group(0)
        present = set(re.findall(r'xmlns:([A-Za-z0-9_.-]+)=', tag))
        missing = [(prefix, uri) for prefix, uri in declared.items()
                   if prefix not in present]
        if not missing:
            return data
        extra = "".join(f' xmlns:{prefix}="{uri}"' for prefix, uri in missing)
        if tag.endswith("/>"):
            new_tag = tag[:-2] + extra + "/>"
        else:
            new_tag = tag[:-1] + extra + ">"
        return text.replace(tag, new_tag, 1).encode("utf-8")
    except Exception as e:
        logger.warning(f"DOCX namespaces: {e}")
        return data


def _docx_rel_targets(raw: bytes) -> List[str]:
    """Внешние адреса из *.rels (mailto:/URL)."""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    out: List[str] = []
    for el in root.iter():
        if not str(el.tag).endswith("Relationship"):
            continue
        if (el.get("TargetMode") or "").lower() != "external":
            continue
        target = el.get("Target") or ""
        if target:
            out.append(target)
    return out


def _docx_mask_rel_targets(raw: bytes, extras: Any, masker: Masker) -> Tuple[bytes, int]:
    """Маскирует внешние цели связей (гиперссылки, mailto:) в *.rels.

    Адрес ссылки не виден в тексте документа, но лежит в XML рядом с ним:
    `mailto:ivanov@example.ru` или URL с фамилией в пути. ElementTree здесь
    не используется — его сериализация меняет префикс пространства имён
    (`ns0:`), а менять нужно только значения атрибутов Target.
    """
    targets = _docx_rel_targets(raw)
    if not targets:
        return raw, 0
    from xml.sax.saxutils import escape as xml_escape

    def attr(value: str) -> str:
        return xml_escape(value, {'"': "&quot;"})

    text = raw.decode("utf-8", "ignore")
    masked = 0
    for target in targets:
        new = mask_text(target, extras, masker)
        if not new or new == target:
            continue
        needle = f'Target="{attr(target)}"'
        if needle in text:
            text = text.replace(needle, f'Target="{attr(new)}"', 1)
            masked += 1
    if not masked:
        return raw, 0
    return text.encode("utf-8"), masked


def _docx_paragraph_items(paragraph) -> List[Any]:
    """Элементы текста абзаца и разделители на месте w:br / w:cr / w:tab.

    Разрыв строки внутри абзаца нельзя просто выбрасывать: «Общество с» и
    «ограниченной ответственностью» склеивались в одно слово
    «сограниченной», модель не видела название организации, и оно оставалось
    в документе. Разделитель — обычная строка, элемент текста — элемент XML.
    """
    tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: List[Any] = []

    def walk(el) -> None:
        for child in el:
            if child.tag in (tag + "p", tag + "pPr", tag + "rPr"):
                # Вложенные абзацы (надписи) обходятся отдельно, а в
                # свойствах абзаца w:tab — это позиция табуляции, а не символ.
                continue
            if child.tag in (tag + "t", tag + "delText", tag + "instrText"):
                # w:delText — удалённый, но остающийся в файле текст (режим
                # исправлений), w:instrText — коды полей (в т.ч. mailto:).
                # И то и другое видно в XML, поэтому обезличивается наравне
                # с обычным текстом.
                out.append(child)
            elif child.tag in (tag + "br", tag + "cr"):
                out.append("\n")
            elif child.tag == tag + "tab":
                out.append(" ")
            else:
                walk(child)

    walk(paragraph)
    return out


def _docx_items_text(items: List[Any]) -> str:
    return "".join(item if isinstance(item, str) else (item.text or "")
                   for item in items)


def _docx_own_runs(paragraph) -> List[Any]:
    """Элементы текста этого абзаца, без вложенных абзацев (поля/надписи)."""
    tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: List[Any] = []

    def walk(el) -> None:
        for child in el:
            if child.tag == tag + "p":
                continue
            if child.tag in (tag + "t", tag + "delText", tag + "instrText"):
                out.append(child)
            else:
                walk(child)

    walk(paragraph)
    return out


def _docx_part_paragraphs(root) -> List[Any]:
    tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return [p for p in root.iter(tag + "p") if _docx_own_runs(p)]


def _mask_docx_paragraph(paragraph, extras: Any, masker: Masker) -> bool:
    items = _docx_paragraph_items(paragraph)
    runs = [item for item in items if not isinstance(item, str)]
    texts = [r.text or "" for r in runs]
    chars: List[str] = []
    cmap: List[Any] = []
    ri = 0
    for item in items:
        if isinstance(item, str):
            chars.append(item)
            cmap.append(None)
            continue
        for oi, ch in enumerate(texts[ri]):
            if ch in _ZERO_WIDTH:
                continue
            chars.append(_norm_char(ch))
            cmap.append((ri, oi))
        ri += 1
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
                    text = _docx_items_text(_docx_paragraph_items(p))
                    if text.strip():
                        lines.append(text)
                part_text = "\n".join(lines)
                if not part_text.strip():
                    continue
                segments.append({"start": position, "end": position + len(part_text),
                                 "label": _docx_part_label(name)})
                parts.append(part_text)
                position += len(part_text) + 1
            # Внешние адреса из word/_rels/*.rels не видны в тексте, но
            # остаются в файле. Отдаём их модели отдельным фрагментом, чтобы
            # она предложила для них замену.
            links: List[str] = []
            for name in z.namelist():
                if not (name.startswith("word/_rels/") and name.endswith(".rels")):
                    continue
                for target in _docx_rel_targets(z.read(name)):
                    low = target.lower()
                    if low.startswith(("mailto:", "http://", "https://", "ftp://")):
                        links.append(target)
            if links:
                part_text = "\n".join(dict.fromkeys(links))
                segments.append({"start": position, "end": position + len(part_text),
                                 "label": "адреса гиперссылок"})
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
                    data = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
                    changed_parts[name] = _docx_keep_namespaces(raw, data)
                if progress:
                    progress(index + 1, len(parts))
            # Внешние цели связей: mailto:/URL в word/_rels/*.rels. В тексте
            # документа их не видно, поэтому обычные проходы их не замечают.
            for name in zin.namelist():
                if not (name.startswith("word/_rels/") and name.endswith(".rels")):
                    continue
                data, rel_masked = _docx_mask_rel_targets(zin.read(name), extras, masker)
                if rel_masked:
                    changed_parts[name] = data
                    masked += rel_masked
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
                progress: Any = None, user: str = "",
                filename: str = "") -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Преобразование .doc")
    tmp = input_path.with_name(f"{input_path.stem}.converted.docx")
    try:
        _doc_to_docx(input_path, tmp)
        result = process_docx(tmp, output_path, progress=progress,
                              user=user, filename=filename)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    result["warning"] = ("Файл .doc преобразован в .docx"
                         + (f". {result['warning']}" if result.get("warning") else ""))
    return result


def process_docx(input_path: Path, output_path: Path,
                 progress: Any = None, user: str = "",
                 filename: str = "") -> Dict[str, Any]:
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
        text, segments, _progress_callback(progress, 5, 67, "Обезличивание текста"),
        user=user, filename=filename or input_path.name)
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_docx_file(
        input_path, output_path, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Правка документа"))
    left = _not_applied(masker, replacements)
    unused = _unused_warning(left, len(replacements), filename or input_path.name)
    if unused:
        _append_mask_log(user, filename or input_path.name,
                         [{**item, "status": "не найдено в файле"} for item in left],
                         page="проверка файла")
    report(100, "Готово")
    result: Dict[str, Any] = {"preview_type": "docx", **stats, "llm_used": llm_ok}
    if unused:
        result["warning"] = unused
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
                  progress: Any = None, user: str = "",
                  filename: str = "") -> Dict[str, Any]:
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
        text, _ocr_segments(lines), _progress_callback(progress, 8, 64, "Обезличивание текста"),
        user=user, filename=filename or input_path.name)
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_image_file(
        input_path, output_path, lines, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Перерисовка изображения"))
    left = _not_applied(masker, replacements)
    unused = _unused_warning(left, len(replacements), filename or input_path.name)
    if unused:
        _append_mask_log(user, filename or input_path.name,
                         [{**item, "status": "не найдено в файле"} for item in left],
                         page="проверка файла")
    report(100, "Готово")
    result: Dict[str, Any] = {"preview_type": "image", **stats, "llm_used": llm_ok}
    if unused:
        result["warning"] = unused
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
                progress: Any = None, user: str = "",
                filename: str = "") -> Dict[str, Any]:
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
        text, segments, _progress_callback(progress, 5, 67, "Обезличивание текста"),
        user=user, filename=filename or input_path.name)
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_pdf_file(
        input_path, output_path, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Перерисовка документа"))
    left = _not_applied(masker, replacements)
    unused = _unused_warning(left, len(replacements), filename or input_path.name)
    if unused:
        warning = f"{warning} {unused}".strip() if warning else unused
        _append_mask_log(user, filename or input_path.name,
                         [{**item, "status": "не найдено в файле"} for item in left],
                         page="проверка файла")
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
                  progress: Any = None, user: str = "",
                  filename: str = "") -> Dict[str, Any]:
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(max(0, min(99, int(pct))), stage)

    report(1, "Извлечение текста")
    text, segments = extract_excel_text(input_path)
    report(3, "Загрузка модели")
    _prepare_llm(report)
    report(5, "Поиск персональных данных")
    replacements, llm_ok, llm_error = anonymize_text(
        text, segments, _progress_callback(progress, 5, 67, "Обезличивание текста"),
        user=user, filename=filename or input_path.name)
    if not llm_ok:
        raise RuntimeError(_llm_failure_message(llm_error))
    report(73, "Применение замен")
    extras = _replacement_extras(replacements)
    masker = _build_replacement_masker(replacements)
    stats = mask_excel_file(
        input_path, output_path, extras, masker=masker,
        progress=_progress_callback(progress, 73, 24, "Перерисовка документа"))
    left = _not_applied(masker, replacements)
    unused = _unused_warning(left, len(replacements), filename or input_path.name)
    if unused:
        _append_mask_log(user, filename or input_path.name,
                         [{**item, "status": "не найдено в файле"} for item in left],
                         page="проверка файла")
    report(98, "Сохранение")
    stats_warning = str(stats.pop("warning", "") or "")
    result: Dict[str, Any] = {"preview_type": "excel", "preview": stats.pop("preview_rows", []),
                              "llm_used": llm_ok, **stats}
    warning = "; ".join(part for part in (stats_warning, unused) if part)
    if warning:
        result["warning"] = warning
    return result
