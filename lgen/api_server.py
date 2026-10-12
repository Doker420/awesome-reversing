"""
LGen REST API — FastAPI wrapper over ArenaGenerator
Запуск: uvicorn api_server:app --host 0.0.0.0 --port 8000

Аутентификация:
    Заголовок X-API-Key:
    - Персональный API ключ пользователя (lg_xxxx...) — списывает токены с баланса в БД
    - Мастер-ключ API_SECRET — доступ администратора (без ограничений)
"""

import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from arena_generator import ArenaGenerator
from database import Database
from config import (
    COST_IMAGE,
    COST_IMAGE_EDIT,
    COST_IMAGE_TO_VIDEO,
    COST_VIDEO,
    COST_LANDING,
    COST_TEXT,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Глобальный генератор + база данных + блокировка
# ─────────────────────────────────────────────────────────────
generator: ArenaGenerator = None
db: Database = None
gen_lock = asyncio.Lock()

API_SECRET = os.getenv("API_SECRET", "change_me_in_dotenv")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "generated")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global generator, db
    db = Database()
    await db.init_db()
    generator = ArenaGenerator()
    await generator.init_browser()
    logger.info("Browser & DB started ✅")
    yield
    await generator.close_browser()
    logger.info("Browser stopped")


app = FastAPI(
    title="LGen API",
    version="2.1.0",
    description=(
        "REST API для интеграции AI-генерации (Arena.ai) в сторонние сервисы и проекты.\n\n"
        "### Авторизация:\n"
        "Передавайте заголовок `X-API-Key`:\n"
        "- **Персональный ключ (`lg_...`)**: выдаётся прямо в Telegram боте в личном кабинете. "
        "Списывает токены с вашего баланса.\n"
        "- **Master ключ (`API_SECRET`)**: административный доступ без списания токенов.\n\n"
        "### Цены в токенах:\n"
        f"- Лендинг/Сайт: `{COST_LANDING:,}` токенов\n"
        f"- Изображение (генерация): `{COST_IMAGE:,}` токенов\n"
        f"- Изображение (редактирование): `{COST_IMAGE_EDIT:,}` токенов\n"
        f"- Видео (2 варианта): `{COST_VIDEO:,}` токенов\n"
        f"- Текст/Промпт: `{COST_TEXT:,}` токенов\n"
    ),
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────
# Auth dependency & Token deduction
# ─────────────────────────────────────────────────────────────
class AuthContext:
    def __init__(self, is_master: bool, user_id: Optional[int] = None, tokens: int = 0):
        self.is_master = is_master
        self.user_id = user_id
        self.tokens = tokens


async def verify_auth(x_api_key: str = Header(..., description="Ваш API ключ (lg_... или мастер-ключ)")) -> AuthContext:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is missing")

    # 1. Проверяем мастер-ключ
    if x_api_key == API_SECRET and API_SECRET != "change_me_in_dotenv":
        return AuthContext(is_master=True)

    # 2. Проверяем ключ пользователя в БД
    user = await db.get_user_by_api_key(x_api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key. Get your key in Telegram bot (/cabinet).")

    return AuthContext(
        is_master=False,
        user_id=user["user_id"],
        tokens=user.get("tokens", 0)
    )


async def charge_user_tokens(auth: AuthContext, cost: int, mode: str, prompt: str):
    """Списывает токены, если запрос не от мастер-ключа."""
    if auth.is_master:
        return
    
    success = await db.spend_tokens(auth.user_id, cost)
    if not success:
        user = await db.get_user(auth.user_id)
        current = user.get("tokens", 0) if user else 0
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient tokens. Required: {cost:,}, available: {current:,}. Buy more in Telegram bot."
        )


# ─────────────────────────────────────────────────────────────
# Схемы данных
# ─────────────────────────────────────────────────────────────
class GenerateCodeRequest(BaseModel):
    prompt: str = Field(..., min_length=5, example="Modern SaaS landing page for AI startup")
    output_dir: Optional[str] = Field(None, example="generated")

class GenerateTextRequest(BaseModel):
    prompt: str = Field(..., min_length=5, example="Write a product description for an AI platform")
    timeout: Optional[int] = Field(300, ge=30, le=900)

class GeneratePromptRequest(BaseModel):
    topic: str = Field(..., min_length=3, example="cyberpunk city at night")
    timeout: Optional[int] = Field(300, ge=30, le=900)

class GenerateImageRequest(BaseModel):
    prompt: str = Field(..., min_length=5, example="A futuristic city at night, neon lights, rain, 8k")
    output_dir: Optional[str] = Field(None, example="generated")
    timeout: Optional[int] = Field(300, ge=30, le=600)

class GenerateVideoRequest(BaseModel):
    prompt: str = Field(..., min_length=5, example="A drone flying over a mountain lake at sunset")
    output_dir: Optional[str] = Field(None, example="generated")
    timeout: Optional[int] = Field(1800, ge=60, le=2400)

class TextResponse(BaseModel):
    result: str
    tokens_used: int
    balance_left: Optional[int] = None
    elapsed_sec: float

class FilePathResponse(BaseModel):
    file: str
    tokens_used: int
    balance_left: Optional[int] = None
    elapsed_sec: float

class VideoResponse(BaseModel):
    files: list[str]
    tokens_used: int
    balance_left: Optional[int] = None
    elapsed_sec: float

class UserProfileResponse(BaseModel):
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    tokens: int
    api_key: str

class HealthResponse(BaseModel):
    status: str
    browser_ready: bool
    timestamp: float


# ─────────────────────────────────────────────────────────────
# Эндпоинты
# ─────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health():
    """Проверка статуса сервиса (без авторизации)."""
    return HealthResponse(
        status="ok",
        browser_ready=generator is not None and generator.browser is not None,
        timestamp=time.time(),
    )


@app.get("/me", response_model=UserProfileResponse, tags=["User"])
async def get_my_profile(auth: AuthContext = Depends(verify_auth)):
    """Получить баланс токенов и данные аккаунта по API ключу."""
    if auth.is_master:
        return UserProfileResponse(
            user_id=0,
            username="admin",
            first_name="Master Admin",
            tokens=999999999,
            api_key=API_SECRET,
        )
    user = await db.get_user(auth.user_id)
    return UserProfileResponse(
        user_id=user["user_id"],
        username=user.get("username"),
        first_name=user.get("first_name"),
        tokens=user.get("tokens", 0),
        api_key=user.get("api_key", ""),
    )


@app.post(
    "/generate/code",
    response_model=FilePathResponse,
    tags=["Generation"],
    summary="Генерация лендинга / кода",
    description=f"Списывает {COST_LANDING:,} токенов. Возвращает путь к ZIP/HTML архиву.",
)
async def generate_code(body: GenerateCodeRequest, auth: AuthContext = Depends(verify_auth)):
    await charge_user_tokens(auth, COST_LANDING, "landing", body.prompt)
    output_dir = body.output_dir or OUTPUT_DIR
    t0 = time.time()
    try:
        async with gen_lock:
            path = await generator.generate_code(body.prompt, output_dir=output_dir)
            if not auth.is_master:
                await db.add_generation_record(auth.user_id, body.prompt, path, "success", "landing", COST_LANDING)
    except Exception as e:
        logger.error(f"generate_code error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_LANDING)  # возврат при ошибке
            await db.add_generation_record(auth.user_id, body.prompt, "", "failed", "landing", 0)
        raise HTTPException(status_code=500, detail=str(e))

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return FilePathResponse(
        file=path,
        tokens_used=0 if auth.is_master else COST_LANDING,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.post(
    "/generate/landing",
    response_model=FilePathResponse,
    tags=["Generation"],
    summary="Алиас для /generate/code",
)
async def generate_landing(body: GenerateCodeRequest, auth: AuthContext = Depends(verify_auth)):
    return await generate_code(body, auth)


@app.post(
    "/generate/text",
    response_model=TextResponse,
    tags=["Generation"],
    summary="Генерация текста",
    description=f"Списывает {COST_TEXT:,} токенов. Возвращает сгенерированный текст.",
)
async def generate_text(body: GenerateTextRequest, auth: AuthContext = Depends(verify_auth)):
    await charge_user_tokens(auth, COST_TEXT, "text", body.prompt)
    t0 = time.time()
    try:
        async with gen_lock:
            result = await generator.generate_text(body.prompt, timeout=body.timeout)
            if not auth.is_master:
                await db.add_generation_record(auth.user_id, body.prompt, "", "success", "text", COST_TEXT)
    except Exception as e:
        logger.error(f"generate_text error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_TEXT)
            await db.add_generation_record(auth.user_id, body.prompt, "", "failed", "text", 0)
        raise HTTPException(status_code=500, detail=str(e))

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return TextResponse(
        result=result,
        tokens_used=0 if auth.is_master else COST_TEXT,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.post(
    "/generate/prompt",
    response_model=TextResponse,
    tags=["Generation"],
    summary="Генерация промта по теме",
    description=f"Списывает {COST_TEXT:,} токенов. Возвращает готовый профессиональный промпт.",
)
async def generate_prompt(body: GeneratePromptRequest, auth: AuthContext = Depends(verify_auth)):
    await charge_user_tokens(auth, COST_TEXT, "prompt", body.topic)
    t0 = time.time()
    try:
        async with gen_lock:
            result = await generator.generate_prompt(body.topic, timeout=body.timeout)
            if not auth.is_master:
                await db.add_generation_record(auth.user_id, body.topic, "", "success", "prompt", COST_TEXT)
    except Exception as e:
        logger.error(f"generate_prompt error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_TEXT)
            await db.add_generation_record(auth.user_id, body.topic, "", "failed", "prompt", 0)
        raise HTTPException(status_code=500, detail=str(e))

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return TextResponse(
        result=result,
        tokens_used=0 if auth.is_master else COST_TEXT,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.post(
    "/generate/image",
    response_model=FilePathResponse,
    tags=["Generation"],
    summary="Генерация изображения",
    description=f"Списывает {COST_IMAGE:,} токенов. Возвращает путь к PNG файлу.",
)
async def generate_image(body: GenerateImageRequest, auth: AuthContext = Depends(verify_auth)):
    await charge_user_tokens(auth, COST_IMAGE, "image", body.prompt)
    output_dir = body.output_dir or OUTPUT_DIR
    t0 = time.time()
    try:
        async with gen_lock:
            path = await generator.generate_image(body.prompt, output_dir=output_dir, timeout=body.timeout)
            if not auth.is_master:
                await db.add_generation_record(auth.user_id, body.prompt, path, "success", "image", COST_IMAGE)
    except Exception as e:
        logger.error(f"generate_image error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_IMAGE)
            await db.add_generation_record(auth.user_id, body.prompt, "", "failed", "image", 0)
        raise HTTPException(status_code=500, detail=str(e))

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return FilePathResponse(
        file=path,
        tokens_used=0 if auth.is_master else COST_IMAGE,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.post(
    "/edit/image",
    response_model=FilePathResponse,
    tags=["Generation"],
    summary="Редактирование изображения",
    description=(
        f"Списывает {COST_IMAGE_EDIT:,} токенов.\n"
        "Принимает исходный файл изображения (multipart/form-data) и текстовую инструкцию, "
        "что именно нужно изменить/дорисовать/заменить. Возвращает отредактированный PNG файл."
    ),
)
async def edit_image(
    prompt: str = Form(..., min_length=3, description="Что изменить в картинке", example="Add neon cyberpunk sunglasses"),
    image: UploadFile = File(..., description="Файл изображения (PNG/JPEG)"),
    timeout: int = Form(300, ge=30, le=600),
    auth: AuthContext = Depends(verify_auth),
):
    await charge_user_tokens(auth, COST_IMAGE_EDIT, "image_edit", prompt)
    t0 = time.time()
    
    # Сохраняем входящий файл во временную директорию uploads
    upload_dir = os.path.join(OUTPUT_DIR, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    temp_ext = os.path.splitext(image.filename)[1] or ".png"
    temp_path = os.path.join(upload_dir, f"input_{int(time.time())}_{temp_ext}")

    try:
        content = await image.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        async with gen_lock:
            path = await generator.edit_image(
                image_path=temp_path,
                prompt=prompt,
                output_dir=OUTPUT_DIR,
                timeout=timeout,
            )
            if not auth.is_master:
                await db.add_generation_record(
                    auth.user_id, prompt, path, "success", "image_edit", COST_IMAGE_EDIT
                )
    except Exception as e:
        logger.error(f"edit_image error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_IMAGE_EDIT)
            await db.add_generation_record(auth.user_id, prompt, "", "failed", "image_edit", 0)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Удаляем временный входной файл
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return FilePathResponse(
        file=path,
        tokens_used=0 if auth.is_master else COST_IMAGE_EDIT,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.post(
    "/animate/image",
    response_model=VideoResponse,
    tags=["Generation"],
    summary="Оживление изображения (Image-to-Video)",
    description=(
        f"Списывает {COST_IMAGE_TO_VIDEO:,} токенов.\n"
        "Загружает статичную картинку и генерирует 2 варианта анимации/видео на основе изображения."
    ),
)
async def animate_image(
    image: UploadFile = File(..., description="Файл изображения (PNG/JPEG)"),
    prompt: Optional[str] = Form("", description="Описание желаемого движения/анимации (необязательно)"),
    timeout: int = Form(1800, ge=60, le=2400),
    auth: AuthContext = Depends(verify_auth),
):
    await charge_user_tokens(auth, COST_IMAGE_TO_VIDEO, "image_to_video", prompt or "animate image")
    t0 = time.time()
    
    upload_dir = os.path.join(OUTPUT_DIR, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    temp_ext = os.path.splitext(image.filename)[1] or ".png"
    temp_path = os.path.join(upload_dir, f"animate_{int(time.time())}_{temp_ext}")

    try:
        content = await image.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        async with gen_lock:
            paths = await generator.animate_image(
                image_path=temp_path,
                prompt=prompt or "",
                output_dir=OUTPUT_DIR,
                timeout=timeout,
            )
            if not auth.is_master:
                first_path = paths[0] if paths else ""
                await db.add_generation_record(
                    auth.user_id, prompt or "animate", first_path, "success", "image_to_video", COST_IMAGE_TO_VIDEO
                )
    except Exception as e:
        logger.error(f"animate_image error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_IMAGE_TO_VIDEO)
            await db.add_generation_record(auth.user_id, prompt or "animate", "", "failed", "image_to_video", 0)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return VideoResponse(
        files=paths,
        tokens_used=0 if auth.is_master else COST_IMAGE_TO_VIDEO,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.post(
    "/generate/video",
    response_model=VideoResponse,
    tags=["Generation"],
    summary="Генерация видео (2 варианта)",
    description=f"Списывает {COST_VIDEO:,} токенов. Arena.ai генерирует 2 варианта видео.",
)
async def generate_video(body: GenerateVideoRequest, auth: AuthContext = Depends(verify_auth)):
    await charge_user_tokens(auth, COST_VIDEO, "video", body.prompt)
    output_dir = body.output_dir or OUTPUT_DIR
    t0 = time.time()
    try:
        async with gen_lock:
            paths = await generator.generate_video(body.prompt, output_dir=output_dir, timeout=body.timeout)
            if not auth.is_master:
                first_path = paths[0] if paths else ""
                await db.add_generation_record(auth.user_id, body.prompt, first_path, "success", "video", COST_VIDEO)
    except Exception as e:
        logger.error(f"generate_video error: {e}")
        if not auth.is_master:
            await db.add_tokens(auth.user_id, COST_VIDEO)
            await db.add_generation_record(auth.user_id, body.prompt, "", "failed", "video", 0)
        raise HTTPException(status_code=500, detail=str(e))

    balance_left = None
    if not auth.is_master:
        u = await db.get_user(auth.user_id)
        balance_left = u.get("tokens", 0)

    return VideoResponse(
        files=paths,
        tokens_used=0 if auth.is_master else COST_VIDEO,
        balance_left=balance_left,
        elapsed_sec=round(time.time() - t0, 2),
    )


@app.get(
    "/download",
    tags=["Files"],
    summary="Скачать сгенерированный файл",
    description="Скачивает файл по пути, возвращенному из /generate/*.",
)
async def download_file(path: str, _: AuthContext = Depends(verify_auth)):
    safe_path = os.path.realpath(path)
    cwd = os.path.realpath(".")
    if not safe_path.startswith(cwd):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(safe_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        safe_path,
        filename=os.path.basename(safe_path),
        media_type="application/octet-stream",
    )


@app.get(
    "/files",
    tags=["Files"],
    summary="Список сгенерированных файлов",
)
async def list_files(_: AuthContext = Depends(verify_auth)):
    out_dir = os.path.realpath(OUTPUT_DIR)
    if not os.path.exists(out_dir):
        return {"files": []}
    files = []
    for f in sorted(os.listdir(out_dir)):
        fp = os.path.join(out_dir, f)
        files.append({
            "name": f,
            "path": fp,
            "size_bytes": os.path.getsize(fp),
            "modified": os.path.getmtime(fp),
        })
    return {"files": files}
