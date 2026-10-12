from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database import Database
from config import ADMIN_IDS
import time


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def admin_stats(message: types.Message, state: FSMContext = None):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    db = Database()
    stats = await db.get_stats()
    
    text = f"""
📊 Статистика бота

👥 Пользователи:
• Всего: {stats['total_users']}
• Новых за 24ч: {stats['new_users_24h']}
• Активных за 24ч: {stats['active_users_24h']}

🎨 Генерации:
• Всего: {stats['total_generations']}
• Успешных: {stats['successful_generations']}
• Процент успеха: {stats['successful_generations']/max(stats['total_generations'], 1)*100:.1f}%

🎫 Промокоды:
• Создано: {stats['total_promocodes']}
• Использовано: {stats['used_promocodes']}
"""
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔝 Топ рефереров", callback_data="admin_top_refs")],
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promo")],
    ])
    
    await message.answer(text, reply_markup=keyboard)


async def admin_top_referrers(update, context):
    query = update.callback_query
    await query.answer()
    
    if not await is_admin(query.from_user.id):
        return
    
    db = Database()
    top = await db.get_top_referrers(10)
    
    message = "🔝 Топ-10 рефереров:\n\n"
    
    for i, user in enumerate(top, 1):
        username = user['username'] or user['first_name']
        message += f"{i}. @{username} - {user['referrals_count']} рефералов\n"
    
    if not top:
        message = "Пока нет рефералов"
    
    await query.message.edit_text(message)


async def admin_create_promocode_start(update, context):
    query = update.callback_query
    await query.answer()
    
    if not await is_admin(query.from_user.id):
        return
    
    await query.message.edit_text(
        "Создание промокода\n\n"
        "Отправьте данные в формате:\n"
        "<code>КОД ГЕНЕРАЦИИ МАКС_ИСПОЛЬЗОВАНИЙ ДНЕЙ_ДЕЙСТВИЯ</code>\n\n"
        "Пример:\n"
        "<code>PROMO2024 5 100 30</code>\n"
        "(код PROMO2024, 5 генераций, 100 использований, 30 дней)",
        parse_mode='HTML'
    )
    
    # Состояние устанавливается в bot.py


async def handle_promocode_creation(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 4:
            await message.answer(
                "❌ Неверный формат. Используйте:\n"
                "<code>КОД ГЕНЕРАЦИИ МАКС_ИСПОЛЬЗОВАНИЙ ДНЕЙ</code>",
                parse_mode='HTML'
            )
            return
        
        code, generations, max_uses, days = parts
        generations = int(generations)
        max_uses = int(max_uses)
        days = int(days)
        
        expires_at = int(time.time()) + (days * 86400)
        
        db = Database()
        success = await db.create_promocode(code, generations, max_uses, expires_at)
        
        if success:
            await message.answer(
                f"✅ Промокод создан!\n\n"
                f"Код: <code>{code}</code>\n"
                f"Генераций: {generations}\n"
                f"Макс. использований: {max_uses}\n"
                f"Действует: {days} дней",
                parse_mode='HTML'
            )
        else:
            await message.answer("❌ Такой промокод уже существует")
        
    except ValueError:
        await message.answer("❌ Ошибка в числовых значениях")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def admin_broadcast_start(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    await message.answer(
        "📢 Рассылка\n\n"
        "Отправьте сообщение, которое хотите разослать всем пользователям:"
    )
    
    await state.set_state("admin_broadcast_waiting")


async def handle_broadcast(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    
    db = Database()
    user_ids = await db.get_all_user_ids()
    
    message_text = message.text
    success = 0
    failed = 0
    
    status_message = await message.answer("Начинаю рассылку...")
    
    for user_id in user_ids:
        try:
            await message.bot.send_message(user_id, message_text)
            success += 1
        except Exception:
            failed += 1
        
        if (success + failed) % 10 == 0:
            await status_message.edit_text(
                f"Рассылка...\n"
                f"Отправлено: {success}\n"
                f"Ошибок: {failed}"
            )
    
    await status_message.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"Отправлено: {success}\n"
        f"Ошибок: {failed}"
    )
    
    await state.clear()