import logging
import asyncio
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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
# КОНФИГУРАЦИЯ И ЛОКАЛИЗАЦИЯ (Пункт 34)
# =====================================================================
PRIMARY_ADMIN_ID = 6176762600  # Твой неизменяемый ID владельца

TEXTS = {
    "welcome": "👋 <b>Добро пожаловать в AntiScamBase | ASB!</b>\n\nПрофессиональный комбайн мониторинга угроз и верификации контрагентов.",
    "report_start": "🚨 <b>Подача заявления</b>\n\nОтправьте цель одним из способов:\n1️⃣ Перешлите сообщение скамера.\n2️⃣ Введите @username.\n3️⃣ Введите цифровой Telegram ID.",
    "cancel_ok": "❌ Действие полностью отменено. Возврат в главное меню.",
    "cooldown": "⏳ <b>Защита от флуда!</b> Пожалуйста, подождите немного перед следующим запросом.",
    "self_report": "⚠️ Вы не можете подать жалобу или поставить реакцию самому себе."
}

# =====================================================================
# СОСТОЯНИЯ FSM (Пункт 10)
# =====================================================================
class BotStates(StatesGroup):
    waiting_for_target = State()
    waiting_for_reason = State()
    waiting_for_check = State()
    waiting_for_new_admin = State()

# =====================================================================
# ВСПУМОГАТЕЛЬНЫЕ КНОПКИ
# =====================================================================
def get_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="fsm_cancel")]])

# =====================================================================
# ДИНАМИЧЕСКИЙ МЕНЕДЖЕР АДМИНИСТРАТОРОВ (Пункт 23)
# =====================================================================
async def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь владельцем или назначенным оператором."""
    if user_id == PRIMARY_ADMIN_ID:
        return True
    try:
        # Используем новое имя таблицы: staff_members
        res = supabase.table("staff_members").select("user_id").eq("user_id", user_id).execute()
        return len(res.data) > 0
    except Exception:
        return False

# =====================================================================
# СИСТЕМА АНТИ-ФЛУДА И КОМБО-РЕЗОЛВЕР (Пункты 6, 15, 30, 31)
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
# ДВИЖОК СИСТЕМЫ РЕАКЦИЙ С ПЕРЕЗАПИСЬЮ (Ультимативное требование)
# =====================================================================
async def apply_user_reaction(reporter_id: int, target_username: str, target_id: int, new_reaction: str) -> str:
    """Управляет реакциями (клоун/подозреваемый/гуд). Стирает старую реакцию при выборе новой."""
    try:
        # Ищем, была ли уже реакция от этого юзера на эту цель
        query = supabase.table("user_reactions").select("id", "reaction_type").eq("reporter_id", reporter_id)
        if target_id and target_id != 0:
            query = query.eq("target_user_id", target_id)
        else:
            query = query.eq("target_username", target_username.lower())
        
        existing = query.execute()
        
        # Получаем текущие данные скамера для обновления счетчиков
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
            
            # Уменьшаем старый счетчик
            old_field = f"{old_type.replace('rating_', '')}_count"
            stats[old_field] = max(0, stats[old_field] - 1)
            
            # Удаляем старую запись реакции
            supabase.table("user_reactions").delete().eq("id", old_rec["id"]).execute()

        # Увеличиваем новый счетчик
        new_field = f"{new_reaction.replace('rating_', '')}_count"
        stats[new_field] += 1
        
        # Записываем новую реакцию в журнал
        supabase.table("user_reactions").insert({
            "reporter_id": reporter_id,
            "target_user_id": target_id,
            "target_username": target_username.lower(),
            "reaction_type": new_reaction
        }).execute()
        
        # Обновляем сводную таблицу скамеров
        await add_or_update_scammer_by_id(
            user_id=target_id,
            username=target_username,
            req_type=new_reaction,
            proof_text=None,
            has_proof=False
        )
        
        # Синхронизируем точечные декременты через прямую корректировку
        supabase.table("scammers").update({
            "clown_count": stats["clown_count"],
            "suspect_count": stats["suspect_count"],
            "good_count": stats["good_count"]
        }).eq("user_id", target_id if target_id != 0 else -1).execute()
        
        return "✅ Ваша реакция успешно учтена! Предыдущий голос аннулирован."
    except Exception as e:
        logger.error(f"Ошибка в движке реакций: {e}")
        return f"❌ Системная ошибка обработки реакции: {e}"

# =====================================================================
# ВНУТРЕННИЙ МЕНЕДЖЕР МЕДИА (Пункты 1, 2, 3, 4, 19, 32)
# =====================================================================
async def get_approved_proofs_v2(username: str, user_id: int) -> list:
    try:
        query = supabase.table("moderation_requests").select("id", "req_type").eq("status", "approved").eq("req_type", "proof")
        if user_id and user_id != 0:
            query = query.eq("target_user_id", user_id)
        else:
            query = query.eq("target_username", username.lower())
        res = query.execute()
        return res.data
    except Exception:
        return []

# =====================================================================
# КОМАНДА START И ДИПЛИНКИ (Пункты 3, 20)
# =====================================================================
@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()  # Пункт 20
    
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

    await message.answer(TEXTS["welcome"], parse_mode="HTML")

# =====================================================================
# СБРОС СОСТОЯНИЙ (Пункт 10)
# =====================================================================
@router.callback_query(F.data == "fsm_cancel")
async def cancel_fsm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(TEXTS["cancel_ok"])
    await callback.answer()

# =====================================================================
# ПОДАЧА ЖАЛОБЫ (Пункты 5, 6, 7, 15)
# =====================================================================
@router.message(F.text == "🚨 Сообщить о пользователе")
async def start_report(message: Message, state: FSMContext):
    if not check_cooldown(message.from_user.id):
        await message.answer(TEXTS["cooldown"], parse_mode="HTML")
        return
    await state.set_state(BotStates.waiting_for_target)
    await message.answer(TEXTS["report_start"], parse_mode="HTML", reply_markup=get_cancel_kb())

@router.message(BotStates.waiting_for_target)
async def process_target(message: Message, state: FSMContext, bot: Bot):
    await bot.send_chat_action(message.chat.id, "typing")
    
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
        await message.answer(TEXTS["self_report"])
        await state.clear()
        return

    await state.update_data(target_user_str=db_username, target_user_id=user_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Предоставить Железный Пруф", callback_data="report_action:proof")]
    ])
    
    display_id = f"<code>{user_id}</code>" if user_id != 0 else "<i>Вычисляется модератором</i>"
    await message.answer(
        f"🎯 <b>Цель определена:</b>\n👤 Юзернейм: @{db_username}\n🆔 ID: {display_id}\n\nНажмите кнопку ниже для отправки материалов.",
        parse_mode="HTML", reply_markup=kb
    )

@router.callback_query(F.data.startswith("report_action:"))
async def report_action(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(BotStates.waiting_for_reason)
    await callback.message.answer("📎 <b>Отправьте доказательства:</b>\nПринимаются любые сообщения, скриншоты, видеозаписи или документы с текстовым описанием ситуации.", parse_mode="HTML", reply_markup=get_cancel_kb())
    await callback.answer()

@router.message(BotStates.waiting_for_reason)
async def process_proof_delivery(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    
    t_id = data.get("target_user_id", 0)
    t_user = data["target_user_str"]
    
    # Всеядный сбор медиаконтента (Пункты 1, 2)
    reason_text = message.text or message.caption or "Медиа-доказательство без текстового сопровождения"
    
    # Чистка тегов (Пункт 8)
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
        # Проверка на накрутку пруфов (Пункт 5)
        dup = supabase.table("moderation_requests").select("id").eq("reporter_id", message.from_user.id).eq("target_username", t_user).eq("status", "pending").execute()
        if dup.data:
            await message.answer("⚠️ У вас уже есть активная жалоба на этого пользователя на рассмотрении.")
            return

        # Запись в базу данных
        supabase.table("moderation_requests").insert({
            "chat_id": message.chat.id,
            "target_username": t_user,
            "target_user_id": t_id,
            "reporter_id": message.from_user.id,
            "reporter_name": message.from_user.full_name or "Пользователь",
            "req_type": "proof",
            "reason": reason_text,
            "media_type": media_type,
            "file_id": file_id,
            "status": "pending"
        }).execute()
        
        await message.answer("✅ <b>Ваши доказательства успешно приняты!</b>\nОни будут проверены модераторами в ближайшее время.", parse_mode="HTML")
        
        # Сигнал администрации (Пункт 4)
        if await is_admin(PRIMARY_ADMIN_ID):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡ Открыть панель", callback_data="admin_view_now")]])
            await bot.send_message(PRIMARY_ADMIN_ID, f"👑 <b>Новый пруф на модерацию против @{t_user}!</b>", parse_mode="HTML", reply_markup=kb)
            
    except Exception as e:
        await message.answer(f"❌ Системный сбой при сохранении: {e}")

# =====================================================================
# МГНОВЕННАЯ ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ И СИСТЕМА РЕАКЦИЙ (Пункты 14, 19, 35)
# =====================================================================
@router.message(F.text == "🔍 Проверить пользователя")
async def ask_check(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_check)
    await message.answer("Введите @username или цифровой ID для проверки в базе данных:", reply_markup=get_cancel_kb())

@router.message(BotStates.waiting_for_check)
async def perform_check(message: Message, state: FSMContext, bot: Bot):
    raw_input = message.text.strip()
    await state.clear()
    
    await bot.send_chat_action(message.chat.id, "typing")
    user_id, username = await combo_resolve_target(bot, raw_input)
    
    search_id = user_id if user_id else (int(raw_input) if raw_input.isdigit() else 0)
    search_username = username if username else raw_input.replace("@", "").strip().lower()

    scammer = await get_user_by_id_or_username(user_id=search_id, username=search_username)
    
    # Цветовые грейды опасности (Пункт 35)
    proofs = await get_approved_proofs_v2(search_username, search_id)
    
    if proofs:
        status_header = "🔴 <b>КРИТИЧЕСКИЙ РЕВЕЛИРОВАНИЕ: СКАМЕР / МОШЕННИК</b> 🔴"
    elif scammer and (scammer.get("clown_count", 0) > 0 or scammer.get("suspect_count", 0) > 0):
        status_header = "🟡 <b>ВНИМАНИЕ: ПОВЫШЕННЫЙ УРОВЕНЬ ПОДОЗРЕНИЙ</b> 🟡"
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
            # Скрытые ссылки диплинков (Пункт 14, 19)
            link = f"https://t.me/{bot_info.username}?start=p_{p['id']}"
            text += f"🔘 <a href='{link}'>Скам {idx}</a>  "
        text += "\n"

    # Клавиатура мгновенной оценки (Один пользователь - один голос с перезаписью)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤡 Клоун", callback_data=f"vote:rating_clown:{search_username}:{search_id}"),
            InlineKeyboardButton(text="🤔 Подозреваемый", callback_data=f"vote:rating_suspect:{search_username}:{search_id}"),
            InlineKeyboardButton(text="❤️ Гуд", callback_data=f"vote:rating_good:{search_username}:{search_id}")
        ]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

@router.callback_query(F.data.startswith("vote:"))
async def process_vote_reaction(callback: CallbackQuery):
    _, reaction_type, t_username, t_id = callback.data.split(":")
    t_id = int(t_id)
    
    if callback.from_user.id == t_id:
        await callback.answer(TEXTS["self_report"], show_alert=True)
        return

    # Запуск динамического перезаписываемого обработчика реакций
    result_text = await apply_user_reaction(callback.from_user.id, t_username, t_id, reaction_type)
    await callback.answer(result_text, show_alert=True)

# =====================================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ МОДЕРАТОРАМИ И ОЧЕРЕДЬЮ (Пункты 4, 11, 13, 16, 23, 24)
# =====================================================================
@router.message(F.text == "⚙️ Панель Модератора")
@router.callback_query(F.data == "admin_view_now")
async def admin_dashboard(event, bot: Bot):
    u_id = event.from_user.id
    if not await is_admin(u_id):
        return

    is_cb = isinstance(event, CallbackQuery)
    msg = event.message if is_cb else event

    requests = await get_pending_requests()
    
    # Кнопки управления панелью (Добавление админов в staff_members)
    admin_kb_buttons = [
        [InlineKeyboardButton(text="➕ Назначить Модератора", callback_data="adm_add_mod")]
    ]

    if not requests:
        text = "✨ <b>Очередь модерации полностью пуста!</b>\nВсе входящие заявления успешно обработаны."
        kb = InlineKeyboardMarkup(inline_keyboard=admin_kb_buttons)
        if is_cb:
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await msg.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    req = requests[0]
    r_id = req['id']
    media_type = req.get("media_type", "text")
    f_id = req.get("file_id")

    caption = (
        f"📋 <b>Заявление на проверку #{r_id}</b>\n\n"
        f"👤 <b>Цель:</b> @{req['target_username']} (ID: <code>{req['target_user_id']}</code>)\n"
        f"📝 <b>Заявитель:</b> {req['reporter_name']} (ID: <code>{req['reporter_id']}</code>)\n"
        f"💬 <b>Текст/Подпись:</b> {req['reason']}"
    )

    control_buttons = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_dec:approve:{r_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_dec:reject:{r_id}")
        ],
        [InlineKeyboardButton(text="➕ Назначить Модератора", callback_data="adm_add_mod")]
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=control_buttons)

    try:
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
    except Exception as e:
        logger.error(f"Ошибка вывода админ-панели: {e}")
        await bot.send_message(msg.chat.id, f"⚠️ Сбой верстки медиа в заявке #{r_id}. Вывод текста:\n\n{caption}", reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "adm_add_mod")
async def adm_add_mod_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != PRIMARY_ADMIN_ID:
        await callback.answer("🔒 Данная функция доступна только Создателю бота.", show_alert=True)
        return
    await state.set_state(BotStates.waiting_for_new_admin)
    await callback.message.answer("✏️ Введите цифровой Telegram ID пользователя, которого хотите назначить Модератором:", reply_markup=get_cancel_kb())
    await callback.answer()

@router.message(BotStates.waiting_for_new_admin)
async def adm_add_mod_finish(message: Message, state: FSMContext):
    if message.from_user.id != PRIMARY_ADMIN_ID:
        return
    raw = message.text.strip()
    await state.clear()
    
    if not raw.isdigit():
        await message.answer("❌ ID должен состоять только из цифр. Операция прервана.")
        return
        
    new_id = int(raw)
    try:
        # Инсерт в таблицу staff_members
        supabase.table("staff_members").insert({"user_id": new_id, "assigned_at": datetime.now().isoformat()}).execute()
        await message.answer(f"🎉 Пользователь <code>{new_id}</code> успешно наделен правами Модератора!", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка добавления в базу (возможно, уже админ): {e}")

@router.callback_query(F.data.startswith("adm_dec:"))
async def handle_admin_decision(callback: CallbackQuery, bot: Bot):
    if not await is_admin(callback.from_user.id):
        return

    _, action, r_id = callback.data.split(":")
    r_id = int(r_id)

    try:
        res = supabase.table("moderation_requests").select("*").eq("id", r_id).execute()
        if not res.data:
            await callback.answer("Заявка не найдена.")
            return
            
        req = res.data[0]
        
        if action == "approve":
            await add_or_update_scammer_by_id(
                user_id=req['target_user_id'],
                username=req['target_username'],
                req_type="proof",
                proof_text=req['reason'],
                has_proof=True
            )
            await update_request_status(r_id, "approved")
            await callback.message.answer(f"✅ Заявление #{r_id} одобрено. Пруф внесен в реестр.")
            
            # Уведомление заявителя (Пункт 11)
            try:
                await bot.send_message(req['chat_id'], f"✨ <b>Ваше заявление #{r_id} проверено модератором и одобрено!</b> Спасибо за помощь проекту.", parse_mode="HTML")
            except Exception:
                pass
        else:
            await update_request_status(r_id, "rejected")
            await callback.message.answer(f"❌ Заявление #{r_id} отклонено модератором.")
            
            # Уведомление заявителя об отказе
            try:
                await bot.send_message(req['chat_id'], f"⚠️ <b>Ваше заявление #{r_id} было отклонено</b> модератором после детального анализа предоставленных материалов.", parse_mode="HTML")
            except Exception:
                pass

        await callback.message.delete()
        # Автоматический переход к следующей заявке в очереди
        await admin_dashboard(callback, bot)
    except Exception as e:
        logger.error(f"Ошибка вынесения вердикта: {e}")
        await callback.answer("Ошибка выполнения операции базы данных.")
