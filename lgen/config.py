import os
from dotenv import load_dotenv

load_dotenv()

MULTILANG = True

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = list(map(int, filter(None, os.getenv('ADMIN_IDS', '').split(','))))

DB_PATH = 'bot_database.db'

COOKIES_PATH = 'cookies/arena_cookies.json'
ARENA_TIMEOUT = 300000

# ── Таймауты генерации (в секундах) ───────────────────────────
# Картинки/редактирование/оживление: 10 минут, видео: 30 минут, текст: 5 минут.
IMAGE_TIMEOUT = int(os.getenv('IMAGE_TIMEOUT', '600'))
VIDEO_TIMEOUT = int(os.getenv('VIDEO_TIMEOUT', '1800'))
TEXT_TIMEOUT = int(os.getenv('TEXT_TIMEOUT', '300'))
ARENA_VIDEO_VARIANTS = int(os.getenv('ARENA_VIDEO_VARIANTS', '2'))   # Arena отдаёт 2 варианта

FREE_TOKENS = 250_000          # токены при регистрации (1 бесплатная картинка)

LANGUAGE_CHOICE_SELECTION = "🇺🇸 English / 🇷🇺 Русский"

# ── Token economy ─────────────────────────────────────────────
# Цена: 10 млн токенов = 35 руб
TOKEN_PRICE_RUB_PER_10M = 35          # 35 руб за 10_000_000 токенов
TOKEN_PRICE_PER_TOKEN = TOKEN_PRICE_RUB_PER_10M / 10_000_000  # ≈ 0.0000035 руб

# Стоимость операций в токенах
COST_IMAGE           = 250_000      # 250к токенов = 1 изображение
COST_IMAGE_EDIT      = 250_000      # 250к токенов = 1 редактирование изображения
COST_IMAGE_TO_VIDEO  = 1_000_000    # 1М токенов   = 1 оживление картинки (видео)
COST_VIDEO           = 1_000_000    # 1М токенов   = 1 видео
COST_LANDING         = 2_500_000    # 2.5М токенов = 1 лендинг
COST_TEXT            = 50_000       # 50к токенов  = 1 текст/промпт

# ── Пакеты токенов ────────────────────────────────────────────
TOKEN_PLANS = {
    "starter": {
        "name": "starter",
        "tokens": 1_000_000,          # 1М
        "price": 4,                   # 3.5 ≈ 4 руб
        "label": "🟢 Старт — 4₽ · 1М токенов",
        "desc": "~4 картинки",
    },
    "basic": {
        "name": "basic",
        "tokens": 5_000_000,          # 5М
        "price": 18,                  # 17.5 ≈ 18 руб
        "label": "🔵 Базовый — 18₽ · 5М токенов",
        "desc": "~20 картинок / 5 видео",
    },
    "pro": {
        "name": "pro",
        "tokens": 10_000_000,         # 10М
        "price": 35,                  # ровно 35 руб
        "label": "🟣 Про — 35₽ · 10М токенов",
        "desc": "~40 картинок / 10 видео / 4 сайта",
    },
    "ultra": {
        "name": "ultra",
        "tokens": 50_000_000,         # 50М
        "price": 150,
        "label": "⭐ Ультра — 150₽ · 50М токенов",
        "desc": "~200 картинок / 50 видео / 20 сайтов",
    },
}

# ── Реферальная система ───────────────────────────────────────
REFERRALS_NEEDED = 5
REFERRAL_BONUS_TOKENS = 500_000       # 500к токенов за реферала

# ── Custom Emoji IDs ──────────────────────────────────────────
EMOJI_IDS = {
    'PREMIUM': "5316708469183645471",
    'BALANCE': "5316747166838982433",
    'REFERRAL': "5316928135285997372",
    'ERROR': "5314252108667591226",
    'GENERATE': "5317008708872473064",
    'ADMIN': "5314268949234359428",
    'PROMO': "5314402428227983888",
    'LOADING': "5317058195485661057",
    'SUCCESS': "5314250824472368130",
    'BACK': "5255703720078879038",
    'HELP': "5316891950186532806",
}

# ── Payments ──────────────────────────────────────────────────
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "")
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "")
CRYPTOBOT_API_TOKEN = os.getenv("CRYPTOBOT_API_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# REST API
API_SECRET = os.getenv("API_SECRET", "change_me_in_dotenv")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "generated")

# Обратная совместимость (старый бот использовал generations)
FREE_GENERATIONS = 1
REFERRAL_BONUS = 1