import os
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from database.supabase_db import (
    create_moderation_request, get_pending_requests, 
    update_request_status, get_user_from_base, add_or_update_scammer
)

router = Router()
ADMIN_ID = 6176762600

class ReportStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()
    waiting_for_proof = State()

def is_valid_text(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    return len(cleaned) > 0

# Расчет процентного рейтинга и вывод титула
async def check_user_and_format_response(username: str) -> str:
    username = username.strip()
    if not username.startswith("@"):
        username = f"@{username}"
        
    user = await get_user_from_base(username)
    
    if not user:
        return f"[ОТСУТСТВУЕТ В БАЗЕ]\n\nПользователь {username} не найден в базе данных Анти-Скам."
        
    total_negative = user["clown_count"] + user["suspect_count"]
    total_votes = total_negative + user["good_count"]
    
    # Расчет вероятности скама
    scam_percentage = 0
    if total_votes > 0:
        # Клоун весит 100% негатива, Подозрение 50%
        scam_weight = (user["clown_count"] * 100) + (user["suspect_count"] * 50)
        scam_percentage = min(100, int(scam_weight / total_votes))
        
    # Корректируем если железно есть пруфы скама
    if user["has_proof"]:
        scam_percentage = max(scam_percentage, 95)
        
    # Определяем титул сообщения
    if user["has_proof"] or user["clown_count"] > 0 or user["suspect_count"] > 0:
        title = "[ВОЗМОЖЕН СКАМ!]"
    else:
        title = "[ЧИСТЫЙ ПОЛЬЗОВАТЕЛЬ]"
        
    proof_status = f"Да (Текст: {user['proof_text']})" if user["has_proof"] else "Нет"
    
    return (
        f"{title}\n\n"
        f"Информация о пользователе: {username}\n"
        f"Процент вероятности скама: {scam_percentage}%\n\n"
        f"Рейтинг по реакциям:\n"
        f"🤔 Подозрение: {user['suspect_count']} раз(а)\n"
        f"🤡 Клоун (Скамер): {user['clown_count']} раз(а)\n"
        f"❤️ Лучший: {user['good_count']} раз(а)\n\n"
        f"Наличие прямых доказательств (пруфов): {proof_status}"
    )

# --- РАБОТА В ГРУППАХ И ЛС (ТОЛЬКО ПРОВЕРКА) ---
@router.message(F.text.lower.contains("скам"))
async def process_scam_check(message: Message):
    text_parts = message.text.split()
    target_user = None
    
    for part in text_parts:
        if part.startswith("@") and len(part) > 1:
            target_user = part
            break
            
    if not target_user and message.reply_to_message:
        if message.reply_to_message.from_user.username:
            target_user = f"@{message.reply_to_message.from_user.username}"
        else:
            target_user = message.reply_to_message.from_user.full_name

    if not target_user:
        return 

    response_text = await check_user_and_format_response(target_user)
    await message.reply(response_text)

# --- ПРИВЕТСТВИЕ ПРИ ДОБАВЛЕНИИ В ЧАТ ---
@router.my_chat_member()
async def bot_added_to_chat(event: ChatMemberUpdated):
    if event.new_chat_member.status in ["member", "administrator"]:
        if event.old_chat_member.status in ["member", "administrator"]:
            return
        await event.bot.send_message(
            chat_id=event.chat.id,
            text=f"Привет, чат «{event.chat.title}»! Я бот-антискам. 🛡\nЧтобы быстро проверить любого юзера, напишите: `скам @username` или ответьте словом `скам` на его сообщение."
        )

# --- ГЛАВНОЕ МЕНЮ В ЛИЧКЕ (КНОПКИ СТАРТА) ---
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start_private(message: Message):
    buttons = [
        [InlineKeyboardButton(text="Сообщить о пользователе", callback_data="user_report_start")]
    ]
    # Если зашел админ, добавляем кнопку модерации
    if message.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="Заявки (Админ)", callback_data="admin_view_now")])
        
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Добро пожаловать в главное меню бота Анти-Скам!", reply_markup=kb)

# Начало процесса подачи жалобы
@router.callback_query(F.data == "user_report_start")
async def report_username_step(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username пользователя, на которого хотите отправить жалобу/рейтинг:")
    await state.set_state(ReportStates.waiting_for_username)
    await callback.answer()

@router.message(ReportStates.waiting_for_username, F.chat.type == "private")
async def save_reported_username(message: Message, state: FSMContext):
    if not message.text.startswith("@"):
        await message.answer("Юзернейм должен начинаться с @. Попробуйте еще раз:")
        return
        
    await state.update_data(target_user=message.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Реакцией", callback_data="report_by_rating"),
         InlineKeyboardButton(text="Пруфом", callback_data="report_by_proof")]
    ])
    await message.answer("Как вы хотите сообщить об этом пользователе?", reply_markup=kb)

# Сценарий 1: Реакция
@router.callback_query(F.data == "report_by_rating")
async def choose_rating_type(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤔 Подозрение", callback_data="set_rate_suspect"),
         InlineKeyboardButton(text="🤡 Клоун", callback_data="set_rate_clown"),
         InlineKeyboardButton(text="❤️ Лучший", callback_data="set_rate_good")]
    ])
    await callback.message.edit_text("Выберите реакцию для пользователя:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("set_rate_"))
async def get_rating_reason(callback: CallbackQuery, state: FSMContext):
    rate_type = callback.data.replace("set_rate_", "rating_")
    await state.update_data(req_type=rate_type)
    
    await callback.message.edit_text(
        f"Опишите причину, почему вы решили выставить этот рейтинг?\n"
        f"(Без причины реакция не применяется)"
    )
    await state.set_state(ReportStates.waiting_for_reason)
    await callback.answer()

@router.message(ReportStates.waiting_for_reason, F.chat.type == "private")
async def process_rating_reason(message: Message, state: FSMContext, bot: Bot):
    if not is_valid_text(message.text):
        await message.answer("Причина не может состоять из невидимых символов. Напишите нормально:")
        return
        
    data = await state.get_data()
    await state.clear()
    
    await create_moderation_request(
        chat_id=message.chat.id, target_user=data["target_user"],
        reporter_id=message.from_user.id, reporter_name=message.from_user.full_name,
        req_type=data["req_type"], reason=message.text
    )
    
    await message.answer("Заявка на выставление рейтинга отправлена на модерацию админу.")
    await notify_admin_new_request(bot, message.from_user.full_name)

# Сценарий 2: Пруф
@router.callback_query(F.data == "report_by_proof")
async def ask_for_proof_data(callback: CallbackQuery, state: FSMContext):
    await state.update_data(req_type="proof")
    await callback.message.edit_text("Пришлите доказательства скама (текст, фото, видео или медиа с описанием):")
    await state.set_state(ReportStates.waiting_for_proof)
    await callback.answer()

@router.message(ReportStates.waiting_for_proof, F.chat.type == "private")
async def process_proof_file_or_text(message: Message, state: FSMContext, bot: Bot):
    reason_text = message.text or message.caption or "Пруф без текстового описания"
    media_id = None
    
    if message.photo:
        media_id = message.photo[-1].file_id
    elif message.video:
        media_id = message.video.file_id
    elif message.document:
        media_id = message.document.file_id
        
    if not media_id and not message.text:
        await message.answer("Отправьте корректное медиа или текст!")
        return
        
    data = await state.get_data()
    await state.clear()
    
    await create_moderation_request(
        chat_id=message.chat.id, target_user=data["target_user"],
        reporter_id=message.from_user.id, reporter_name=message.from_user.full_name,
        req_type="proof", reason=reason_text, media_file_id=media_id
    )
    
    await message.answer("Доказательства скама успешно отправлены админу на проверку.")
    await notify_admin_new_request(bot, message.from_user.full_name)

async def notify_admin_new_request(bot: Bot, name: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 Да", callback_data="admin_view_now"),
         InlineKeyboardButton(text="❌ Нет, потом", callback_data="admin_view_later")]
    ])
    await bot.send_message(
        ADMIN_ID, f"{name}, тут есть запросы на внесение данных в скам базу\nхочешь посмотреть или позже?", reply_markup=kb
    )

# --- АДМИНКА ---
@router.callback_query(F.data == "admin_view_later")
async def view_later(callback: CallbackQuery):
    await callback.message.edit_text("Понял, заявки можно посмотреть в любое время через Главное меню кнопкой «Заявки»")
    await callback.answer()

@router.callback_query(F.data == "admin_view_now")
async def show_moderation_queue(callback: CallbackQuery):
    requests = await get_pending_requests()
    if not requests:
        await callback.message.edit_text("Все заявки разобраны!")
        await callback.answer()
        return
    
    req = requests[0]
    rating_types = {"rating_clown": "🤡 Клоун", "rating_suspect": "🤔 Подозрение", "rating_good": "❤️ Лучший", "proof": "📁 Пруф/Доказательство"}
    type_display = rating_types.get(req['req_type'], req['req_type'])
    
    text = f"Заявка на рейтинг пользователю: {req['target_username']}\nРеакция: {type_display}\nПричина: {req['reason']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять", callback_data=f"approve_{req['id']}"),
         InlineKeyboardButton(text="Отказать", callback_data=f"reject_{req['id']}")]
    ])
    
    if req['media_file_id']:
        await callback.message.delete()
        await callback.message.answer_photo(photo=req['media_file_id'], caption=text, reply_markup=kb)
    else:
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def handle_decision(callback: CallbackQuery):
    action, req_id = callback.data.split("_")
    req_id = int(req_id)
    
    requests = await get_pending_requests()
    current_req = next((r for r in requests if r['id'] == req_id), None)
    
    if action == "approve" and current_req:
        await update_request_status(req_id, "approved")
        is_proof = current_req['req_type'] == "proof"
        await add_or_update_scammer(
            username=current_req['target_username'],
            req_type=current_req['req_type'],
            proof_text=current_req['reason'] if is_proof else None,
            has_proof=is_proof
        )
        msg_text = "Понял, заявка принята и была добавлена в базу данных."
    else:
        await update_request_status(req_id, "rejected")
        msg_text = "Хорошо, заявка отказана и не была добавлена."
        
    await callback.message.delete()
    await callback.message.answer(msg_text)
    
    next_requests = await get_pending_requests()
    if next_requests:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Показать следующую ➡️", callback_data="admin_view_now")]])
        await callback.message.answer("В очереди есть еще необработанные заявки.", reply_markup=kb)
    await callback.answer()
