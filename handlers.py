import os
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# Импортируем функции работы с базой данных
from database.supabase_db import (
    get_user_by_id_or_username,
    create_moderation_request,
    get_pending_requests,
    update_request_status,
    add_or_update_scammer_by_id
)

router = Router()

# Твой жестко прописанный ADMIN_ID
ADMIN_ID = 6176762600

class ReportStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()


async def resolve_user_data(bot: Bot, raw_input: str):
    raw_input = raw_input.strip()
    if raw_input.isdigit():
        return int(raw_input), None
    
    cleaned = raw_input.replace("@", "").strip()
    try:
        chat = await bot.get_chat(f"@{cleaned}")
        return chat.id, chat.username or cleaned
    except Exception:
        return None, cleaned


def is_valid_text(text: str) -> bool:
    return bool(text and len(text.strip()) >= 2)


# Твоя родная функция уведомления админа
async def notify_admin_new_request(bot: Bot, reporter_name: str):
    if ADMIN_ID == 0:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 Да", callback_data="admin_view_now")],
        [InlineKeyboardButton(text="❌ Нет, потом", callback_data="admin_close_notify")]
    ])
    try:
        await bot.send_message(
            chat_id=ADMIN_ID, 
            text=f"Админ, тут есть новые запросы в скам базу. Смотрим?", 
            reply_markup=kb
        )
    except Exception as e:
        print(f"Не удалось отправить уведомление админу: {e}")


# =====================================================================
# ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ
# =====================================================================

@router.message(F.text == "🔍 Проверить пользователя")
async def ask_user_to_check(message: Message):
    await message.answer("Введите @username пользователя или его цифровой Telegram ID, которого вы хотите проверить:")


@router.message(F.text.startswith("@") | F.text.regexp(r"^\d+$"))
async def check_user_in_db(message: Message, bot: Bot):
    raw_input = message.text.strip()
    user_id, username = await resolve_user_data(bot, raw_input)
    
    scammer = await get_user_by_id_or_username(user_id=user_id, username=username or raw_input)
    
    if scammer:
        text = (
            f"🚨 [ВНИМАНИЕ! ПОЛЬЗОВАТЕЛЬ НАЙДЕН В БАЗЕ]\n\n"
            f"👤 Юзернейм: @{scammer.get('current_username', 'Не указан')}\n"
            f"🆔 Telegram ID: `{scammer.get('user_id', 'Скрыт')}`\n\n"
            f"📊 Текущий рейтинг жалоб:\n"
            f"🤡 Клоун: {scammer.get('clown_count', 0)}\n"
            f"🤔 Подозреваемый: {scammer.get('suspect_count', 0)}\n"
            f"❤️ Гуд (Доверие): {scammer.get('good_count', 0)}\n"
        )
        if scammer.get('has_proof'):
            text += f"\n📄 Подтвержденный пруф:\n{scammer.get('proof_text', 'Без описания')}"
        await message.answer(text, parse_mode="Markdown")
    else:
        display_name = f"@{username}" if username else raw_input
        await message.answer(f"*[ОТСУТСТВУЕТ В БАЗЕ]*\n\nПользователь {display_name} не найден в базе данных Анти-Скам.", parse_mode="Markdown")


# =====================================================================
# СОЗДАНИЕ ЖАЛОБЫ
# =====================================================================

@router.message(F.text == "🚨 Сообщить о пользователе")
async def start_report(message: Message, state: FSMContext):
    await state.set_state(ReportStates.waiting_for_username)
    await message.answer("Введите @username пользователя (или его ID), на которого хотите отправить жалобу:")


@router.message(ReportStates.waiting_for_username, F.chat.type == "private")
async def save_reported_username(message: Message, state: FSMContext, bot: Bot):
    raw_input = message.text.strip()
    user_id, cleaned_name = await resolve_user_data(bot, raw_input)
    
    db_username = (cleaned_name or raw_input).replace("@", "").lower().strip()
    display_name = f"@{db_username}"
    
    await state.update_data(target_user_str=db_username, target_user_id=user_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤡 Клоун", callback_data="set_type:rating_clown"),
            InlineKeyboardButton(text="🤔 Подозреваемый", callback_data="set_type:rating_suspect"),
            InlineKeyboardButton(text="❤️ Гуд", callback_data="set_type:rating_good")
        ],
        [InlineKeyboardButton(text="📄 Железный Пруф", callback_data="set_type:proof")]
    ])
    await message.answer(f"Объект модерации определен как: {display_name}\nВыбери тип рейтинга или жалобы:", reply_markup=kb)


@router.callback_query(F.data.startswith("set_type:"))
async def set_report_type(callback: CallbackQuery, state: FSMContext):
    req_type = callback.data.split(":")[1]
    await state.update_data(req_type=req_type)
    
    await callback.message.delete()
    await state.set_state(ReportStates.waiting_for_reason)
    await callback.message.answer("Опишите причину, почему вы решили выставить этот рейтинг:")
    await callback.answer()


@router.message(ReportStates.waiting_for_reason, F.chat.type == "private")
async def process_rating_reason(message: Message, state: FSMContext, bot: Bot):
    if not is_valid_text(message.text):
        await message.answer("Причина слишком короткая. Напишите подробнее:")
        return
        
    data = await state.get_data()
    await state.clear()
    
    await create_moderation_request(
        chat_id=message.chat.id, 
        target_user=data["target_user_str"],
        target_user_id=data.get("target_user_id"), 
        reporter_id=message.from_user.id, 
        reporter_name=message.from_user.full_name,
        req_type=data["req_type"], 
        reason=message.text
    )
    
    await message.answer("Заявка на выставление рейтинга отправлена на модерацию админу.")
    # Вызов уведомления админа
    await notify_admin_new_request(bot, message.from_user.full_name)


# =====================================================================
# ПАНЕЛЬ МОДЕРАТОРА
# =====================================================================

@router.message(F.text == "⚙️ Панель Модератора")
@router.callback_query(F.data == "admin_view_now")
async def admin_view_requests(event):
    is_callback = isinstance(event, CallbackQuery)
    message = event.message if is_callback else event

    requests = await get_pending_requests()
    if not requests:
        if is_callback:
            await event.message.edit_text("Все заявки разобраны!")
            await event.answer()
        else:
            await message.answer("Все заявки разобраны!")
        return

    req = requests[0]
    req_id = req['id']
    
    types_map = {
        "rating_clown": "🤡 Клоун",
        "rating_suspect": "🤔 Подозреваемый",
        "rating_good": "❤️ Гуд",
        "proof": "📄 Железный Пруф"
    }
    human_type = types_map.get(req['req_type'], req['req_type'])

    text = (
        f"📋 *Новая заявка #{req_id}*\n"
        f"👤 На кого: @{req['target_username']} (ID: {req.get('target_user_id') or 'Не найден'})\n"
        f"🏷️ Тип оценки: {human_type}\n"
        f"📝 От кого: {req['reporter_name']} (ID: {req['reporter_id']})\n"
        f"💬 Описание: {req['reason']}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{req_id}")
        ]
    ])
    
    if is_callback:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await event.answer()
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def handle_decision(callback: CallbackQuery):
    action, req_id = callback.data.split("_")
    req_id = int(req_id)
    
    # 1. Сначала берем заявку, пока она со статусом 'pending'
    requests = await get_pending_requests()
    current_req = next((r for r in requests if r['id'] == req_id), None)
    
    if action == "approve":
        if current_req:
            is_proof = current_req['req_type'] == "proof"
            
            # 2. СНАЧАЛА переносим данные в таблицу скамеров
            await add_or_update_scammer_by_id(
                user_id=current_req.get('target_user_id'), 
                username=current_req['target_username'],
                req_type=current_req['req_type'], 
                proof_text=current_req['reason'] if is_proof else None,
                has_proof=is_proof
            )
            
            # 3. И только после записи меняем статус самой заявки на approved
            await update_request_status(req_id, "approved")
            msg_text = f"Заявка #{req_id} успешно подтверждена и внесена в базу Анти-Скам."
        else:
            msg_text = "Ошибка: не удалось получить данные заявки до одобрения."
    else:
        await update_request_status(req_id, "rejected")
        msg_text = "Заявка успешно отклонена."
        
    await callback.message.delete()
    await callback.message.answer(msg_text)
    
    next_requests = await get_pending_requests()
    if next_requests:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Показать следующую ➡️", callback_data="admin_view_now")]
        ])
        await callback.message.answer("В очереди есть еще заявки.", reply_markup=kb)
    else:
        await callback.message.answer("Все заявки разобраны!")
        
    await callback.answer()


@router.callback_query(F.data == "admin_close_notify")
async def close_admin_notification(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer("Уведомление закрыто.")
