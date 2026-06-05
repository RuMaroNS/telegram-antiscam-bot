import os
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command

# Импортируем функции работы с базой данных
from database.supabase_db import (
    get_user_by_id_or_username,
    create_moderation_request,
    get_pending_requests,
    update_request_status,
    add_or_update_scammer_by_id
)
# Предполагаем, что у тебя есть коннект к supabase в supabase_db
from database.supabase_db import supabase 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

ADMIN_ID = 6176762600

class ReportStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()

class CheckStates(StatesGroup):
    waiting_for_input = State()


# =====================================================================
# АВТО-ОТЛЕЖИВАНИЕ ПОЛЬЗОВАТЕЛЕЙ (ЗАПОМИНАЕМ В ЛИЦО)
# =====================================================================

@router.message()
async def track_every_user(message: Message):
    """
    Хэндлер-шпион. Ловит абсолютно любые сообщения в личке и чатах,
    чтобы сохранить связь Username -> ID во внутренней базе данных.
    """
    if message.from_user:
        user_id = message.from_user.id
        username = message.from_user.username.replace("@", "").strip() if message.from_user.username else None
        full_name = message.from_user.full_name
        
        # Сохраняем/обновляем юзера в таблице users (UPSERT)
        if username:
            try:
                supabase.table("users").upsert({
                    "user_id": user_id,
                    "username": username,
                    "full_name": full_name
                }, on_conflict="user_id").execute()
            except Exception as e:
                logger.error(f"Не удалось закешировать пользователя {user_id}: {e}")


# =====================================================================
# КОМБО-РЕЗОЛВЕР АЙДИ (УМНЫЙ ПОИСК ПО ВСЕМ ФРОНТАМ)
# =====================================================================

async def combo_find_user_id(bot: Bot, raw_input: str) -> tuple:
    """
    Ищет ID и юзернейм по всем доступным методам:
    1. Проверка на чистый ID
    2. Поиск во внутренней базе 'users' (Лицо в лицо)
    3. Поиск по глобальным кэшам
    """
    raw_input = raw_input.strip()
    
    # Метод 1: Введен чистый ID
    if raw_input.isdigit():
        return int(raw_input), None
        
    cleaned_username = raw_input.replace("@", "").strip()
    
    # Метод 2: Ищем во внутренней базе данных бота (таблица users)
    try:
        res = supabase.table("users").select("user_id").eq("username", cleaned_username).execute()
        if res.data:
            found_id = res.data[0]["user_id"]
            logger.info(f"🎯 ID для @{cleaned_username} найден во внутренней БД users: {found_id}")
            return found_id, cleaned_username
    except Exception as e:
        logger.error(f"Ошибка при поиске в локальной таблице users: {e}")
        
    # Метод 3: Пробуем системный фолбэк (на случай каналов/супергрупп)
    try:
        chat = await bot.get_chat(f"@{cleaned_username}")
        return chat.id, chat.username or cleaned_username
    except Exception:
        pass

    # Если ни один метод не сработал — возвращаем 0, но сохраняем текст юзернейма
    return 0, cleaned_username


# =====================================================================
# ХЭНДЛЕРЫ: СООБЩИТЬ О ПОЛЬЗОВАТЕЛЕ (ОТПРАВКА ЖАЛОБЫ)
# =====================================================================

@router.message(F.text == "🚨 Сообщить о пользователе")
async def start_report(message: Message, state: FSMContext):
    await state.set_state(ReportStates.waiting_for_username)
    await message.answer(
        "🚨 *Подача жалобы (КОМБО-РЕЖИМ)*\n\n"
        "Вы можете действовать двумя способами:\n"
        "1️⃣ **Перешлите (Forward)** сюда любое сообщение от скамера.\n"
        "2️⃣ Или просто **напишите его @username** (или ID) текстом.\n\n"
        "_Бот автоматически применит все методы поиска его ID!_",
        parse_mode="Markdown"
    )

@router.message(ReportStates.waiting_for_username, F.chat.type == "private")
async def save_reported_username(message: Message, state: FSMContext, bot: Bot):
    user_id = 0
    db_username = None

    # МЕТОД А: Юзер переслал сообщение скамера (Железный ID)
    if message.forward_from:
        user_id = message.forward_from.id
        db_username = message.forward_from.username or f"id_{user_id}"
    elif message.forward_from_chat:
        user_id = message.forward_from_chat.id
        db_username = message.forward_from_chat.username or message.forward_from_chat.title
        
    # МЕТОД Б: Юзер просто ввел текст (Включаем Комбо-Резолвер)
    else:
        raw_input = message.text.strip()
        user_id, db_username = await combo_find_user_id(bot, raw_input)

    db_username = db_username.replace("@", "").strip()
    display_id = f"`{user_id}`" if user_id != 0 else "Не найден в кэше (будет проверен модератором)"

    await state.update_data(target_user_str=db_username, target_user_id=user_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤡 Клоун", callback_data="set_type:rating_clown"),
            InlineKeyboardButton(text="🤔 Подозреваемый", callback_data="set_type:rating_suspect"),
            InlineKeyboardButton(text="❤️ Гуд", callback_data="set_type:rating_good")
        ],
        [InlineKeyboardButton(text="📄 Железный Пруф", callback_data="set_type:proof")]
    ])
    
    await message.answer(
        f"🎯 *Объект распознан!*\n"
        f"👤 Юзернейм: @{db_username}\n"
        f"🆔 Telegram ID: {display_id}\n\n"
        f"Выберите тип рейтинга для отправки на модерацию:", 
        reply_markup=kb, 
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("set_type:"))
async def set_report_type(callback: CallbackQuery, state: FSMContext):
    req_type = callback.data.split(":")[1]
    await state.update_data(req_type=req_type)
    await callback.message.delete()
    await state.set_state(ReportStates.waiting_for_reason)
    await callback.message.answer("Опишите подробно причину жалобы / выставления рейтинга:")
    await callback.answer()


@router.message(ReportStates.waiting_for_reason, F.chat.type == "private")
async def process_rating_reason(message: Message, state: FSMContext, bot: Bot):
    if len(message.text.strip()) < 2:
        await message.answer("Пожалуйста, распишите причину подробнее:")
        return
        
    data = await state.get_data()
    await state.clear()
    
    t_id = data.get("target_user_id", 0)
    
    try:
        await create_moderation_request(
            chat_id=message.chat.id, 
            target_user=data["target_user_str"],
            target_user_id=t_id, 
            reporter_id=message.from_user.id, 
            reporter_name=message.from_user.full_name or "Пользователь",
            req_type=data["req_type"], 
            reason=message.text
        )
        await message.answer("✅ Заявка отправлена модераторам. Спасибо!")
        
        # Сигнал админу
        if ADMIN_ID != 0:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💯 Проверить очередь", callback_data="admin_view_now")]])
            await bot.send_message(ADMIN_ID, f"👑 *Админ*, прилетела новая жалоба на @{data['username']}!", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка сохранения заявки: {e}")
        await message.answer(f"❌ Ошибка бэкенда: {e}")


# =====================================================================
# ХЭНДЛЕРЫ: ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ (УМНЫЙ ПОИСК С КОНВЕРТАЦИЕЙ)
# =====================================================================

@router.message(F.text == "🔍 Проверить пользователя")
async def ask_user_to_check(message: Message, state: FSMContext):
    await state.set_state(CheckStates.waiting_for_input)
    await message.answer("Введите @username или цифровой ID для проверки в базе:")


@router.message(CheckStates.waiting_for_input, F.chat.type == "private")
async def check_user_in_db(message: Message, state: FSMContext, bot: Bot):
    raw_input = message.text.strip()
    await state.clear()
    
    # Запускаем комбо-поиск ID перед тем, как лезть в таблицу скамеров
    user_id, username = await combo_find_user_id(bot, raw_input)
    
    search_id = user_id if user_id else (int(raw_input) if raw_input.isdigit() else 0)
    search_username = username if username else raw_input.replace("@", "").strip()

    scammer = await get_user_by_id_or_username(user_id=search_id, username=search_username)
    
    if scammer:
        db_username = scammer.get('current_username', search_username)
        db_id = scammer.get('user_id')
        id_display = f"`{db_id}`" if db_id and db_id != 0 else "Не найден в кэше"
        
        text = (
            f"🚨 *[ПОЛЬЗОВАТЕЛЬ НАЙДЕН В БАЗЕ СКАМЕРОВ]* 🚨\n\n"
            f"👤 *Юзернейм:* @{db_username}\n"
            f"🆔 *Telegram ID:* {id_display}\n\n"
            f"📊 *Рейтинг:*\n"
            f"🤡 Клоун: {scammer.get('clown_count', 0)}\n"
            f"🤔 Подозреваемый: {scammer.get('suspect_count', 0)}\n"
            f"❤️ Гуд: {scammer.get('good_count', 0)}\n"
        )
        if scammer.get('has_proof'):
            text += f"\n📄 *Пруф:* _{scammer.get('proof_text')}_"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer(f"✅ Пользователь @{search_username} не найден в базе данных скамеров.", parse_mode="Markdown")

# =====================================================================
# АДМИН-ПАНЕЛЬ (КРАТКАЯ ВЕРСИЯ)
# =====================================================================

@router.message(F.text == "⚙️ Панель Модератора")
@router.callback_query(F.data == "admin_view_now")
async def admin_view_requests(event):
    user_id = event.from_user.id
    if user_id != ADMIN_ID:
        return

    is_callback = isinstance(event, CallbackQuery)
    message = event.message if is_callback else event

    requests = await get_pending_requests()
    if not requests:
        if is_callback:
            await event.message.edit_text("✨ Очередь пуста.")
        else:
            await message.answer("✨ Очередь пуста.")
        return

    req = requests[0]
    req_id = req['id']
    
    t_id = req.get('target_user_id', 0)
    id_text = f"`{t_id}`" if t_id != 0 else "Не определен"

    text = (
        f"📋 *Заявка #{req_id}*\n\n"
        f"👤 *Цель:* @{req['target_username']} (ID: {id_text})\n"
        f"📝 *Отправитель:* {req['reporter_name']}\n"
        f"💬 *Причина:* {req['reason']}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{req_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{req_id}")
    ]])
    
    if is_callback:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def handle_decision(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    action, req_id = callback.data.split("_")
    req_id = int(req_id)
    
    requests = await get_pending_requests()
    current_req = next((r for r in requests if r['id'] == req_id), None)
    
    if action == "approve" and current_req:
        is_proof = current_req['req_type'] == "proof"
        scam_id = current_req.get('target_user_id', 0)
        
        await add_or_update_scammer_by_id(
            user_id=scam_id, 
            username=current_req['target_username'],
            req_type=current_req['req_type'], 
            proof_text=current_req['reason'] if is_proof else None,
            has_proof=is_proof
        )
        await update_request_status(req_id, "approved")
        await callback.message.answer(f"✅ Одобрено по ID `{scam_id}`")
    else:
        await update_request_status(req_id, "rejected")
        await callback.message.answer("❌ Отклонено")
        
    await callback.message.delete()
    await admin_view_requests(callback)
    await callback.answer()
