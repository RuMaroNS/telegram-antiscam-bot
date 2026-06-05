import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command

from database.supabase_db import (
    get_user_by_id_or_username,
    create_moderation_request,
    get_pending_requests,
    update_request_status,
    add_or_update_scammer_by_id,
    get_cached_user_by_username
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

ADMIN_ID = 6176762600  # Твой ID админа

class ReportStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()

class CheckStates(StatesGroup):
    waiting_for_input = State()

# =====================================================================
# КОМБО-РЕЗОЛВЕР АЙДИ (УМНЫЙ ГЛУБОКИЙ ПОИСК)
# =====================================================================
async def combo_find_user_id(bot: Bot, raw_input: str) -> tuple:
    """
    Комбо-поиск ID по всем фронтам без использования сторонних юзерботов.
    """
    raw_input = raw_input.strip()
    
    # Способ 1: Если сразу ввели цифры (ID)
    if raw_input.isdigit():
        return int(raw_input), f"id_{raw_input}"
        
    cleaned_username = raw_input.replace("@", "").strip().lower()
    
    # Способ 2: Ищем в нашей кэш-базе "Лицо в лицо" (таблица users)
    cached_user = await get_cached_user_by_username(cleaned_username)
    if cached_user and cached_user.get("user_id"):
        found_id = cached_user["user_id"]
        logger.info(f"🎯 ID для @{cleaned_username} взят из кэша users: {found_id}")
        return found_id, cleaned_username
        
    # Способ 3: Пробуем системный вызов (для каналов/групп/тех кто открыт)
    try:
        chat = await bot.get_chat(f"@{cleaned_username}")
        return chat.id, chat.username.lower() if chat.username else cleaned_username
    except Exception:
        pass

    # Не нашли — отдаем 0 и юзернейм текстом
    return 0, cleaned_username

# =====================================================================
# СТАРТ И КОМАНДЫ
# =====================================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Добро пожаловать в систему *AntiScamBase | ASB*!\n\n"
        "используйте кнопки ниже для проверки или подачи жалоб.",
        parse_mode="Markdown"
    )

# =====================================================================
# ХЭНДЛЕРЫ: ПОДАЧА ЖАЛОБЫ
# =====================================================================
@router.message(F.text == "🚨 Сообщить о пользователе")
async def start_report(message: Message, state: FSMContext):
    await state.set_state(ReportStates.waiting_for_username)
    await message.answer(
        "🚨 *Подача жалобы (КОМБО-РЕЖИМ)*\n\n"
        "Отправьте цель одним из способов:\n"
        "1️⃣ **Перешлите (Forward)** любое сообщение скамера из ЛС.\n"
        "2️⃣ Напишите его **@username** текстом.\n"
        "3️⃣ Напишите его **цифровой Telegram ID**.",
        parse_mode="Markdown"
    )

@router.message(ReportStates.waiting_for_username, F.chat.type == "private")
async def save_reported_username(message: Message, state: FSMContext, bot: Bot):
    user_id = 0
    db_username = None

    # Вариант А: Пересланное сообщение (Железный ID вытаскивается встроенными методами ТГ)
    if message.forward_from:
        user_id = message.forward_from.id
        db_username = message.forward_from.username or f"id_{user_id}"
    elif message.forward_from_chat:
        user_id = message.forward_from_chat.id
        db_username = message.forward_from_chat.username or message.forward_from_chat.title
    # Вариант Б: Текстовый ввод (Включаем комбо-резолвер)
    else:
        user_id, db_username = await combo_find_user_id(bot, message.text)

    db_username = db_username.replace("@", "").strip().lower()
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
        f"🎯 *Цель распознана:*\n"
        f"👤 Юзернейм: @{db_username}\n"
        f"🆔 Telegram ID: {display_id}\n\n"
        f"Выберите категорию жалобы:", 
        reply_markup=kb, 
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("set_type:"))
async def set_report_type(callback: CallbackQuery, state: FSMContext):
    req_type = callback.data.split(":")[1]
    await state.update_data(req_type=req_type)
    await callback.message.delete()
    await state.set_state(ReportStates.waiting_for_reason)
    await callback.message.answer("Опишите подробно причину или прикрепите ссылку на доказательства:")
    await callback.answer()

@router.message(ReportStates.waiting_for_reason, F.chat.type == "private")
async def process_rating_reason(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    
    t_id = data.get("target_user_id", 0)
    
    try:
        await create_moderation_request(
            chat_id=message.chat.id, 
            target_user=data["target_user_str"],
            target_user_id=t_id, 
            reporter_id=message.from_user.id, 
            reporter_name=message.from_user.full_name or "Аноним",
            req_type=data["req_type"], 
            reason=message.text
        )
        await message.answer("✅ Заявка отправлена команде модерации. Спасибо за бдительность!")
        
        if ADMIN_ID:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡ Открыть очередь", callback_data="admin_view_now")]])
            await bot.send_message(ADMIN_ID, f"👑 *Админ*, новая жалоба на @{data['target_user_str']}!", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")

# =====================================================================
# ХЭНДЛЕРЫ: ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ
# =====================================================================
@router.message(F.text == "🔍 Проверить пользователя")
async def ask_user_to_check(message: Message, state: FSMContext):
    await state.set_state(CheckStates.waiting_for_input)
    await message.answer("Введите @username или цифровой ID для мгновенной проверки:")

@router.message(CheckStates.waiting_for_input, F.chat.type == "private")
async def check_user_in_db(message: Message, state: FSMContext, bot: Bot):
    raw_input = message.text.strip()
    await state.clear()
    
    user_id, username = await combo_find_user_id(bot, raw_input)
    
    search_id = user_id if user_id else (int(raw_input) if raw_input.isdigit() else 0)
    search_username = username if username else raw_input.replace("@", "").strip().lower()

    scammer = await get_user_by_id_or_username(user_id=search_id, username=search_username)
    
    if scammer:
        db_username = scammer.get('current_username', search_username)
        db_id = scammer.get('user_id', 0)
        id_display = f"`{db_id}`" if db_id != 0 else "Скрыт/Не найден"
        
        text = (
            f"🚨 *[ВНИМАНИЕ! ПОЛЬЗОВАТЕЛЬ НАЙДЕН В БАЗЕ]* 🚨\n\n"
            f"👤 Юзернейм: @{db_username}\n"
            f"🆔 Telegram ID: {id_display}\n\n"
            f"📊 Текущий рейтинг жалоб:\n"
            f"🤡 Клоун: {scammer.get('clown_count', 0)}\n"
            f"🤔 Подозреваемый: {scammer.get('suspect_count', 0)}\n"
            f"❤️ Гуд (Доверие): {scammer.get('good_count', 0)}\n"
        )
        if scammer.get('has_proof'):
            text += f"\n📄 *Доказательства:* {scammer.get('proof_text')}"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer(f"✅ Пользователь @{search_username} не найден в базе данных скамеров.", parse_mode="Markdown")

# =====================================================================
# ПАНЕЛЬ МОДЕРАТОРА (АДМИНКА)
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
        text = "✨ Прекрасно! Все заявки из очереди модерации были успешно разобраны."
        if is_callback:
            await event.message.edit_text(text)
        else:
            await message.answer(text)
        return

    req = requests[0]
    req_id = req['id']
    t_id = req.get('target_user_id', 0)
    id_text = f"`{t_id}`" if t_id != 0 else "0"

    text = (
        f"📋 *Заявка #{req_id}*\n\n"
        f"👤 *Цель:* @{req['target_username']} (ID: {id_text})\n"
        f"📝 *Тип рейтинга:* {req['req_type']}\n"
        f"📝 *Отправитель:* {req['reporter_name']}\n"
        f"💬 *Обоснование:* {req['reason']}"
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
        await callback.message.answer(f"✅ Заявка #{req_id} одобрена. Данные внесены по ID {scam_id} в базу.")
    else:
        await update_request_status(req_id, "rejected")
        await callback.message.answer(f"❌ Заявка #{req_id} отклонена.")
        
    await callback.message.delete()
    await admin_view_requests(callback)
    await callback.answer()
