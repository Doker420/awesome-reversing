# LGen REST API — Интеграция в сторонние проекты

> **Base URL:** `http://YOUR_VPS_IP:8000`  
> **Swagger UI:** `http://YOUR_VPS_IP:8000/docs`  
> **ReDoc:** `http://YOUR_VPS_IP:8000/redoc`

---

## 🔑 Авторизация и получение API Ключа

Каждый пользователь может получить персональный API ключ прямо в Telegram-боте:
1. Зайдите в бот и откройте **«👤 Кабинет»**.
2. Нажмите **«🔑 Управление API»**.
3. Скопируйте ключ формата `lg_xxxxxxxxxxxxxxxxxxxx`.

При каждом вызове API токен списывается непосредственно с баланса вашего пользователя в боте! Если генерация падает с ошибкой — токены автоматически возвращаются.

### Заголовок запроса:
```http
X-API-Key: lg_your_api_key_here
```

---

## 💰 Списание токенов

| Эндпоинт | Тип | Списание с баланса | Описание |
| :--- | :--- | :--- | :--- |
| `POST /generate/code` | Лендинг/Код | **2 500 000 токенов** | Возвращает архив сайта |
| `POST /generate/image` | Изображение | **250 000 токенов** | PNG изображение |
| `POST /generate/video` | Видео | **1 000 000 токенов** | 2 варианта видео |
| `POST /generate/text` | Текст | **50 000 токенов** | Текстовый ответ |
| `POST /generate/prompt` | Промпт | **50 000 токенов** | Создание промпта |
| `GET /me` | Профиль | **Бесплатно** | Баланс и данные аккаунта |

---

## 📡 Эндпоинты API

### 1. Проверка профиля и баланса токенов
`GET /me`

```bash
curl http://YOUR_VPS_IP:8000/me \
  -H "X-API-Key: lg_your_api_key"
```

**Ответ:**
```json
{
  "user_id": 123456789,
  "username": "durov",
  "first_name": "Pavel",
  "tokens": 4750000,
  "api_key": "lg_9f8a7b6c5d4e3f..."
}
```

---

### 2. Генерация изображения
`POST /generate/image`

```bash
curl -X POST http://YOUR_VPS_IP:8000/generate/image \
  -H "X-API-Key: lg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Futuristic neon sports car at midnight, 8k render, photorealistic"
  }'
```

**Ответ:**
```json
{
  "file": "generated/image_1728751900.png",
  "tokens_used": 250000,
  "balance_left": 4500000,
  "elapsed_sec": 38.4
}
```

---

### 3. Редактирование изображения (Image-to-Image / Inpainting)
`POST /edit/image`

Отправляет исходную картинку и промпт с описанием правок (дорисовать, изменить фон, переодеть и т.д.).

```bash
curl -X POST http://YOUR_VPS_IP:8000/edit/image \
  -H "X-API-Key: lg_your_api_key" \
  -F "prompt=Add neon sunglasses and cyberpunk background" \
  -F "image=@photo.jpg"
```

**Ответ:**
```json
{
  "file": "generated/image_1728751950.png",
  "tokens_used": 250000,
  "balance_left": 4250000,
  "elapsed_sec": 42.1
}
```

---

### 4. Оживление изображения (Image-to-Video)
`POST /animate/image`

Принимает статичную картинку и генерирует на её основе 2 анимированных видео-ролика.

```bash
curl -X POST http://YOUR_VPS_IP:8000/animate/image \
  -H "X-API-Key: lg_your_api_key" \
  -F "image=@portrait.jpg" \
  -F "prompt=Smooth cinematic camera zoom, hair blowing in the wind"
```

**Ответ:**
```json
{
  "files": [
    "generated/video_1728752000_0.mp4",
    "generated/video_1728752000_1.mp4"
  ],
  "tokens_used": 1000000,
  "balance_left": 3250000,
  "elapsed_sec": 210.3
}
```

---

### 5. Генерация видео по тексту (2 варианта)
`POST /generate/video`

```bash
curl -X POST http://YOUR_VPS_IP:8000/generate/video \
  -H "X-API-Key: lg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Ocean waves crashing on rocks in slow motion, cinematic lighting",
    "timeout": 600
  }'
```

**Ответ:**
```json
{
  "files": [
    "generated/video_1728751900_0.mp4",
    "generated/video_1728751900_1.mp4"
  ],
  "tokens_used": 1000000,
  "balance_left": 3500000,
  "elapsed_sec": 240.5
}
```

---

### 6. Генерация лендинга / кода
`POST /generate/code` (или `POST /generate/landing`)

```bash
curl -X POST http://YOUR_VPS_IP:8000/generate/code \
  -H "X-API-Key: lg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Landing page for luxury watch brand, dark modern luxury aesthetics"
  }'
```

**Ответ:**
```json
{
  "file": "generated/landing_1728751817.zip",
  "tokens_used": 2500000,
  "balance_left": 1000000,
  "elapsed_sec": 125.1
}
```

---

### 5. Генерация текста / промпта
`POST /generate/text` или `POST /generate/prompt`

```bash
curl -X POST http://YOUR_VPS_IP:8000/generate/prompt \
  -H "X-API-Key: lg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "cozy coffee shop during autumn rain"
  }'
```

---

### 6. Скачивание готового файла
`GET /download?path=<file_path>`

```bash
curl -O -H "X-API-Key: lg_your_api_key" \
  "http://YOUR_VPS_IP:8000/download?path=generated/image_1728751900.png"
```

---

## 💻 Примеры интеграции в сторонние проекты

### Python (библиотека `requests`)

```python
import requests

API_URL = "http://YOUR_VPS_IP:8000"
API_KEY = "lg_your_personal_key_here"
HEADERS = {"X-API-Key": API_KEY}

# 1. Проверяем баланс
profile = requests.get(f"{API_URL}/me", headers=HEADERS).json()
print(f"Баланс: {profile['tokens']} токенов")

# 2. Генерируем картинку
res = requests.post(f"{API_URL}/generate/image", headers=HEADERS, json={
    "prompt": "Cyberpunk girl portrait, highly detailed"
}).json()

image_path = res["file"]
print(f"Списано токенов: {res['tokens_used']}, остаток: {res['balance_left']}")

# 3. Скачиваем файл на свой сервер/компьютер
file_data = requests.get(f"{API_URL}/download", headers=HEADERS, params={"path": image_path})
with open("result.png", "wb") as f:
    f.write(file_data.content)
print("Файл сохранён в result.png!")
```

---

### Node.js / JavaScript (Fetch)

```javascript
const API_URL = "http://YOUR_VPS_IP:8000";
const API_KEY = "lg_your_personal_key_here";

async function generateLanding(prompt) {
  const response = await fetch(`${API_URL}/generate/code`, {
    method: "POST",
    headers: {
      "X-API-Key": API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ prompt }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(`Ошибка: ${error.detail}`);
  }

  const data = await response.json();
  console.log("Сгенерирован файл:", data.file);
  console.log("Осталось токенов:", data.balance_left);
  return data.file;
}
```

---

## ⚠️ Обработка ошибок

- **`401 Unauthorized`** — Не передан или недействителен `X-API-Key`.
- **`402 Payment Required`** — Недостаточно токенов на балансе (`Insufficient tokens`). Пользователю нужно пополнить баланс в Telegram-боте.
- **`500 Internal Server Error`** — Ошибка генерации (таймаут Arena.ai или капча). При ошибке **все списанные токены автоматически возвращаются на баланс**!
