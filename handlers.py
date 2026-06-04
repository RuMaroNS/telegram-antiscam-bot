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

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# Твой жестко прописанный ADMIN_ID
ADMIN_ID = 6176762600

# Состояния FSM
class ReportStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()

class CheckStates(StatesGroup):
    waiting_for_input = State()


# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

async def resolve_user_data(bot: Bot, raw_input: str):
    """Преобразует ввод пользователя в Telegram ID и юзернейм через get_chat"""
    raw_input = raw_input.strip()
    if raw_input.isdigit():
        return int(raw_input), None
    
    cleaned = raw_input.replace("@", "").strip()
    try:
        chat = await bot.get_chat(f"@{cleaned}")
        return chat.id, chat.username or cleaned
    except Exception as e:
        logger.error(f"Ошибка резолва пользователя через API Telegram ({raw_input}): {e}")
        return None, cleaned


def is_valid_text(text: str) -> bool:
    """Проверяет длину введенного текста"""
    return bool(text and len(text.strip()) >= 2)


async def notify_admin_new_request(bot: Bot, reporter_name: str):
    """Отправляет уведомление админу о новых заявках в очереди"""
    if ADMIN_ID == 0:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 Да, давай", callback_data="admin_view_now")],
        [InlineKeyboardButton(text="❌ Нет, позже", callback_data="admin_close_notify")]
    ])
    try:
        await bot.send_message(
            chat_id=ADMIN_ID, 
            text=f"👑 *Админ, внимание!* Поступили новые жалобы на модерацию. Проверим?", 
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Критическая ошибка отправки уведомления админу {ADMIN_ID}: {e}")


# =====================================================================
# СТАРТ И ОБРАБОТКА ГЛАВНОГО МЕНЮ
# =====================================================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start. Сбрасывает стейты и строит reply-клавиатуру"""
    await state.clear()
    
    main_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить пользователя")],
            [KeyboardButton(text="🚨 Сообщить о пользователе")],
            [KeyboardButton(text="⚙️ Панель Модератора")]
        ],
        resize_keyboard=True
    )
    
    welcome_text = (
        "👋 *Добро пожаловать в единую базу Анти-Скам!*\n\n"
        "• Нажмите *🔍 Проверить пользователя*, чтобы узнать рейтинг человека.\n"
        "• Нажмите *🚨 Сообщить о пользователе*, чтобы подать жалобу на мошенничество.\n"
        "• Кнопка *⚙️ Панель Модератора* доступна только администраторам системы."
    )
    await message.answer(welcome_text, reply_markup=main_kb, parse_mode="Markdown")


# Перехват кликов по меню во время любых активных стейтов (анти-баг)
@router.message(F.text.in_({"🔍 Проверить пользователя", "🚨 Сообщить о пользователе", "⚙️ Панель Модератора"}))
async def cancel_state_on_menu_click(message: Message, state: FSMContext, bot: Bot):
    """Если юзер находится внутри сценария FSM, но нажал кнопку меню — сбрасываем сценарий"""
    await state.clear()
    if message.text == "🔍 Проверить пользователя":
        await ask_user_to_check(message, state)
    elif message.text == "🚨 Сообщить о пользователе":
        await start_report(message, state)
    elif message.text == "⚙️ Панель Модератора":
        await admin_view_requests(message)


# =====================================================================
# БЛОК: ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ
# =====================================================================

@router.message(F.text == "🔍 Проверить пользователя")
async def ask_user_to_check(message: Message, state: FSMContext):
    """Инициализация сценария проверки"""
    await state.set_state(CheckStates.waiting_for_input)
    await message.answer("Введите @username пользователя или его цифровой Telegram ID, которого вы хотите проверить:")


@router.message(CheckStates.waiting_for_input, F.chat.type == "private")
async def check_user_in_db(message: Message, state: FSMContext, bot: Bot):
    """Получение данных и запрос к Supabase для проверки скамера"""
    raw_input = message.text.strip()
    await state.clear()
    
    user_id, username = await resolve_user_data(bot, raw_input)
    
    # Запрос в БД
    scammer = await get_user_by_id_or_username(user_id=user_id, username=username or raw_input)
    
    if scammer:
        text = (
            f"🚨 *[ВНИМАНИЕ! ПОЛЬЗОВАТЕЛЬ НАЙДЕН В БАЗЕ]* 🚨\n\n"
            f"👤 *Юзернейм:* @{scammer.get('current_username', 'Не указан')}\n"
            f"🆔 *Telegram ID:* `{scammer.get('user_id', 'Скрыт')}`\n\n"
            f"📊 *Текущий рейтинг жалоб:*\n"
            f"🤡 Клоун: {scammer.get('clown_count', 0)}\n"
            f"🤔 Подозреваемый: {scammer.get('suspect_count', 0)}\n"
            f"❤️ Гуд (Доверие): {scammer.get('good_count', 0)}\n"
        )
        if scammer.get('has_proof'):
            text += f"\n📄 *Подтвержденный пруф от админа:*\n_{scammer.get('proof_text', 'Без описания')}_"
        await message.answer(text, parse_mode="Markdown")
    else:
        display_name = f"@{username}" if username else raw_input
        await message.answer(f"*[ОТСУТСТВУЕТ В БАЗЕ]*\n\nПользователь {display_name} не найден в базе данных Анти-Скам.", parse_mode="Markdown")


# =====================================================================
# БЛОК: СОЗДАНИЕ ЖАЛОБЫ (ПОДАЧА ЗАЯВКИ НА МОДЕРАЦИЮ)
# =====================================================================

@router.message(F.text == "🚨 Сообщить о пользователе")
async def start_report(message: Message, state: FSMContext):
    """Инициализация сценария подачи жалобы"""
    await state.set_state(ReportStates.waiting_for_username)
    await message.answer("Введите @username пользователя (или его ID), на которого хотите отправить жалобу:")


@router.message(ReportStates.waiting_for_username, F.chat.type == "private")
async def save_reported_username(message: Message, state: FSMContext, bot: Bot):
    """Сохранение имени цели и вывод инлайн-клавиатуры типов жалоб"""
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
    """Выбор типа и переход к описанию причины"""
    req_type = callback.data.split(":")[1]
    await state.update_data(req_type=req_type)
    
    await callback.message.delete()
    await state.set_state(ReportStates.waiting_for_reason)
    await callback.message.answer("Опишите подробно причину, почему вы решили выставить этот рейтинг / оставить жалобу:")
    await callback.answer()


@router.message(ReportStates.waiting_for_reason, F.chat.type == "private")
async def process_rating_reason(message: Message, state: FSMContext, bot: Bot):
    """Финальный этап создания заявки и отправка ее в Supabase"""
    if not is_valid_text(message.text):
        await message.answer("Описание причины слишком короткое. Распишите подробнее, пожалуйста:")
        return
        
    data = await state.get_data()
    await state.clear()
    
    try:
        await create_moderation_request(
            chat_id=message.chat.id, 
            target_user=data["target_user_str"],
            target_user_id=data.get("target_user_id"), 
            reporter_id=message.from_user.id, 
            reporter_name=message.from_user.full_name,
            req_type=data["req_type"], 
            reason=message.text
        )
        await message.answer("✅ Ваша заявка успешно создана и отправлена команде модерации. Спасибо за бдительность!")
        # Сигнализируем админу
        await notify_admin_new_request(bot, message.from_user.full_name)
    except Exception as db_err:
        logger.error(f"Ошибка сохранения заявки в Supabase: {db_err}")
        await message.answer("❌ Произошла внутренняя ошибка при сохранении заявки в базу данных.")


# =====================================================================
# БЛОК: АДМИН-ПАНЕЛЬ И ОБРАБОТКА ОДОБРЕНИЯ / ОТКЛОНЕНИЯ
# =====================================================================

@router.message(F.text == "⚙️ Панель Модератора")
@router.callback_query(F.data == "admin_view_now")
async def admin_view_requests(event):
    """Отображение очереди заявок на модерацию (Доступ только для ADMIN_ID)"""
    user_id = event.from_user.id
    if user_id != ADMIN_ID:
        if isinstance(event, CallbackQuery):
            await event.answer("У вас нет прав администратора.", show_alert=True)
        else:
            await event.answer("⚠️ Доступ запрещен. Вы не являетесь администратором бота.")
        return

    is_callback = isinstance(event, CallbackQuery)
    message = event.message if is_callback else event

    # Берем заявки 'pending'
    requests = await get_pending_requests()
    if not requests:
        if is_callback:
            await event.message.edit_text("✨ Все заявки успешно разобраны! Очередь пуста.")
            await event.answer()
        else:
            await message.answer("✨ Все заявки успешно разобраны! Очередь пуста.")
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
        f"📋 *Новая заявка на модерацию #{req_id}*\n\n"
        f"👤 *На кого жалоба:* @{req['target_username']} (ID: {req.get('target_user_id') or 'Не определен'})\n"
        f"🏷️ *Тип действия:* {human_type}\n"
        f"📝 *Отправитель:* {req['reporter_name']} (ID: {req['reporter_id']})\n"
        f"💬 *Описание/Пруфы:* {req['reason']}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{req_id}"),
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
    """Обработчик решений админа (Принять/Отклонить заявку)"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Куда лезешь? Ты не админ!", show_alert=True)
        return

    action, req_id = callback.data.split("_")
    req_id = int(req_id)
    
    # Берем данные текущей заявки, пока она в статусе pending
    requests = await get_pending_requests()
    current_req = next((r for r in requests if r['id'] == req_id), None)
    
    if action == "approve":
        if current_req:
            is_proof = current_req['req_type'] == "proof"
            
            try:
                # 1. Запись скамера в БД
                await add_or_update_scammer_by_id(
                    user_id=current_req.get('target_user_id'), 
                    username=current_req['target_username'],
                    req_type=current_req['req_type'], 
                    proof_text=current_req['reason'] if is_proof else None,
                    has_proof=is_proof
                )
                
                # 2. Перевод статуса самой заявки в approved
                await update_request_status(req_id, "approved")
                msg_text = f"✅ Заявка #{req_id} одобрена. Данные внесены в базу Анти-Скам."
                
            except Exception as write_err:
                logger.error(f"Ошибка при записи скамера в базу: {write_err}")
                # Если упало — выводим ошибку на экран, чтобы понять причину сбоя Supabase
                msg_text = f"❌ Ошибка Supabase при сохранении скамера: {str(write_err)}"
        else:
            msg_text = "❌ Ошибка: не удалось выгрузить данные заявки из кэша до её утверждения."
    else:
        # При отклонении просто меняем статус в истории заявок
        try:
            await update_request_status(req_id, "rejected")
            msg_text = f"❌ Заявка #{req_id} успешно отклонена модератором."
        except Exception as status_err:
            msg_text = f"❌ Ошибка изменения статуса заявки: {str(status_err)}"
        
    await callback.message.delete()
    await callback.message.answer(msg_text)
    
    # Проверяем, есть ли еще заявки
    next_requests = await get_pending_requests()
    if next_requests:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Показать следующую заявку ➡️", callback_data="admin_view_now")]
        ])
        await callback.message.answer("📥 В очереди модерации еще остались необработанные заявки.", reply_markup=kb)
    else:
        await callback.message.answer("✨ Прекрасно! Все заявки из очереди модерации были успешно разобраны.")
        
    await callback.answer()


@router.callback_query(F.data == "admin_close_notify")
async def close_admin_notification(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.delete()
    await callback.answer("Уведомление закрыто.")
