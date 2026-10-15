# LGen — Landing / Image / Video / Text Generator

> ⚠️ **Обновление (исправление генерации медиа).** Раньше работали только текст и
> лендинг: в режимах «картинка», «видео», «изменить фото» и «оживить фото» промпт
> не отправлялся (модалка Terms of Use блокировала composer, а Enter «съедался»
> ей же), а результат не забирался (искали текстовую кнопку «Download», хотя в
> карточке результата Arena это иконка).
> Что именно исправлено и как это проверить — **[FIXES.ru.md](FIXES.ru.md)**.

AI-генератор контента через Arena.ai. Включает:
- 🤖 **Telegram Bot** — для пользователей
- 🌐 **REST API** — для интеграций / автоматизации

## 📂 Структура проекта

```
LGen/
├── bot.py               # Telegram Bot (aiogram 3)
├── api_server.py        # REST API (FastAPI)
├── arena_generator.py   # Playwright-обёртка над arena.ai
├── config.py            # Конфигурация
├── database.py          # SQLite база
├── admin.py             # Админ-функции
├── payments.py          # Платежи (CryptoBot / Platega)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── API_DOCS.md          # ← Полная документация API
└── cookies/
    └── arena_cookies.json
```

## ✅ Проверка после обновления

```bash
# быстрые офлайн-тесты (без браузера)
pytest -q tests/

# офлайн-прогон реального конвейера в Chromium против локального мока arena.ai
# (модалка Terms of Use, textarea, Send message, Stop generation, карточка результата)
python selftest_arena.py            # все режимы
python selftest_arena.py image      # только картинка
python selftest_arena.py video      # только видео
```

## 🚀 Быстрый старт (локально)

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Отредактируйте .env

mkdir -p cookies generated

# 🌟 Запуск всего одной командой (Бот + REST API одновременно):
python run.py

# Или раздельно (при необходимости):
python run.py --bot          # только Telegram-бот
python run.py --api          # только REST API сервер
```

## 🐳 Деплой на VPS (Docker)

```bash
# Настроить .env
cp .env.example .env && nano .env

# Добавить cookies/arena_cookies.json

# Запустить всё
docker compose up -d --build
```

- API доступен на `http://YOUR_VPS:8000`
- Swagger UI: `http://YOUR_VPS:8000/docs`

## 📡 API — режимы генерации

| Endpoint | Режим Arena | Описание |
|----------|-------------|----------|
| `POST /generate/code` | code/direct | Лендинг (ZIP/HTML) |
| `POST /generate/text` | text/direct | Текстовый ответ |
| `POST /generate/prompt` | text/direct | Генерация промта по теме |
| `POST /generate/image` | image/direct | Изображение (PNG) |
| `POST /edit/image` | image/direct | Редактирование фото (multipart) |
| `POST /animate/image` | video | Оживление фото → 2 видео |
| `POST /generate/video` | video | 2 видео-варианта |
| `GET /download?path=` | — | Скачать файл |
| `GET /files` | — | Список файлов |
| `GET /health` | — | Статус |

→ Подробная документация: **[API_DOCS.md](API_DOCS.md)**

## 🔐 Авторизация API

Все запросы (кроме `/health`) требуют заголовка:
```
X-API-Key: your_api_secret
```
Ключ настраивается в `.env` → `API_SECRET`.