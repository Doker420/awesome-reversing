FROM python:3.11-slim

# Системные зависимости для Playwright
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libpangocairo-1.0-0 libxshmfence1 libx11-6 libxext6 \
    libxcb1 libx11-xcb1 fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright браузеры
RUN playwright install chromium
RUN playwright install-deps chromium

# Код
COPY . .

# Создаём нужные папки
RUN mkdir -p cookies generated

EXPOSE 8000

# Запускаем API сервер
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
