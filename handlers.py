import os
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    ChatMemberUpdated
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# Предполагаем, что ты обновишь функции в supabase_db для поддержки user_id
from database.supabase_db import (
    create_moderation_request, 
    get_pending_requests, 
    update_request_status, 
    get_user_by_id_or_username, # Новая/обновленная функция поиска
    add_or_update_scammer_by_id  # Новая/обновленная функция записи
)

router = Router()
ADMIN_ID = 6176762600

class ReportStates(StatesGroup):
    waiting_for_check_username = State()
    waiting_for_username = State()
    waiting_for_reason = State()
    waiting_for_proof = State()

def is_valid_text(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    return len(cleaned) > 0

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ ID ИЗ ЮЗЕРНЕЙМА ---
async def resolve_user_data(bot: Bot, username_or_id: str):
    """
    Пытается определить реальный цифровой ID и актуальный юзернейм.
    Принимает строку вида '@R0bONe', 'R0bONE' или числовой ID.
    """
    target = username_or_id.strip()
    
    # Если это уже числовой ID
    if target.isdigit():
        try:
            chat = await bot.get_chat(int(target))
            return chat.id, f"@{chat.username}" if chat.username else chat.full_name
        except Exception:
            return int(target), None

    # Если передан юзернейм, очищаем его и переводим в нижний регистр для единообразия
    cleaned_username = target.replace("@", "").strip()
    
    try:
        # Telegram позволяет боту запросить информацию о чате/юзернейме напрямую
        chat = await bot.get_chat(f"@{cleaned_username}")
        return chat.id, f"@{chat.username}" if chat.username else f"@{cleaned_username}"
    except Exception as e:
        # Если бот лично не сталкивался с юзером или его нет в общих чатах, get_chat может выдать ошибку.
        # В таком случае возвращаем None вместо ID, но сохраняем очищенный lowercase-юзернейм для поиска по старой схеме.
        print(f"Предупреждение resolve_user_data: не удалось получить ID для @{cleaned_username}: {e}")
        return None, f"@{cleaned_username.lower()}"


# --- ФУНКЦИЯ РАСЧЕТА РЕЙТИНГА И СБОРА ТИТУЛА (ПО ID / LOWERCASE ЮЗЕРУ) ---
async def check_user_and_format_response(bot: Bot, username_or_id: str) -> str:
    user_id, current_name = await resolve_user_data(bot, username_or_id)
    
    # Ищем в базе данных. Функция должна уметь искать И по целому user_id, И по тексту username
    user = await get_user_by_id_or_username(user_id=user_id, username=current_name)
    
    display_name = current_name if current_name else username_or_id
    
    if not user:
        return f"[ОТСУТСТВУЕТ В БАЗЕ]\n\nПользователь {display_name} не найден в базе данных Анти-Скам."
        
    total_negative = user.get("clown_count", 0) + user.get("suspect_count", 0)
    total_votes = total_negative + user.get("good_count", 0)
    
    scam_percentage = 0
    if total_votes > 0:
        scam_weight = (user.get("clown_count", 0) * 100) + (user.get("suspect_count", 0) * 50)
        scam_percentage = min(100, int(scam_weight / total_votes))
        
    if user.get("has_proof"):
        scam_percentage = max(scam_percentage, 95)
        
    if user.get("has_proof") or user.get("clown_count", 0) > 0 or user.get("suspect_count", 0) > 0:
        title = "[ВОЗМОЖЕН СКАМ!]"
    else:
        title = "[ЧИСТЫЙ ПОЛЬЗОВАТЕЛЬ]"
        
    proof_status = f"Да (Текст: {user['proof_text']})" if user.get("has_proof") else "Нет"
    
    # Отображаем красивое имя (из базы или актуальное), но внутренне всё связано по ID
    final_username = user.get("current_username", display_name)
    
    return (
        f"{title}\n\n"
        f"Информация о пользователе: {final_username}\n"
        f"Уникальный Telegram ID: `{user_id or 'Скрыт/Не найден'}`\n"
        f"Процент вероятности скама: {scam_percentage}%\n\n"
        f"Рейтинг по реакциям:\n"
        f"🤔 Подозрение: {user.get('suspect_count', 0)} раз(а)\n"
        f"🤡 Клоун (Скамер): {user.get('clown_count', 0)} раз(а)\n"
        f"❤️ Лучший: {user.get('good_count', 0)} раз(а)\n\n"
        f"Наличие прямых доказательств (пруфов): {proof_status}"
    )


# --- РАБОТА В ГРУППАХ ---
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def process_scam_check_group(message: Message, bot: Bot):
    if not message.text and not message.caption:
        return

    full_text = (message.text or message.caption or "").strip()
    if "скам" not in full_text.lower():
        return

    target_user_str = None

    # Способ 1: Извлекаем из сущностей упоминания
    msg_entities = message.entities or message.caption_entities
    if msg_entities:
        for entity in msg_entities:
            if entity.type == "mention":
                target_user_str = full_text[entity.offset:entity.offset + entity.length]
                break

    # Способ 2: Перебор слов
    if not target_user_str:
        words = full_text.split()
        for word in words:
            if "@" in word:
                cleaned_word = word.strip(".,!?()[]{}'")
                idx = cleaned_word.find("@")
                if idx != -1:
                    target_user_str = cleaned_word[idx:]
                    break
            
    # Способ 3: Из реплея (Самый точный способ получить ID мгновенно!)
    if not target_user_str and message.reply_to_message:
        reply_msg = message.reply_to_message
        if reply_msg.from_user:
            # Если ответили на человека, мы сразу железно знаем его цифровой ID!
            target_user_str = str(reply_msg.from_user.id)
        elif reply_msg.sender_chat:
            target_user_str = str(reply_msg.sender_chat.id)

    if not target_user_str:
        return

    try:
        response_text = await check_user_and_format_response(bot, target_user_str)
        await message.reply(response_text)
    except Exception as e:
        print(f"Ошибка при обработке скам-запроса в группе: {e}")


@router.message(F.chat.type.in_({"group", "supergroup"}), Command("start"))
async def cmd_start_group(message: Message):
    await message.reply("Я запущен и работаю в этой группе. Нажмите кнопку в личке или введите `скам @username`.")


@router.my_chat_member()
async def bot_added_to_chat(event: ChatMemberUpdated):
    if event.new_chat_member.status in ["member", "administrator"]:
        if event.old_chat_member.status in ["member", "administrator"]:
            return
        await event.bot.send_message(
            chat_id=event.chat.id,
            text=f"Привет, чат «{event.chat.title}»! Я бот-антискам. 🛡\nЧтобы быстро проверить любого юзера, напишите: `скам @username` или ответьте словом `скам` на его сообщение."
        )


# --- РАБОТА В ЛИЧКЕ ---
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start_private(message: Message, state: FSMContext):
    await state.clear()
    
    keyboard_buttons = [
        [KeyboardButton(text="🔍 Проверить пользователя")],
        [KeyboardButton(text="🚨 Сообщить о пользователе")]
    ]
    
    if message.from_user.id == ADMIN_ID:
        keyboard_buttons.append([KeyboardButton(text="⚙️ Панель Модератора")])
        
    main_menu_keyboard = ReplyKeyboardMarkup(
        keyboard=keyboard_buttons,
        resize_keyboard=True,
        one_time_keyboard=False
    )
    
    await message.answer(
        "Добро пожаловать в главное меню бота Анти-Скам!\nИспользуйте кнопки меню внизу экрана для взаимодействия.",
        reply_markup=main_menu_keyboard
    )


@router.message(F.text == "🔍 Проверить пользователя", F.chat.type == "private")
async def check_user_button_handler(message: Message, state: FSMContext):
    await message.answer("Введите @username пользователя или его цифровой Telegram ID, которого вы хотите проверить:")
    await state.set_state(ReportStates.waiting_for_check_username)


@router.message(ReportStates.waiting_for_check_username, F.chat.type == "private")
async def process_check_username_step(message: Message, state: FSMContext, bot: Bot):
    username = message.text.strip()
    await state.clear()
    
    response_text = await check_user_and_format_response(bot, username)
    await message.answer(response_text)


@router.message(F.chat.type == "private", F.text.lower.contains("скам"))
async def process_scam_check_private(message: Message, bot: Bot):
    text_parts = message.text.split()
    target_user = None
    for part in text_parts:
        if (part.startswith("@") and len(part) > 1) or part.isdigit():
            target_user = part
            break
            
    if not target_user:
        await message.answer("Для проверки пользователя в личке нажмите кнопку «🔍 Проверить пользователя»")
        return

    response_text = await check_user_and_format_response(bot, target_user)
    await message.answer(response_text)


# --- СЦЕНАРИЙ: ПОДАЧА ЖАЛОБЫ (БЕЗОПАСНАЯ К РЕГИСТРУ) ---
@router.message(F.text == "🚨 Сообщить о пользователе", F.chat.type == "private")
async def report_username_step(message: Message, state: FSMContext):
    await message.answer("Введите @username пользователя (или его ID), на которого хотите отправить жалобу:")
    await state.set_state(ReportStates.waiting_for_username)


@router.message(ReportStates.waiting_for_username, F.chat.type == "private")
async def save_reported_username(message: Message, state: FSMContext, bot: Bot):
    raw_input = message.text.strip()
    
    # Пытаемся сразу найти ID и привести юзернейм к lowercase стандарту
    user_id, cleaned_name = await resolve_user_data(bot, raw_input)
    
    await state.update_data(target_user_str=cleaned_name, target_user_id=user_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Реакцией", callback_data="report_by_rating"),
         InlineKeyboardButton(text="Пруфом", callback_data="report_by_proof")]
    ])
    await message.answer(f"Объект модерации определен как: {cleaned_name}\nКак вы хотите сообщить о нем?", reply_markup=kb)


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
    await callback.message.edit_text("Опишите причину, почему вы решили выставить этот рейтинг?")
    await state.set_state(ReportStates.waiting_for_reason)
    await callback.answer()


@router.message(ReportStates.waiting_for_reason, F.chat.type == "private")
async def process_rating_reason(message: Message, state: FSMContext, bot: Bot):
    if not is_valid_text(message.text):
        await message.answer("Причина некорректна. Напишите нормально:")
        return
        
    data = await state.get_data()
    await state.clear()
    
    # Передаем в модерацию и текстовое имя (lowercase), и цифровой ID
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
    await notify_admin_new_request(bot, message.from_user.full_name)


@router.callback_query(F.data == "report_by_proof")
async def ask_for_proof_data(callback: CallbackQuery, state: FSMContext):
    await state.update_data(req_type="proof")
    await callback.message.edit_text("Пришлите доказательства скама (текст, фото или видео):")
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
        chat_id=message.chat.id, 
        target_user=data["target_user_str"],
        target_user_id=data.get("target_user_id"),
        reporter_id=message.from_user.id, 
        reporter_name=message.from_user.full_name,
        req_type="proof", 
        reason=reason_text, 
        media_file_id=media_id
    )
    
    await message.answer("Доказательства скама успешно отправлены админу.")
    await notify_admin_new_request(bot, message.from_user.full_name)


async def notify_admin_new_request(bot: Bot, name: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 Да", callback_data="admin_view_now"),
         InlineKeyboardButton(text="❌ Нет, потом", callback_data="admin_view_later")]
    ])
    try:
        await bot.send_message(ADMIN_ID, f"Админ, тут есть новые запросы в скам базу. Смотрим?", reply_markup=kb)
    except Exception as e:
        print(f"Не удалось уведомить админа: {e}")


# --- ПАНЕЛЬ МОДЕРАТОРА (СОХРАНЕНИЕ СТРОГО ПО USER_ID) ---
@router.message(F.text == "⚙️ Панель Модератора", F.chat.type == "private")
async def admin_menu_text_btn(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Посмотреть активные заявки 📊", callback_data="admin_view_now")]
    ])
    await message.answer("Вы вошли в панель управления модерацией заявок:", reply_markup=kb)


@router.callback_query(F.data == "admin_view_later")
async def view_later(callback: CallbackQuery):
    await callback.message.edit_text("Заявки доступны по кнопке в меню панели.")
    await callback.answer()


@router.callback_query(F.data == "admin_view_now")
async def show_moderation_queue(callback: CallbackQuery):
    requests = await get_pending_requests()
    if not requests:
        if callback.message.text:
            await callback.message.edit_text("Все заявки разобраны!")
        else:
            await callback.message.answer("Все заявки разобраны!")
        await callback.answer()
        return
    
    req = requests[0]
    rating_types = {
        "rating_clown": "🤡 Клоун", 
        "rating_suspect": "🤔 Подозрение", 
        "rating_good": "❤️ Лучший", 
        "proof": "📁 Пруф"
    }
    type_display = rating_types.get(req['req_type'], req['req_type'])
    
    text = (
        f"Заявка для: {req['target_username']}\n"
        f"Реальный ID: `{req.get('target_user_id') or 'Не определен'}`\n"
        f"Реакция: {type_display}\n"
        f"Причина: {req['reason']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять", callback_data=f"approve_{req['id']}"),
         InlineKeyboardButton(text="Отказать", callback_data=f"reject_{req['id']}")]
    ])
    
    if req['media_file_id']:
        await callback.message.delete()
        await callback.message.answer_photo(photo=req['media_file_id'], caption=text, reply_markup=kb)
    else:
        if callback.message.text:
            await callback.message.edit_text(text, reply_markup=kb)
        else:
            await callback.message.answer(text, reply_markup=kb)
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
        
        # Передаем обновленные параметры в БД (привязка идет по ID, а текст сохраняется параллельно в нижнем регистре)
        await add_or_update_scammer_by_id(
            user_id=current_req.get('target_user_id'),
            username=current_req['target_username'].lower(), # Пишем всегда маленькими буквами
            req_type=current_req['req_type'],
            proof_text=current_req['reason'] if is_proof else None,
            has_proof=is_proof
        )
        msg_text = "Заявка успешно подтверждена и внесена по ID пользователя."
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
    await callback.answer()
