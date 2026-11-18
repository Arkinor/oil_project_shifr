"""main.py — веб-сервис обезличивания PDF/Word/Excel/изображений с локальной LLM.

Запуск:  python main.py
Сборка:  build.bat  ->  dist/PIIMasker.exe

Модули:
  config.py      — пути, константы, settings.json, логирование;
  auth.py        — пользователи и проверка X-Login/X-Password;
  processing.py  — задания, LM Studio, обезличивание и запись файлов;
  main.py        — HTTP API, трей и запуск сервера.

Контракт с фронтендом (не менять!):
  GET  /                     -> index.html
  POST /api/login            {login,password} -> {ok,login} | 401 "Неверный логин или пароль"
  GET  /api/me               (X-Login,X-Password) -> {login}
  GET  /api/health           -> {ok,model,lm_studio_url,llm_ok,rag,frozen,tray,users_count}
  POST /api/process          multipart file     -> {ok,job_id,filename,status,progress}
  GET  /api/jobs             (X-Login,X-Password) -> {ok,jobs:[...]}
  GET  /api/jobs/{job_id}    (X-Login,X-Password) -> {ok,status,progress,stage,...}
  GET  /api/download/{name}  (X-Login,X-Password) -> файл

Состояние заданий пишется в data/state/<user>/jobs/<job_id>.json и переживает
перезапуск сервера; исходники лежат в data/uploads/<user>/, результаты —
в data/outputs/<user>/.
"""
import logging
import os
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import processing
from auth import authenticate, ensure_users_file, get_current_user, load_users
from config import (APP_DIR, BASE_DIR, IMAGE_EXTS, LOG_FILE, OUTPUT_DIR,
                    SERVER_LOG_FILE, _LOG_FORMAT, ensure_settings, load_settings,
                    logger)
from processing import (check_llm, model_state, prune_history, recover_state,
                        user_outputs, user_uploads)


# ================================================================
# 1. HTTP API
# ================================================================
app = FastAPI(title="PII Masker")
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
    state, mtype = model_state(s)
    return {
        "ok": True,
        "model": s["model"],
        "model_state": state,
        "model_type": mtype,
        "lm_studio_url": s["lm_studio_url"],
        "llm_ok": check_llm(s),
        "rag": False,
        "frozen": bool(getattr(sys, "frozen", False)),
        "tray": TRAY_ICON is not None,
        "users_count": len(load_users()),
    }


def _safe_name(name: str) -> str:
    name = Path(name or "document").name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120] or "document"


@app.post("/api/process")
def process(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    s = load_settings()
    if not s.get("llm_enabled", True):
        raise HTTPException(400, "LLM отключена — обезличивание невозможно")
    if not check_llm(s):
        # Модель может быть не загружена — LM Studio загрузит её по первому
        # запросу. Отказ только если сервер недоступен или модели нет в списке.
        models = processing.available_models(s)
        hint = (f" Доступные модели: {', '.join(models[:10])}" if models else "")
        raise HTTPException(503, f"LM Studio недоступна по {s.get('lm_studio_url')} "
                                 f"или модель {s.get('model')} "
                                 f"не найдена — загрузка не начата.{hint}")
    original = _safe_name(file.filename or "document")
    ext = Path(original).suffix.lower()
    if ext not in (".pdf", ".xlsx", ".docx", ".doc", *IMAGE_EXTS):
        raise HTTPException(400, "Поддерживаются PDF, Word (.doc/.docx), Excel (.xlsx), "
                                 "PNG и JPG")

    max_mb = int(s.get("max_upload_mb", 50))
    content = file.file.read(max_mb * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > max_mb * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {max_mb} МБ")

    prune_history()
    uid = uuid.uuid4().hex[:12]
    job_id = uuid.uuid4().hex
    in_path = user_uploads(user) / f"{uid}_{original}"
    # .doc нельзя перезаписать на месте: результат всегда .docx.
    out_ext = ".docx" if ext == ".doc" else ext
    out_base = f"masked_{uid}_{Path(original).stem}{out_ext}"
    out_name = out_base
    out_path = user_outputs(user) / out_name
    in_path.write_bytes(content)

    job = {
        "id": job_id,
        "user": user,
        "filename": original,
        "size": len(content),
        "status": "queued",
        "progress": 0,
        "stage": "В очереди",
        "elapsed": 0,
        "error": "",
        "created": time.time(),
        "started": None,
        "finished": None,
        "download_name": out_base,
        "out_name": out_name,
        "result": None,
    }
    with processing.JOBS_LOCK:
        processing.JOBS[job_id] = job
    processing._save_job(job)

    threading.Thread(
        target=processing._run_job,
        args=(job_id, in_path, out_path, ext),
        daemon=True,
        name=f"job-{uid}",
    ).start()
    logger.info(f"QUEUE | user={user} | file={original} | job={job_id}")
    return {"ok": True, "job_id": job_id, "filename": original,
            "status": "queued", "progress": 0}


@app.get("/api/jobs")
def jobs(user: str = Depends(get_current_user)):
    return {"ok": True, "jobs": processing._list_jobs(user)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: str = Depends(get_current_user)):
    data = processing._find_job(user, job_id)
    if data is None:
        raise HTTPException(404, "Задание не найдено")
    return {"ok": True, **data}


@app.get("/api/download/{filename}")
def download(filename: str, user: str = Depends(get_current_user)):
    name = Path(filename).name
    if not processing._owns_output(user, name):
        raise HTTPException(404, "Файл не найден")
    folder = user_outputs(user).resolve()
    path = (folder / name).resolve()
    if folder != path.parent:
        raise HTTPException(400, "Недопустимое имя файла")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    media = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(Path(name).suffix.lower(), "application/octet-stream")
    logger.info(f"DOWNLOAD | {user} | {name}")
    return FileResponse(path, media_type=media, filename=name)


# ================================================================
# 2. ТРЕЙ
# ================================================================
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


# ================================================================
# 3. ЗАПУСК
# ================================================================
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
    root = logging.getLogger()
    try:
        path = str(SERVER_LOG_FILE)
        if any(getattr(h, "baseFilename", "") == path for h in root.handlers):
            return
        fh = logging.FileHandler(SERVER_LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(fh)
        root.setLevel(logging.INFO)
        logger.info(f"Логирование в файлы: {LOG_FILE.name}, {SERVER_LOG_FILE.name}")
    except Exception as e:
        logger.error(f"server.log: {e}")


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
    # log_config=None: uvicorn пишет в корневой логгер, т.е. в activity.log/server.log.
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


def run_server() -> None:
    ensure_settings()
    ensure_users_file()

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        if sys.stdout is None:
            sys.stdout = _NoConsoleStream()
        if sys.stderr is None:
            sys.stderr = _NoConsoleStream()
    _setup_file_logging()
    recover_state()

    s = load_settings()
    port = int(s.get("port", 9321))
    host = str(s.get("host", "0.0.0.0"))
    logger.info(f"START | host={host} | port={port} | model={s.get('model')} | "
                f"lm={s.get('lm_studio_url')} | llm_enabled={s.get('llm_enabled', True)}")
    logger.info(f"LLM state: {model_state(s)}")

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
