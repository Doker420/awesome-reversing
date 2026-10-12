"""
LGen Telegram Bot — токеновая система
Красивый UI личного кабинета + генерация через Arena.ai
"""

import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
    CallbackQuery,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    BOT_TOKEN, ADMIN_IDS,
    TOKEN_PLANS, FREE_TOKENS,
    COST_IMAGE, COST_IMAGE_EDIT, COST_IMAGE_TO_VIDEO, COST_VIDEO, COST_LANDING, COST_TEXT,
    REFERRALS_NEEDED, REFERRAL_BONUS_TOKENS,
)
from database import Database
from arena_generator import ArenaGenerator
from admin import is_admin
from payments import create_platega_payment, create_cryptobot_payment, check_and_activate_payment

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
REF_PHOTO_PATH = os.path.join(BOT_DIR, "ref.jpg")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Утилиты форматирования
# ─────────────────────────────────────────────────────────────
def fmt_tokens(n: int) -> str:
    """Красиво форматирует число токенов."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}М".rstrip('0').rstrip('.')
    if n >= 1_000:
        return f"{n / 1_000:.1f}К".rstrip('0').rstrip('.')
    return str(n)


def tokens_to_rub(n: int) -> str:
    """Переводит токены в рубли по курсу 10М = 35₽."""
    rub = n * 35 / 10_000_000
    if rub < 0.01:
        return "< 0.01₽"
    return f"{rub:.2f}₽"


def progress_bar(current: int, total: int, width: int = 10) -> str:
    """Рисует текстовый прогресс-бар из emoji."""
    if total <= 0:
        return "░" * width
    filled = min(int(current / total * width), width)
    return "█" * filled + "░" * (width - filled)


# ─────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────
class GenStates(StatesGroup):
    choose_mode     = State()
    waiting_prompt  = State()
    waiting_image   = State()

class AdminStates(StatesGroup):
    waiting_promocode = State()
    waiting_broadcast = State()

class PaymentStates(StatesGroup):
    choosing_plan   = State()
    waiting_check   = State()


# ─────────────────────────────────────────────────────────────
# Инициализация
# ─────────────────────────────────────────────────────────────
db = Database()
generator = ArenaGenerator()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

gen_lock = asyncio.Lock()
waiting_queue: list[dict] = []


# ─────────────────────────────────────────────────────────────
# ═══════════════ КЛАВИАТУРЫ ═══════════════
# ─────────────────────────────────────────────────────────────

def kb_main(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚡ Создать", callback_data="gen_menu"),
        InlineKeyboardButton(text="👤 Кабинет", callback_data="cabinet"),
    )
    builder.row(
        InlineKeyboardButton(text="💎 Купить токены", callback_data="buy"),
        InlineKeyboardButton(text="👥 Рефералы", callback_data="referral"),
    )
    builder.row(
        InlineKeyboardButton(text="🎫 Промокод", callback_data="promo"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
    )
    if user_id in ADMIN_IDS:
        builder.row(InlineKeyboardButton(text="🔧 Админ", callback_data="admin"))
    return builder.as_markup()


def kb_generate_menu(tokens: int) -> InlineKeyboardMarkup:
    """Меню выбора режима генерации с визуальным индикатором доступности."""
    def _cost_tag(cost: int) -> str:
        ok = tokens >= cost
        icon = "✅" if ok else "🔴"
        return f"{icon} {fmt_tokens(cost)} т."

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🌐 Лендинг  {_cost_tag(COST_LANDING)}",
            callback_data="mode_landing"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🖼 Картинка  {_cost_tag(COST_IMAGE)}",
            callback_data="mode_image"
        ),
        InlineKeyboardButton(
            text=f"🎨 Изменить фото  {_cost_tag(COST_IMAGE_EDIT)}",
            callback_data="mode_image_edit"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🎬 Видео  {_cost_tag(COST_VIDEO)}",
            callback_data="mode_video"
        ),
        InlineKeyboardButton(
            text=f"🪄 Оживить фото  {_cost_tag(COST_IMAGE_TO_VIDEO)}",
            callback_data="mode_image_to_video"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"✍️ Текст  {_cost_tag(COST_TEXT)}",
            callback_data="mode_text"
        ),
        InlineKeyboardButton(
            text=f"💡 Промпт  {_cost_tag(COST_TEXT)}",
            callback_data="mode_prompt"
        ),
    )
    builder.row(
        InlineKeyboardButton(text="💎 Купить токены", callback_data="buy"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu"),
    )
    return builder.as_markup()


def kb_plans() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, plan in TOKEN_PLANS.items():
        builder.row(InlineKeyboardButton(
            text=plan["label"],
            callback_data=f"plan_{key}"
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu"))
    return builder.as_markup()


def kb_pay_method(lang: str = "ru") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🪙 Крипто (CryptoBot)", callback_data="method_crypto"))
    # builder.row(InlineKeyboardButton(text="💳 Карта (Platega)", callback_data="method_platega"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="buy"))
    return builder.as_markup()


def kb_back(cb: str = "back_to_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)]
    ])


def kb_admin() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="👑 Топ рефералов", callback_data="admin_top_refs"))
    builder.row(
        InlineKeyboardButton(text="🎫 Создать промо", callback_data="admin_create_promo"),
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu"))
    return builder.as_markup()


# ─────────────────────────────────────────────────────────────
# ═══════════════ ТЕКСТЫ ЭКРАНОВ ═══════════════
# ─────────────────────────────────────────────────────────────

async def text_cabinet(user_id: int) -> str:
    """Красивый личный кабинет."""
    u = await db.get_user(user_id)
    if not u:
        return "❌ Пользователь не найден"

    tokens = u.get("tokens", 0)
    refs   = u.get("referrals_count", 0)
    to_bonus = REFERRALS_NEEDED - (refs % REFERRALS_NEEDED)

    # Индикатор «хватит на»
    can_image   = tokens // COST_IMAGE
    can_video   = tokens // COST_VIDEO
    can_landing = tokens // COST_LANDING

    bar_img  = progress_bar(min(tokens, COST_IMAGE * 5),   COST_IMAGE * 5)
    bar_vid  = progress_bar(min(tokens, COST_VIDEO * 3),   COST_VIDEO * 3)
    bar_land = progress_bar(min(tokens, COST_LANDING * 2), COST_LANDING * 2)

    api_key = u.get("api_key") or "Не создан (нажмите кнопку ниже)"
    name = u.get("first_name") or u.get("username") or f"id{user_id}"

    return (
        f"👤 <b>Личный кабинет</b> — {name}\n"
        f"{'─' * 30}\n\n"
        f"💰 <b>Баланс токенов:</b>  <code>{fmt_tokens(tokens)}</code>\n"
        f"   ≈ {tokens_to_rub(tokens)} по текущему курсу\n\n"
        f"🔑 <b>Ваш API ключ:</b>\n"
        f"<code>{api_key}</code>\n"
        f"<i>(Используйте заголовок X-API-Key для интеграций)</i>\n\n"
        f"📊 <b>Возможности:</b>\n"
        f"   🖼 Картинки:  <b>{can_image}</b>  {bar_img}\n"
        f"   🎬 Видео:     <b>{can_video}</b>  {bar_vid}\n"
        f"   🌐 Лендинги:  <b>{can_landing}</b>  {bar_land}\n\n"
        f"👥 <b>Рефералы:</b>  {refs}  "
        f"(ещё {to_bonus} → +{fmt_tokens(REFERRAL_BONUS_TOKENS)} токенов)\n\n"
        f"💡 <b>Курс:</b>  10М токенов = 35₽\n"
        f"   250К = 🖼  |  1М = 🎬  |  2.5М = 🌐"
    )


def text_welcome(user_data: dict) -> str:
    tokens = user_data.get("tokens", 0)
    name   = user_data.get("first_name") or "пользователь"
    return (
        f"✨ <b>Добро пожаловать, {name}!</b>\n\n"
        f"Я генерирую <b>лендинги</b>, <b>изображения</b>, "
        f"<b>видео</b> и <b>тексты</b> через Arena.ai.\n\n"
        f"💰 <b>Баланс:</b>  <code>{fmt_tokens(tokens)}</code> токенов\n\n"
        f"<b>Стоимость генерации:</b>\n"
        f"  🌐 Лендинг     — <code>{fmt_tokens(COST_LANDING)}</code>\n"
        f"  🖼 Картинка    — <code>{fmt_tokens(COST_IMAGE)}</code>\n"
        f"  🎬 Видео       — <code>{fmt_tokens(COST_VIDEO)}</code>\n"
        f"  ✍️ Текст/Промпт — <code>{fmt_tokens(COST_TEXT)}</code>\n\n"
        f"💎 <i>10М токенов = 35₽</i>"
    )


def text_buy_menu() -> str:
    lines = ["💎 <b>Купить токены</b>\n"]
    for plan in TOKEN_PLANS.values():
        lines.append(
            f"  {plan['label']}\n"
            f"    <i>{plan['desc']}</i>\n"
        )
    lines.append("\n💡 Чем больше — тем выгоднее!")
    return "\n".join(lines)


def text_gen_menu(tokens: int) -> str:
    return (
        f"⚡ <b>Создать контент</b>\n\n"
        f"Ваш баланс: <code>{fmt_tokens(tokens)}</code> токенов\n\n"
        f"<b>Выберите тип генерации:</b>"
    )


# ─────────────────────────────────────────────────────────────
# ═══════════════ ХЕНДЛЕРЫ ═══════════════
# ─────────────────────────────────────────────────────────────

# ── /start ──────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user

    if not await db.get_user(user.id):
        referrer_id = None
        args = message.text.split()
        if len(args) > 1:
            try:
                rid = int(args[1])
                if await db.get_user(rid):
                    referrer_id = rid
            except ValueError:
                pass

        await db.add_user(user.id, user.username or "", user.first_name or "", referrer_id, "ru")

        if user.id in ADMIN_IDS:
            await db.add_tokens(user.id, 10_000_000)

        if referrer_id:
            try:
                await db.add_referral(referrer_id, user.id)
                ref_data = await db.get_user(referrer_id)
                rc = ref_data.get("referrals_count", 0)
                if rc % REFERRALS_NEEDED == 0:
                    await bot.send_message(
                        referrer_id,
                        f"🎉 Новый реферал! Итого: {rc}\n"
                        f"💰 Бонус +{fmt_tokens(REFERRAL_BONUS_TOKENS)} токенов начислен!"
                    )
                else:
                    to_go = REFERRALS_NEEDED - (rc % REFERRALS_NEEDED)
                    await bot.send_message(
                        referrer_id,
                        f"👥 Новый реферал! Итого: {rc}\n"
                        f"Ещё {to_go} → бонус!"
                    )
            except Exception as e:
                logger.error(f"Referral notify error: {e}")

        asyncio.create_task(_schedule_ref_notifications(user.id))

    await db.update_last_active(user.id)
    user_data = await db.get_user(user.id)
    await message.answer(
        text_welcome(user_data),
        reply_markup=kb_main(user.id),
        parse_mode="HTML"
    )


# ── Команды ─────────────────────────────────────────────────
@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "❓ <b>Помощь</b>\n\n"
        "🌐 <b>Лендинг</b> — получите ZIP с готовым сайтом\n"
        "🖼 <b>Картинка</b> — AI-генерация изображения\n"
        "🎬 <b>Видео</b> — 2 варианта на выбор\n"
        "✍️ <b>Текст</b> — любой текст, статья, описание\n"
        "💡 <b>Промпт</b> — AI создаст промпт по вашей теме\n\n"
        "<b>Стоимость:</b>\n"
        f"  🌐 {fmt_tokens(COST_LANDING)} токенов\n"
        f"  🖼 {fmt_tokens(COST_IMAGE)} токенов\n"
        f"  🎬 {fmt_tokens(COST_VIDEO)} токенов\n"
        f"  ✍️ {fmt_tokens(COST_TEXT)} токенов\n\n"
        "💎 <b>Курс:</b> 10М токенов = 35₽\n"
        "👥 Приглашай друзей — получай токены бесплатно!\n\n"
        "📩 Поддержка: @support",
        parse_mode="HTML",
        reply_markup=kb_back()
    )


@dp.message(Command("cabinet"))
async def cmd_cabinet(message: Message):
    await message.answer(
        await text_cabinet(message.from_user.id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Создать", callback_data="gen_menu"),
             InlineKeyboardButton(text="💎 Пополнить", callback_data="buy")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")],
        ]),
        parse_mode="HTML"
    )


@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    await message.answer(text_buy_menu(), reply_markup=kb_plans(), parse_mode="HTML")


@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    await message.answer(
        await text_cabinet(message.from_user.id),
        reply_markup=kb_back(),
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────────────────────
# ═══════════════ CALLBACK HANDLERS ═══════════════
# ─────────────────────────────────────────────────────────────

# ── Главное меню ────────────────────────────────────────────
@dp.callback_query(F.data == "back_to_menu")
async def cb_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_data = await db.get_user(callback.from_user.id)
    try:
        await callback.message.edit_text(
            text_welcome(user_data),
            reply_markup=kb_main(callback.from_user.id),
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            text_welcome(user_data),
            reply_markup=kb_main(callback.from_user.id),
            parse_mode="HTML"
        )
    await callback.answer()


# ── Личный кабинет ──────────────────────────────────────────
@dp.callback_query(F.data == "cabinet")
async def cb_cabinet(callback: CallbackQuery):
    text = await text_cabinet(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Создать", callback_data="gen_menu"),
         InlineKeyboardButton(text="💎 Пополнить", callback_data="buy")],
        [InlineKeyboardButton(text="🔑 Управление API", callback_data="api_menu"),
         InlineKeyboardButton(text="📋 История", callback_data="history")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="referral"),
         InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ── Управление API ключом ──────────────────────────────────
@dp.callback_query(F.data == "api_menu")
async def cb_api_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    key = await db.get_or_create_api_key(user_id)
    u = await db.get_user(user_id)
    tokens = u.get("tokens", 0)

    text = (
        f"🔑 <b>Управление API ключом</b>\n\n"
        f"С помощью этого ключа вы можете делать запросы из любых ваших сайтов, "
        f"скриптов и приложений. Токены будут автоматически списываться с вашего баланса Telegram-бота.\n\n"
        f"💎 <b>Баланс:</b> <code>{fmt_tokens(tokens)}</code> токенов\n"
        f"🔑 <b>Ваш API Ключ:</b>\n"
        f"<code>{key}</code>\n\n"
        f"📡 <b>Пример запроса (curl):</b>\n"
        f"<pre>curl -X POST http://YOUR_VPS:8000/generate/image \\\n"
        f"  -H 'X-API-Key: {key}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{\"prompt\": \"Cyberpunk samurai\"}}'</pre>\n\n"
        f"📚 <b>Документация API:</b> <code>http://YOUR_VPS:8000/docs</code>\n"
        f"⚠️ <i>Никому не передавайте ваш ключ!</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сгенерировать новый ключ", callback_data="api_regenerate")],
        [InlineKeyboardButton(text="👤 Назад в кабинет", callback_data="cabinet")],
    ])

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "api_regenerate")
async def cb_api_regenerate(callback: CallbackQuery):
    user_id = callback.from_user.id
    new_key = await db.regenerate_api_key(user_id)
    await callback.answer("✅ Новый API ключ создан! Старый аннулирован.", show_alert=True)
    await cb_api_menu(callback)


# ── История генераций ────────────────────────────────────────
@dp.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery):
    history = await db.get_user_history(callback.from_user.id, limit=8)
    if not history:
        await callback.answer("📋 История пуста", show_alert=True)
        return

    mode_icons = {
        "landing": "🌐", "image": "🖼", "video": "🎬",
        "text": "✍️", "prompt": "💡", "code": "🌐",
    }
    lines = ["📋 <b>Последние генерации:</b>\n"]
    for i, h in enumerate(history, 1):
        icon  = mode_icons.get(h.get("mode", "landing"), "⚙️")
        date  = time.strftime("%d.%m %H:%M", time.localtime(h.get("created_at", 0)))
        st    = "✅" if h.get("status") == "success" else "❌"
        tok   = fmt_tokens(h.get("tokens_used", 0))
        prompt = (h.get("prompt") or "")[:35]
        lines.append(f"{i}. {icon} {st} {date}  -{tok}\n   <i>{prompt}…</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb_back("cabinet"),
        parse_mode="HTML"
    )
    await callback.answer()


# ── Помощь ──────────────────────────────────────────────────
@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "❓ <b>Помощь</b>\n\n"
        "🌐 <b>Лендинг</b> — ZIP с готовым сайтом\n"
        "🖼 <b>Картинка</b> — AI изображение\n"
        "🎬 <b>Видео</b> — 2 варианта от Arena.ai\n"
        "✍️ <b>Текст</b> — статья, описание, пост\n"
        "💡 <b>Промпт</b> — AI придумает промпт по теме\n\n"
        f"<b>Стоимость:</b>\n"
        f"  🌐 {fmt_tokens(COST_LANDING)} т. · 🖼 {fmt_tokens(COST_IMAGE)} т. · "
        f"🎬 {fmt_tokens(COST_VIDEO)} т. · ✍️ {fmt_tokens(COST_TEXT)} т.\n\n"
        "💎 Курс: <b>10М токенов = 35₽</b>\n"
        "📩 Поддержка: @support",
        reply_markup=kb_back(),
        parse_mode="HTML"
    )
    await callback.answer()


# ─────────────────────────────────────────────────────────────
# ═══ ГЕНЕРАЦИЯ — выбор режима ════════════════════════════════
# ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "gen_menu")
async def cb_gen_menu(callback: CallbackQuery, state: FSMContext):
    u = await db.get_user(callback.from_user.id)
    tokens = u.get("tokens", 0)
    try:
        await callback.message.edit_text(
            text_gen_menu(tokens),
            reply_markup=kb_generate_menu(tokens),
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            text_gen_menu(tokens),
            reply_markup=kb_generate_menu(tokens),
            parse_mode="HTML"
        )
    await state.set_state(GenStates.choose_mode)
    await callback.answer()


MODE_CONFIG = {
    "mode_landing": {
        "mode": "landing", "cost": COST_LANDING,
        "icon": "🌐", "name": "лендинг",
        "hint": "Опишите желаемый лендинг:\n<i>Пример: «Современный лендинг для фитнес-клуба с тёмной темой»</i>",
    },
    "mode_image": {
        "mode": "image", "cost": COST_IMAGE,
        "icon": "🖼", "name": "картинку",
        "hint": "Опишите изображение:\n<i>Пример: «Закат над горами в стиле аниме, детализированный»</i>",
    },
    "mode_image_edit": {
        "mode": "image_edit", "cost": COST_IMAGE_EDIT,
        "icon": "🎨", "name": "изменение картинки",
        "hint": "📸 <b>Отправьте изображение</b> (фото или файл), которое хотите отредактировать:",
    },
    "mode_video": {
        "mode": "video", "cost": COST_VIDEO,
        "icon": "🎬", "name": "видео",
        "hint": "Опишите видео:\n<i>Пример: «Дрон летит над ночным городом, неоновые огни, 4K»</i>",
    },
    "mode_image_to_video": {
        "mode": "image_to_video", "cost": COST_IMAGE_TO_VIDEO,
        "icon": "🪄", "name": "оживление фото",
        "hint": "📸 <b>Отправьте изображение</b> (фото или файл), которое хотите оживить в видео:",
    },
    "mode_text": {
        "mode": "text", "cost": COST_TEXT,
        "icon": "✍️", "name": "текст",
        "hint": "Введите запрос:\n<i>Пример: «Напиши продающее описание для онлайн-курса по Python»</i>",
    },
    "mode_prompt": {
        "mode": "prompt", "cost": COST_TEXT,
        "icon": "💡", "name": "промпт",
        "hint": "Введите тему, а я создам промпт:\n<i>Пример: «киберпанк девушка под дождём»</i>",
    },
}


@dp.callback_query(F.data.startswith("mode_"))
async def cb_choose_mode(callback: CallbackQuery, state: FSMContext):
    cfg = MODE_CONFIG.get(callback.data)
    if not cfg:
        await callback.answer()
        return

    u = await db.get_user(callback.from_user.id)
    tokens = u.get("tokens", 0)

    if tokens < cfg["cost"]:
        shortage = cfg["cost"] - tokens
        await callback.answer(
            f"❌ Недостаточно токенов!\n"
            f"Нужно: {fmt_tokens(cfg['cost'])}\n"
            f"Ваш баланс: {fmt_tokens(tokens)}\n"
            f"Не хватает: {fmt_tokens(shortage)}",
            show_alert=True
        )
        return

    await state.update_data(mode=cfg["mode"], cost=cfg["cost"])

    if cfg["mode"] in ("image_edit", "image_to_video"):
        await state.set_state(GenStates.waiting_image)
        await callback.message.edit_text(
            f"{cfg['icon']} <b>{cfg['name'].capitalize()}</b>\n\n"
            f"Будет списано: <code>{fmt_tokens(cfg['cost'])}</code> токенов\n\n"
            f"{cfg['hint']}",
            reply_markup=kb_back("gen_menu"),
            parse_mode="HTML"
        )
    else:
        await state.set_state(GenStates.waiting_prompt)
        await callback.message.edit_text(
            f"{cfg['icon']} <b>Генерация — {cfg['name']}</b>\n\n"
            f"Будет списано: <code>{fmt_tokens(cfg['cost'])}</code> токенов\n\n"
            f"{cfg['hint']}",
            reply_markup=kb_back("gen_menu"),
            parse_mode="HTML"
        )
    await callback.answer()


# ─────────────────────────────────────────────────────────────
# Получение картинки для редактирования
# ─────────────────────────────────────────────────────────────
@dp.message(GenStates.waiting_image, F.photo | F.document)
async def process_image_for_edit(message: Message, state: FSMContext):
    os.makedirs("generated/uploads", exist_ok=True)
    temp_path = f"generated/uploads/tg_{message.from_user.id}_{int(time.time())}.png"

    if message.photo:
        photo = message.photo[-1]
        await bot.download(photo, destination=temp_path)
    elif message.document:
        await bot.download(message.document, destination=temp_path)

    await state.update_data(image_path=temp_path)
    await state.set_state(GenStates.waiting_prompt)
    fsm = await state.get_data()
    mode = fsm.get("mode")

    if mode == "image_to_video":
        await message.answer(
            "✅ <b>Изображение получено!</b>\n\n"
            "Напишите, какое движение или действие должно происходить (или отправьте точку/любой текст для естественной анимации):\n"
            "<i>Пример: «Камера плавно приближается, волны движутся на закате»</i>",
            reply_markup=kb_back("gen_menu"),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "✅ <b>Изображение получено!</b>\n\n"
            "Теперь напишите, что именно нужно <b>изменить, добавить или удалить</b> на этой картинке:\n"
            "<i>Пример: «Добавь неоновые очки на персонажа и сделай фон ночным городом»</i>",
            reply_markup=kb_back("gen_menu"),
            parse_mode="HTML"
        )


# ─────────────────────────────────────────────────────────────
# Обработка промпта и запуск генерации
# ─────────────────────────────────────────────────────────────
@dp.message(GenStates.waiting_prompt)
async def process_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode", "landing")
    cost = data.get("cost", COST_LANDING)
    await state.clear()
    await _run_generation(message, message.text, mode, cost, extra_data=data)


async def _run_generation(message: Message, prompt: str, mode: str, cost: int, extra_data: dict = None):
    user_id = message.from_user.id
    extra_data = extra_data or {}
    u = await db.get_user(user_id)
    if not u:
        return

    tokens = u.get("tokens", 0)
    if tokens < cost:
        await message.answer(
            f"❌ <b>Недостаточно токенов</b>\n\n"
            f"Нужно: <code>{fmt_tokens(cost)}</code>\n"
            f"Баланс: <code>{fmt_tokens(tokens)}</code>\n\n"
            f"Пополните баланс ⬇️",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить токены", callback_data="buy")],
            ]),
            parse_mode="HTML"
        )
        return

    # Очередь
    in_queue = False
    if gen_lock.locked():
        waiting_queue.append({"user_id": user_id, "message": message})
        pos = len(waiting_queue)
        in_queue = True
        await message.answer(
            f"⏳ <b>В очереди</b>  #{pos}\n\n"
            f"Ваш запрос поставлен в очередь. Ожидайте...",
            parse_mode="HTML"
        )

    async with gen_lock:
        if in_queue:
            for item in list(waiting_queue):
                if item["user_id"] == user_id:
                    waiting_queue.remove(item)
                    break
            for idx, item in enumerate(waiting_queue, 1):
                try:
                    await item["message"].answer(
                        f"🔄 Ваша позиция в очереди: <b>#{idx}</b>", parse_mode="HTML"
                    )
                except Exception:
                    pass

        # Списываем токены
        if not await db.spend_tokens(user_id, cost):
            await message.answer("❌ Не удалось списать токены. Пополните баланс.", parse_mode="HTML")
            return

        mode_icons = {
            "landing": "🌐", "image": "🖼", "video": "🎬",
            "text": "✍️", "prompt": "💡",
            "image_edit": "🎨", "image_to_video": "🪄",
        }
        icon = mode_icons.get(mode, "⚙️")

        status_msg = await message.answer(
            f"{icon} <b>Генерирую...</b>\n\n"
            f"<code>{prompt[:100]}{'…' if len(prompt) > 100 else ''}</code>\n\n"
            f"⏱ Это может занять несколько минут. Ожидайте...",
            parse_mode="HTML"
        )

        try:
            result_path = None
            result_text = None
            result_files = None

            if mode in ("landing", "code"):
                result_path = await generator.generate_landing(prompt)
            elif mode == "image":
                result_path = await generator.generate_image(prompt)
            elif mode == "image_edit":
                img_path = extra_data.get("image_path")
                if not img_path or not os.path.exists(img_path):
                    raise Exception("Исходное изображение не найдено. Пожалуйста, отправьте его заново.")
                result_path = await generator.edit_image(img_path, prompt)
            elif mode == "image_to_video":
                img_path = extra_data.get("image_path")
                if not img_path or not os.path.exists(img_path):
                    raise Exception("Исходное изображение не найдено. Пожалуйста, отправьте его заново.")
                result_files = await generator.animate_image(img_path, prompt)
            elif mode == "video":
                result_files = await generator.generate_video(prompt)
            elif mode == "text":
                result_text = await generator.generate_text(prompt)
            elif mode == "prompt":
                result_text = await generator.generate_prompt(prompt)

            # ── Защита от «пустого» результата ───────────────────
            # Раньше при ошибке генератор мог вернуть None: пользователь не получал
            # никакого ответа, а токены списывались. Теперь это явная ошибка.
            if not (result_path or result_files or (result_text and result_text.strip())):
                raise Exception("Пустой результат генерации (Arena.ai не вернула файл/текст)")

            # ── Отправляем результат ─────────────────────────────
            updated = await db.get_user(user_id)
            bal_after = updated.get("tokens", 0)

            caption_footer = (
                f"\n\n💰 Списано: <code>-{fmt_tokens(cost)}</code>\n"
                f"Остаток: <code>{fmt_tokens(bal_after)}</code>"
            )

            if result_path and os.path.exists(result_path):
                doc = FSInputFile(result_path, filename=os.path.basename(result_path))
                await message.answer_document(
                    document=doc,
                    caption=(
                        f"✅ <b>Готово!</b>\n\n"
                        f"{icon} {mode.capitalize()}{caption_footer}"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Создать ещё", callback_data="gen_menu"),
                         InlineKeyboardButton(text="👤 Кабинет", callback_data="cabinet")],
                    ])
                )

            elif result_files:
                # Видео — 2 варианта
                for idx, fpath in enumerate(result_files, 1):
                    if os.path.exists(fpath):
                        vf = FSInputFile(fpath, filename=os.path.basename(fpath))
                        cap = f"🎬 <b>Вариант {idx}</b>{caption_footer if idx == len(result_files) else ''}"
                        await message.answer_document(document=vf, caption=cap, parse_mode="HTML")
                    else:
                        # URL
                        await message.answer(
                            f"🎬 <b>Вариант {idx}:</b>\n{fpath}{caption_footer if idx == len(result_files) else ''}",
                            parse_mode="HTML"
                        )

            elif result_text:
                # Текст / промпт — делим если длинный
                chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                for ci, chunk in enumerate(chunks):
                    if ci == len(chunks) - 1:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="⚡ Создать ещё", callback_data="gen_menu"),
                             InlineKeyboardButton(text="👤 Кабинет", callback_data="cabinet")],
                        ])
                        await message.answer(
                            f"✅ <b>Готово!</b>\n\n{chunk}{caption_footer}",
                            parse_mode="HTML", reply_markup=kb
                        )
                    else:
                        await message.answer(chunk, parse_mode="HTML")

            await db.add_generation_record(user_id, prompt, result_path or "", "success", mode, cost)
            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Generation error [{mode}] user={user_id}: {e}")
            # Возвращаем токены
            await db.add_tokens(user_id, cost)
            try:
                await status_msg.edit_text(
                    "❌ <b>Ошибка генерации</b>\n\n"
                    "Токены возвращены на ваш счёт.\n"
                    "Попробуйте позже или обратитесь в поддержку: @support",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="gen_menu")],
                    ])
                )
            except Exception:
                await message.answer("❌ Ошибка. Токены возвращены.", parse_mode="HTML")
            await db.add_generation_record(user_id, prompt, "", "failed", mode, 0)


# ─────────────────────────────────────────────────────────────
# ═══ ПОКУПКА ТОКЕНОВ ══════════════════════════════════════════
# ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "buy")
async def cb_buy(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PaymentStates.choosing_plan)
    try:
        await callback.message.edit_text(
            text_buy_menu(), reply_markup=kb_plans(), parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            text_buy_menu(), reply_markup=kb_plans(), parse_mode="HTML"
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("plan_"), PaymentStates.choosing_plan)
async def cb_choose_plan(callback: CallbackQuery, state: FSMContext):
    plan_key = callback.data.removeprefix("plan_")
    if plan_key not in TOKEN_PLANS:
        await callback.answer("❌ Неизвестный тариф", show_alert=True)
        return

    plan = TOKEN_PLANS[plan_key]
    await state.update_data(selected_plan=plan_key)

    await callback.message.edit_text(
        f"💎 <b>{plan['label']}</b>\n\n"
        f"📦 {fmt_tokens(plan['tokens'])} токенов\n"
        f"💰 Стоимость: <b>{plan['price']}₽</b>\n"
        f"<i>{plan['desc']}</i>\n\n"
        f"Выберите способ оплаты:",
        reply_markup=kb_pay_method(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("method_"), PaymentStates.choosing_plan)
async def cb_choose_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.removeprefix("method_")
    fsm = await state.get_data()
    plan_key = fsm.get("selected_plan")
    if not plan_key or plan_key not in TOKEN_PLANS:
        await callback.answer("❌ Ошибка. Начните заново.", show_alert=True)
        await state.clear()
        return

    plan = TOKEN_PLANS[plan_key]
    user = callback.from_user

    await callback.answer("⏳ Создаю ссылку на оплату...")

    if method == "platega":
        url, ext_id = await create_platega_payment(
            db, user.id, user.username or "", plan_key, plan["price"]
        )
    else:
        url, ext_id = await create_cryptobot_payment(db, user.id, plan_key, plan["price"])

    if url and ext_id:
        await state.update_data(payment_ext_id=ext_id, payment_method=method)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {plan['price']}₽", url=url)],
            [InlineKeyboardButton(
                text="✅ Проверить оплату",
                callback_data=f"check_pay_{ext_id}_{method}"
            )],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="buy")],
        ])
        await callback.message.edit_text(
            f"💎 <b>Оплата</b> — {plan['label']}\n\n"
            f"1️⃣ Нажмите <b>«Оплатить»</b>\n"
            f"2️⃣ Оплатите через CryptoBot\n"
            f"3️⃣ Нажмите <b>«Проверить оплату»</b>\n\n"
            f"После оплаты <code>{fmt_tokens(plan['tokens'])}</code> токенов "
            f"поступят на ваш счёт автоматически.",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка создания платежа.</b>\n\nПопробуйте позже.",
            reply_markup=kb_back("buy"),
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("check_pay_"))
async def cb_check_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    ext_id = parts[2]
    method = parts[3]
    await callback.answer("⏳ Проверяю...")

    is_paid = await check_and_activate_payment(db, ext_id, method)
    if is_paid:
        u = await db.get_user(callback.from_user.id)
        tokens = u.get("tokens", 0)
        await callback.message.edit_text(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"💰 Ваш баланс: <code>{fmt_tokens(tokens)}</code> токенов",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Создать контент", callback_data="gen_menu")],
                [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cabinet")],
            ]),
            parse_mode="HTML"
        )
    else:
        await callback.answer(
            "❌ Оплата ещё не прошла. Подождите и попробуйте снова.",
            show_alert=True
        )


# ─────────────────────────────────────────────────────────────
# ═══ РЕФЕРАЛЫ ════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "referral")
async def cb_referral(callback: CallbackQuery):
    u = await db.get_user(callback.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"

    refs = u.get("referrals_count", 0)
    to_bonus = REFERRALS_NEEDED - (refs % REFERRALS_NEEDED)

    share_text = "🤖 Генерируй лендинги, картинки и видео бесплатно с AI! Получи токены по моей ссылке:"
    share_url = f"https://t.me/share/url?url={ref_link}&text={share_text}"

    text = (
        f"👥 <b>Реферальная программа</b>\n\n"
        f"За каждых {REFERRALS_NEEDED} приглашённых друзей — "
        f"+{fmt_tokens(REFERRAL_BONUS_TOKENS)} токенов 🎁\n\n"
        f"📊 <b>Ваша статистика:</b>\n"
        f"  Приглашено: <b>{refs}</b>\n"
        f"  До бонуса: ещё <b>{to_bonus}</b>\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n"
        f"<code>{ref_link}</code>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Поделиться ссылкой", url=share_url)],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")],
    ])

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        if os.path.exists(REF_PHOTO_PATH):
            await bot.send_photo(
                callback.message.chat.id,
                photo=FSInputFile(REF_PHOTO_PATH),
                caption=text, reply_markup=kb, parse_mode="HTML"
            )
        else:
            await bot.send_message(callback.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Referral send error: {e}")
        await bot.send_message(callback.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

    await callback.answer()


# ─────────────────────────────────────────────────────────────
# ═══ ПРОМОКОДЫ ═══════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "promo")
async def cb_promo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_promocode)
    try:
        await callback.message.edit_text(
            "🎫 <b>Активация промокода</b>\n\nВведите промокод:",
            reply_markup=kb_back(),
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            "🎫 <b>Активация промокода</b>\n\nВведите промокод:",
            reply_markup=kb_back(),
            parse_mode="HTML"
        )
    await callback.answer()


@dp.message(AdminStates.waiting_promocode)
async def process_promocode(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()

    if await is_admin(user_id):
        parts = message.text.split()
        # Формат: КОД ТОКЕНЫ МАКС_ИСП ДНЕЙ
        if len(parts) == 4:
            try:
                code, tokens_str, max_uses, days = (
                    parts[0].upper(), int(parts[1]), int(parts[2]), int(parts[3])
                )
                expires = int(time.time()) + days * 86400
                success = await db.create_promocode(code, tokens_str, max_uses, expires)
                if success:
                    await message.answer(
                        f"✅ <b>Промокод создан!</b>\n\n"
                        f"Код: <code>{code}</code>\n"
                        f"Токены: {fmt_tokens(tokens_str)}\n"
                        f"Макс. использований: {max_uses}\n"
                        f"Дней: {days}",
                        parse_mode="HTML"
                    )
                else:
                    await message.answer("❌ Такой промокод уже существует.", parse_mode="HTML")
                return
            except (ValueError, IndexError):
                await message.answer(
                    "❌ Формат: <code>КОД ТОКЕНЫ МАКС_ИСП ДНЕЙ</code>\n"
                    "Пример: <code>PROMO2024 1000000 100 30</code>",
                    parse_mode="HTML"
                )
                return

    success, msg = await db.use_promocode(user_id, message.text.strip())
    await message.answer(msg, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────
# ═══ АДМИНКА ═════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "admin")
async def cb_admin(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            "🔧 <b>Панель администратора</b>",
            reply_markup=kb_admin(), parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer("🔧 <b>Панель администратора</b>", reply_markup=kb_admin(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    s = await db.get_stats()
    await callback.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: {s['total_users']}\n"
        f"🆕 Новых за 24ч: {s['new_users_24h']}\n"
        f"🔥 Активных за 24ч: {s['active_users_24h']}\n\n"
        f"⚙️ Генераций всего: {s['total_generations']}\n"
        f"✅ Успешных: {s['successful_generations']}\n\n"
        f"💎 Токенов продано: {fmt_tokens(s.get('total_tokens_issued', 0))}\n"
        f"🎫 Промокодов: {s['total_promocodes']} / использовано: {s['used_promocodes']}",
        reply_markup=kb_admin(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_top_refs")
async def cb_admin_top_refs(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    top = await db.get_top_referrers(limit=10)
    lines = ["👑 <b>Топ 10 рефералов:</b>\n"]
    for i, u in enumerate(top, 1):
        uname = f"@{u['username']}" if u.get("username") else "—"
        lines.append(f"{i}. {u.get('first_name', '?')} ({uname}) — {u['referrals_count']}")
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=kb_admin(), parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_create_promo")
async def cb_admin_create_promo(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_promocode)
    await callback.message.edit_text(
        "🎫 <b>Создание промокода</b>\n\n"
        "Формат: <code>КОД ТОКЕНЫ МАКС_ИСП ДНЕЙ</code>\n"
        "Пример: <code>BONUS2024 1000000 100 30</code>\n\n"
        "<i>1000000 = 1М токенов</i>",
        reply_markup=kb_back("admin"),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправьте сообщение для рассылки всем пользователям:",
        reply_markup=kb_back("admin"),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    all_users = await db.get_all_user_ids()
    status = await message.answer(f"📢 Рассылка... Получателей: {len(all_users)}")
    sent = failed = 0
    for uid in all_users:
        try:
            await bot.send_message(uid, message.text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    await status.edit_text(f"✅ Рассылка завершена!\nУспешно: {sent} | Ошибок: {failed}")
    await state.clear()


# ─────────────────────────────────────────────────────────────
# Команды-алиасы
# ─────────────────────────────────────────────────────────────
@dp.message(Command("generate"))
async def cmd_generate(message: Message, state: FSMContext):
    u = await db.get_user(message.from_user.id)
    tokens = (u or {}).get("tokens", 0)
    await message.answer(
        text_gen_menu(tokens),
        reply_markup=kb_generate_menu(tokens),
        parse_mode="HTML"
    )
    await state.set_state(GenStates.choose_mode)


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    u = await db.get_user(message.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    refs = (u or {}).get("referrals_count", 0)
    to_bonus = REFERRALS_NEEDED - (refs % REFERRALS_NEEDED)
    await message.answer(
        f"👥 <b>Рефералы</b>\n\nПриглашено: {refs}\nДо бонуса: {to_bonus}\n\n"
        f"Ссылка: <code>{ref_link}</code>",
        parse_mode="HTML"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_admin(message.from_user.id):
        return
    s = await db.get_stats()
    await message.answer(
        f"📊 Пользователей: {s['total_users']} | Генераций: {s['total_generations']}",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────────────────────
# Echo
# ─────────────────────────────────────────────────────────────
@dp.message()
async def echo(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return  # в FSM — пусть FSM обрабатывает
    await message.answer(
        "❓ Используйте меню ниже или /help",
        reply_markup=kb_main(message.from_user.id)
    )


# ─────────────────────────────────────────────────────────────
# Реферальные уведомления
# ─────────────────────────────────────────────────────────────
async def _schedule_ref_notifications(user_id: int):
    asyncio.create_task(_delayed_ref_notif(user_id, "5m",  5 * 60))
    asyncio.create_task(_delayed_ref_notif(user_id, "12h", 12 * 3600))
    asyncio.create_task(_delayed_ref_notif(user_id, "24h", 24 * 3600))


async def _delayed_ref_notif(user_id: int, notif_type: str, delay: int):
    await asyncio.sleep(delay)
    u = await db.get_user(user_id)
    if not u or u.get(f"referral_notified_{notif_type}"):
        return
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    share_url = f"https://t.me/share/url?url={ref_link}&text=🤖 Генерируй AI-контент бесплатно!"
    texts = {
        "5m":  f"⚡️ Получи токены бесплатно!\n\nПригласи {REFERRALS_NEEDED} друзей → +{fmt_tokens(REFERRAL_BONUS_TOKENS)} токенов 🎁",
        "12h": f"💡 Совет: зарабатывай токены!\n\nЗа каждых {REFERRALS_NEEDED} приглашённых — {fmt_tokens(REFERRAL_BONUS_TOKENS)} токенов бесплатно 📈",
        "24h": f"🎁 Не упусти бонус!\n\nТвоя реферальная ссылка ждёт. Поделись с друзьями! 🚀",
    }
    text = texts.get(notif_type, "")
    if not text:
        return
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Пригласить друзей", url=share_url)],
        ])
        if os.path.exists(REF_PHOTO_PATH):
            await bot.send_photo(user_id, FSInputFile(REF_PHOTO_PATH), caption=text, reply_markup=kb)
        else:
            await bot.send_message(user_id, text, reply_markup=kb)

        # Помечаем как отправленное
        import aiosqlite
        async with aiosqlite.connect(db.db_path) as conn:
            col = f"referral_notified_{notif_type}"
            await conn.execute(f"UPDATE users SET {col} = 1 WHERE user_id = ?", (user_id,))
            await conn.commit()
    except Exception as e:
        logger.error(f"Ref notif {notif_type} → {user_id}: {e}")


async def reschedule_missed_notifications():
    now = int(time.time())
    all_users = await db.get_all_user_ids()
    for uid in all_users:
        u = await db.get_user(uid)
        if not u:
            continue
        age = now - u.get("created_at", now)
        if age > 86400:
            continue
        if not u.get("referral_notified_5m"):
            asyncio.create_task(_delayed_ref_notif(uid, "5m",  max(0, 300 - age)))
        if not u.get("referral_notified_12h"):
            asyncio.create_task(_delayed_ref_notif(uid, "12h", max(0, 43200 - age)))
        if not u.get("referral_notified_24h"):
            asyncio.create_task(_delayed_ref_notif(uid, "24h", max(0, 86400 - age)))


# ─────────────────────────────────────────────────────────────
# Startup / Shutdown / Main
# ─────────────────────────────────────────────────────────────
async def on_startup():
    await db.init_db()
    await generator.init_browser()
    asyncio.create_task(reschedule_missed_notifications())
    logger.info("✅ Bot started")


async def on_shutdown():
    await generator.close_browser()
    logger.info("Bot stopped")


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    logger.info("Starting bot...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())