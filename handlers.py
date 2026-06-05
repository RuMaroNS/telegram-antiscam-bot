import logging
import asyncio
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command, CommandObject

# Импорт клиента Supabase из твоей архитектуры БД
from database.supabase_db import (
    get_user_by_id_or_username,
    create_moderation_request,
    get_pending_requests,
    update_request_status,
    add_or_update_scammer_by_id,
    get_cached_user_by_username,
    supabase
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

router = Router()

# =====================================================================
# КОНФИГУРАЦИЯ И ЛОКАЛИЗАЦИЯ
# =====================================================================
PRIMARY_ADMIN_ID = 6176762600  # Твой неизменяемый ID владельца

TEXTS = {
    "welcome": "👋 <b>Добро пожаловать в AntiScamBase | ASB!</b>\n\nИспользуйте меню кнопок внизу для проверки контрагентов или подачи жалоб.",
    "report_start": "🚨 <b>Подача заявления</b>\n\nОтправьте цель одним из способов:\n1️⃣ Перешлите сообщение скамера.\n2️⃣ Введите @username.\n3️⃣ Введите цифровой Telegram ID.",
    "cancel_ok": "❌ Действие отменено. Возврат в главное меню.",
    "cooldown": "⏳ <b>Защита от флуда!</b> Пожалуйста, подождите немного перед следующим запросом.",
    "self_report": "⚠️ Вы не можете подать жалобу или поставить реакцию самому себе."
}

# =====================================================================
# КЛАВИАТУРЫ (ГЛАВНОЕ МЕНЮ И КНОПКИ ОТМЕНЫ)
# =====================================================================
def get_main_menu_kb():
    """Создает постоянные кнопки внизу экрана в ЛС бота"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить пользователя")],
            [KeyboardButton(text="🚨 Сообщить о пользователе")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def get_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="fsm_cancel")]])

# =====================================================================
# ДИНАМИЧЕСКИЙ МЕНЕДЖЕР АДМИНИСТРАТОРОВ (staff_members)
# =====================================================================
async def is_admin(user_id: int) -> bool:
    if user_id == PRIMARY_ADMIN_ID:
        return True
    try:
        res = supabase.table("staff_members").select("user_id").eq("user_id", user_id).execute()
        return len(res.data) > 0
    except Exception:
        return False

# =====================================================================
# ВСПУМОГАТЕЛЬНЫЕ ФУНКЦИИ И РЕЗОЛВЕР
# =====================================================================
user_cooldowns = {}

def check_cooldown(user_id: int, seconds: int = 2) -> bool:
    now = datetime.now().timestamp()
    if user_id in user_cooldowns and now - user_cooldowns[user_id] < seconds:
        return False
    user_cooldowns[user_id] = now
    return True

async def combo_resolve_target(bot: Bot, raw_input: str) -> tuple:
    raw_input = raw_input.strip()
    if raw_input.isdigit():
        return int(raw_input), f"id_{raw_input}"
    
    cleaned = raw_input.replace("@", "").strip().lower()
    cached = await get_cached_user_by_username(cleaned)
    if cached and cached.get("user_id"):
        return int(cached["user_id"]), cleaned
        
    try:
        chat = await bot.get_chat(f"@{cleaned}")
        return chat.id, (chat.username.lower() if chat.username else cleaned)
    except Exception:
        return 0, cleaned

# =====================================================================
# СОСТОЯНИЯ FSM
# =====================================================================
class BotStates(StatesGroup):
    waiting_for_target = State()
    waiting_for_reason = State()
    waiting_for_check = State()
    waiting_for_new_admin = State()

# =====================================================================
# ОБЩАЯ ЛОГИКА ПРОВЕРКИ (ДЛЯ ЛС И ДЛЯ ГРУПП)
# =====================================================================
async def execute_user_check(bot: Bot, target_input: str, trigger_user_id: int) -> tuple:
    """Универсальная функция проверки пользователя в базе данных"""
    user_id, username = await combo_resolve_target(bot, target_input)
    search_id = user_id if user_id else (int(target_input) if target_input.isdigit() else 0)
    search_username = username if username else target_input.replace("@", "").strip().lower()

    scammer = await get_user_by_id_or_username(user_id=search_id, username=search_username)
    
    try:
        query = supabase.table("moderation_requests").select("id").eq("status", "approved").eq("req_type", "proof")
        if search_id and search_id != 0:
            query = query.eq("target_user_id", search_id)
        else:
            query = query.eq("target_username", search_username.lower())
        res = query.execute()
        proofs = res.data
    except Exception:
        proofs = []
    
    if proofs:
        status_header = "🔴 <b>КРИТИЧЕСКИЙ СТАТУС: СКАМЕР / МОШЕННИК</b> 🔴"
    elif scammer and (scammer.get("clown_count", 0) > 0 or scammer.get("suspect_count", 0) > 0):
        status_header = "🟡 <b>ВНИМАНИЕ: ЕСТЬ ЖАЛОБЫ В СИСТЕМЕ РЕАКЦИЙ</b> 🟡"
    else:
        status_header = "🟢 <b>АНАЛИЗ ЗАВЕРШЕН: НАДЕЖНЫЙ ПОЛЬЗОВАТЕЛЬ</b> 🟢"

    clowns = scammer.get("clown_count", 0) if scammer else 0
    suspects = scammer.get("suspect_count", 0) if scammer else 0
    goods = scammer.get("good_count", 0) if scammer else 0
    db_id_text = f"<code>{scammer.get('user_id', search_id)}</code>" if scammer else f"<code>{search_id}</code>"

    text = (
        f"{status_header}\n\n"
        f"👤 <b>Юзернейм:</b> @{search_username}\n"
        f"🆔 <b>Telegram ID:</b> {db_id_text}\n\n"
        f"📊 <b>Активность системы реакций:</b>\n"
        f"🤡 Клоун: {clowns}\n"
        f"🤔 Подозреваемый: {suspects}\n"
        f"❤️ Доверие (Гуд): {goods}\n"
    )

    if proofs:
        bot_info = await bot.get_me()
        text += "\n📄 <b>Пруфы:</b>\n"
        for idx, p in enumerate(proofs, start=1):
            link = f"https://t.me/{bot_info.username}?start=p_{p['id']}"
            text += f"🔘 <a href='{link}'>Скам {idx}</a>  "
        text += "\n"

    # Кнопки реакций выводятся только если проверяемый — не тот, кто проверяет
    kb = None
    if trigger_user_id != search_id:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤡 Клоун", callback_data=f"vote:rating_clown:{search_username}:{search_id}"),
                InlineKeyboardButton(text="🤔 Подозреваемый", callback_data=f"vote:rating_suspect:{search_username}:{search_id}"),
                InlineKeyboardButton(text="❤️ Гуд", callback_data=f"vote:rating_good:{search_username}:{search_id}")
            ]
        ])
        
    return text, kb

# =====================================================================
# ОБРАБОТКА ДЛЯ ГРУПП (Пункт: скам @username)
# =====================================================================
@router.message(F.chat.type.in_({"group", "supergroup"}), F.text.lower().startswith("скам"))
async def group_check_handler(message: Message, bot: Bot):
    if not check_cooldown(message.from_user.id, seconds=3):
        return

    parts = message.text.split(maxsplit=1)
    target_input = ""

    # Если написали просто "скам" ответом на сообщение
    if len(parts) == 1 and message.reply_to_message:
        from_user = message.reply_to_message.from_user
        target_input = str(from_user.id)
    # Если написали "скам @username" или "скам ID"
    elif len(parts) > 1:
        target_input = parts[1].strip()
    else:
        return

    await bot.send_chat_action(message.chat.id, "typing")
    text, kb = await execute_user_check(bot, target_input, message.from_user.id)
    
    # В группах выводим информацию БЕЗ инлайн-кнопок реакций, чтобы не флудили
    await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)

# =====================================================================
# КОМАНДА START И ДИПЛИНКИ
# =====================================================================
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    
    if command.args and command.args.startswith("p_"):
        try:
            proof_id = int(command.args.split("_")[1])
            res = supabase.table("moderation_requests").select("*").eq("id", proof_id).execute()
            if not res.data:
                await message.answer("❌ Данное доказательство не найдено.")
                return
                
            req = res.data[0]
            caption = f"📄 <b>Обоснование пруфа #{proof_id}:</b>\n\n{req['reason']}"
            m_type = req.get("media_type", "text")
            f_id = req.get("file_id")
            
            if m_type == "photo" and f_id:
                await message.answer_photo(f_id, caption=caption, parse_mode="HTML")
            elif m_type == "video" and f_id:
                await message.answer_video(f_id, caption=caption, parse_mode="HTML")
            elif m_type == "document" and f_id:
                await message.answer_document(f_id, caption=caption, parse_mode="HTML")
            else:
                await message.answer(caption, parse_mode="HTML")
            return
        except Exception as e:
            await message.answer(f"❌ Ошибка загрузки медиа-пруфа: {e}")
            return

    # Железно выдаем главное меню при старте
    await message.answer(TEXTS["welcome"], parse_mode="HTML", reply_markup=get_main_menu_kb())

# =====================================================================
# СБРОС СОСТОЯНИЙ И ОТМЕНА
# =====================================================================
@router.callback_query(F.data == "fsm_cancel")
async def cancel_fsm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(TEXTS["cancel_ok"], reply_markup=get_main_menu_kb())
    await callback.answer()

# =====================================================================
# РАБОТА В ЛИСЕ (КНОПКА: ПРОГНОЗ/ПРОВЕРКА)
# =====================================================================
@router.message(F.text == "🔍 Проверить пользователя", F.chat.type == "private")
async def ask_check(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_check)
    await message.answer("Введите @username или цифровой ID для проверки в базе данных:", reply_markup=get_cancel_kb())

@router.message(BotStates.waiting_for_check, F.chat.type == "private")
async def perform_check(message: Message, state: FSMContext, bot: Bot):
    raw_input = message.text.strip()
    await state.clear()
    
    await bot.send_chat_action(message.chat.id, "typing")
    text, kb = await execute_user_check(bot, raw_input, message.from_user.id)
    
    # В личке выводим результат С кнопками реакций и возвращаем клавиатуру меню
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu_kb(), disable_web_page_preview=True)
    if kb:
        await message.answer("Вы можете выразить свое отношение к пользователю кнопками реакций ниже:", reply_markup=kb)

# =====================================================================
# ДВИЖОК ПЕРЕЗАПИСЫВАЕМЫХ РЕАКЦИЙ С ЗАЩИТОЙ ОТ НАКРУТОК
# =====================================================================
async def apply_user_reaction(reporter_id: int, target_username: str, target_id: int, new_reaction: str) -> str:
    try:
        query = supabase.table("user_reactions").select("id", "reaction_type").eq("reporter_id", reporter_id)
        if target_id and target_id != 0:
            query = query.eq("target_user_id", target_id)
        else:
            query = query.eq("target_username", target_username.lower())
        existing = query.execute()
        
        scammer = await get_user_by_id_or_username(user_id=target_id, username=target_username)
        stats = {
            "clown_count": scammer.get("clown_count", 0) if scammer else 0,
            "suspect_count": scammer.get("suspect_count", 0) if scammer else 0,
            "good_count": scammer.get("good_count", 0) if scammer else 0
        }
        
        if existing.data:
            old_rec = existing.data[0]
            old_type = old_rec["reaction_type"]
            if old_type == new_reaction:
                return "⚠️ Вы уже поставили эту реакцию данному пользователю!"
            old_field = f"{old_type.replace('rating_', '')}_count"
            stats[old_field] = max(0, stats[old_field] - 1)
            supabase.table("user_reactions").delete().eq("id", old_rec["id"]).execute()

        new_field = f"{new_reaction.replace('rating_', '')}_count"
        stats[new_field] += 1
        
        supabase.table("user_reactions").insert({
            "reporter_id": reporter_id,
            "target_user_id": target_id,
            "target_username": target_username.lower(),
            "reaction_type": new_reaction
        }).execute()
        
        await add_or_update_scammer_by_id(user_id=target_id, username=target_username, req_type=new_reaction, proof_text=None, has_proof=False)
        supabase.table("scammers").update({"clown_count": stats["clown_count"], "suspect_count": stats["suspect_count"], "good_count": stats["good_count"]}).eq("user_id", target_id if target_id != 0 else -1).execute()
        return "✅ Реакция обновлена! Старый голос аннулирован."
    except Exception as e:
        logger.error(f"Ошибка реакций: {e}")
        return "❌ Ошибка обновления реакции базы."

@router.callback_query(F.data.startswith("vote:"))
async def process_vote_reaction(callback: CallbackQuery):
    _, reaction_type, t_username, t_id = callback.data.split(":")
    t_id = int(t_id)
    if callback.from_user.id == t_id:
        await callback.answer(TEXTS["self_report"], show_alert=True)
        return
    result_text = await apply_user_reaction(callback.from_user.id, t_username, t_id, reaction_type)
    await callback.answer(result_text, show_alert=True)

# =====================================================================
# ПОДАЧА ЖАЛОБЫ С МЕДИАПРУФАМИ (ТОЛЬКО В ЛС)
# =====================================================================
@router.message(F.text == "🚨 Сообщить о пользователе", F.chat.type == "private")
async def start_report(message: Message, state: FSMContext):
    if not check_cooldown(message.from_user.id):
        await message.answer(TEXTS["cooldown"], parse_mode="HTML")
        return
    await state.set_state(BotStates.waiting_for_target)
    await message.answer(TEXTS["report_start"], parse_mode="HTML", reply_markup=get_cancel_kb())

@router.message(BotStates.waiting_for_target, F.chat.type == "private")
async def process_target(message: Message, state: FSMContext, bot: Bot):
    user_id = 0
    db_username = None

    if message.forward_from:
        user_id = message.forward_from.id
        db_username = message.forward_from.username or f"id_{user_id}"
    elif message.forward_from_chat:
        user_id = message.forward_from_chat.id
        db_username = message.forward_from_chat.username or message.forward_from_chat.title
    else:
        user_id, db_username = await combo_resolve_target(bot, message.text)

    db_username = db_username.replace("@", "").strip().lower()

    if user_id == message.from_user.id:
        await message.answer(TEXTS["self_report"], reply_markup=get_main_menu_kb())
        await state.clear()
        return

    await state.update_data(target_user_str=db_username, target_user_id=user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Предоставить Железный Пруф", callback_data="report_action:proof")]])
    display_id = f"<code>{user_id}</code>" if user_id != 0 else "<i>Вычисляется модератором</i>"
    await message.answer(f"🎯 <b>Цель определена:</b>\n👤 Юзернейм: @{db_username}\n🆔 ID: {display_id}\n\nНажмите кнопку ниже для отправки материалов.", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("report_action:"))
async def report_action(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(BotStates.waiting_for_reason)
    await callback.message.answer("📎 <b>Отправьте доказательства:</b>\nПринимаются скриншоты, видео или файлы с текстовым описанием.", parse_mode="HTML", reply_markup=get_cancel_kb())
    await callback.answer()

@router.message(BotStates.waiting_for_reason, F.chat.type == "private")
async def process_proof_delivery(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    
    t_id = data.get("target_user_id", 0)
    t_user = data["target_user_str"]
    
    reason_text = message.text or message.caption or "Медиа-доказательство без текста"
    reason_text = reason_text.replace("<", "&lt;").replace(">", "&gt;")
    
    media_type = "text"
    file_id = None
    
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id

    try:
        dup = supabase.table("moderation_requests").select("id").eq("reporter_id", message.from_user.id).eq("target_username", t_user).eq("status", "pending").execute()
        if dup.data:
            await message.answer("⚠️ У вас уже есть активная жалоба на этого пользователя.", reply_markup=get_main_menu_kb())
            return

        supabase.table("moderation_requests").insert({
            "chat_id": message.chat.id, "target_username": t_user, "target_user_id": t_id,
            "reporter_id": message.from_user.id, "reporter_name": message.from_user.full_name or "Пользователь",
            "req_type": "proof", "reason": reason_text, "media_type": media_type, "file_id": file_id, "status": "pending"
        }).execute()
        
        await message.answer("✅ <b>Ваши доказательства приняты модерацией!</b>", parse_mode="HTML", reply_markup=get_main_menu_kb())
        if await is_admin(PRIMARY_ADMIN_ID):
            await bot.send_message(PRIMARY_ADMIN_ID, f"👑 <b>Новый пруф против @{t_user}!</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡ Открыть панель", callback_data="admin_view_now")]]))
    except Exception as e:
        await message.answer(f"❌ Ошибка сохранения: {e}", reply_markup=get_main_menu_kb())

# =====================================================================
# АДМИН-ПАНЕЛЬ
# =====================================================================
@router.message(F.text == "⚙️ Панель Модератора", F.chat.type == "private")
@router.callback_query(F.data == "admin_view_now")
async def admin_dashboard(event, bot: Bot):
    u_id = event.from_user.id
    if not await is_admin(u_id):
        return
    is_cb = isinstance(event, CallbackQuery)
    msg = event.message if is_cb else event

    requests = await get_pending_requests()
    admin_kb_buttons = [[InlineKeyboardButton(text="➕ Назначить Модератора", callback_data="adm_add_mod")]]

    if not requests:
        text = "✨ <b>Очередь модерации пуста!</b>"
        await (event.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=admin_kb_buttons)) if is_cb else msg.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=admin_kb_buttons)))
        return

    req = requests[0]
    r_id = req['id']
    media_type = req.get("media_type", "text")
    f_id = req.get("file_id")

    caption = f"📋 <b>Заявление #{r_id}</b>\n\n👤 <b>Цель:</b> @{req['target_username']} (ID: <code>{req['target_user_id']}</code>)\n📝 <b>Заявитель:</b> {req['reporter_name']}\n💬 <b>Текст:</b> {req['reason']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_dec:approve:{r_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_dec:reject:{r_id}")],
        [InlineKeyboardButton(text="➕ Назначить Модератора", callback_data="adm_add_mod")]
    ])

    if is_cb:
        await event.message.delete()
    if media_type == "photo" and f_id:
        await bot.send_photo(msg.chat.id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")
    elif media_type == "video" and f_id:
        await bot.send_video(msg.chat.id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")
    elif media_type == "document" and f_id:
        await bot.send_document(msg.chat.id, f_id, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await bot.send_message(msg.chat.id, caption, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "adm_add_mod")
async def adm_add_mod_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != PRIMARY_ADMIN_ID:
        await callback.answer("🔒 Доступно только Создателю.", show_alert=True)
        return
    await state.set_state(BotStates.waiting_for_new_admin)
    await callback.message.answer("✏️ Введите цифровой Telegram ID нового Модератора:", reply_markup=get_cancel_kb())
    await callback.answer()

@router.message(BotStates.waiting_for_new_admin, F.chat.type == "private")
async def adm_add_mod_finish(message: Message, state: FSMContext):
    if message.from_user.id != PRIMARY_ADMIN_ID:
        return
    raw = message.text.strip()
    await state.clear()
    if not raw.isdigit():
        await message.answer("❌ Неверный ID.", reply_markup=get_main_menu_kb())
        return
    try:
        supabase.table("staff_members").insert({"user_id": int(raw), "assigned_at": datetime.now().isoformat()}).execute()
        await message.answer(f"🎉 Модератор {raw} успешно добавлен!", parse_mode="HTML", reply_markup=get_main_menu_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка добавления: {e}", reply_markup=get_main_menu_kb())

@router.callback_query(F.data.startswith("adm_dec:"))
async def handle_admin_decision(callback: CallbackQuery, bot: Bot):
    if not await is_admin(callback.from_user.id):
        return
    _, action, r_id = callback.data.split(":")
    r_id = int(r_id)
    try:
        res = supabase.table("moderation_requests").select("*").eq("id", r_id).execute()
        if res.data:
            req = res.data[0]
            if action == "approve":
                await add_or_update_scammer_by_id(user_id=req['target_user_id'], username=req['target_username'], req_type="proof", proof_text=req['reason'], has_proof=True)
                await update_request_status(r_id, "approved")
                try:
                    await bot.send_message(req['chat_id'], f"✨ Ваше заявление #{r_id} одобрено!")
                except Exception: pass
            else:
                await update_request_status(r_id, "rejected")
                try:
                    await bot.send_message(req['chat_id'], f"⚠️ Ваше заявление #{r_id} отклонено.")
                except Exception: pass
        await callback.message.delete()
        await admin_dashboard(callback, bot)
    except Exception:
        await callback.answer("Ошибка базы данных.")
