import os
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from database.supabase_db import create_moderation_request, get_pending_requests, update_request_status, add_to_scammers_base

router = Router()
ADMIN_ID = 6176762600

class ScamStates(StatesGroup):
    waiting_for_reason = State()

def is_valid_text(text: str) -> bool:
    if not text:
        return False
    # Очистка от пробелов и скрытых невидимых символов юникода
    cleaned = text.strip().replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    return len(cleaned) > 0

# --- ГРУППОВЫЕ КОМАНДЫ ---

@router.message(F.chat.type.in_({"group", "supergroup"}), Command("скам"))
async def cmd_scam_group(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) < 2 and not message.reply_to_message:
        await message.reply("Укажите пользователя: `скам @username` или ответьте реплеем на его сообщение.")
        return

    if len(args) >= 2:
        target_user = args[1]
    else:
        if message.reply_to_message.from_user.username:
            target_user = f"@{message.reply_to_message.from_user.username}"
        else:
            target_user = message.reply_to_message.from_user.full_name

    await state.update_data(
        chat_id=message.chat.id,
        target_user=target_user,
        reporter_id=message.from_user.id,
        reporter_name=message.from_user.full_name,
        req_type="scam_report"
    )
    
    await message.reply(
        f"{message.from_user.first_name}, почему ты решил именно такую реакцию поставить?\n"
        f"(Опиши причину, без причины - реакция не применяется)",
        parse_mode=None
    )
    await state.set_state(ScamStates.waiting_for_reason)

@router.message(ScamStates.waiting_for_reason)
async def process_reason(message: Message, state: FSMContext, bot: Bot):
    if not is_valid_text(message.text):
        await message.reply("Ошибка: причина не может быть пустой или состоять из невидимых символов! Напишите нормально.")
        return

    user_data = await state.get_data()
    await state.clear()
    
    await create_moderation_request(
        chat_id=user_data["chat_id"],
        target_user=user_data["target_user"],
        reporter_id=user_data["reporter_id"],
        reporter_name=user_data["reporter_name"],
        req_type=user_data["req_type"],
        reason=message.text
    )
    
    await message.reply("Заявка отправлена администратору на модерацию.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 Да", callback_data="admin_view_now"),
         InlineKeyboardButton(text="❌ Нет, потом", callback_data="admin_view_later")]
    ])
    
    await bot.send_message(
        ADMIN_ID,
        f"{user_data['reporter_name']}, тут есть запросы на внесение данных в скам базу\nхочешь посмотреть или позже?",
        reply_markup=kb
    )

# --- ЛИЧКА АДМИНА ---

@router.message(Command("start"), F.chat.id == ADMIN_ID)
async def admin_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заявки", callback_data="admin_view_now")]
    ])
    await message.answer("Главное меню администратора.", reply_markup=kb)

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
    
    text = (
        f"Заявка на рейтинг пользователю: {req['target_username']}\n"
        f"Реакция: {req['req_type']}\n"
        f"Причина: {req['reason']}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять", callback_data=f"approve_{req['id']}"),
         InlineKeyboardButton(text="Отказать", callback_data=f"reject_{req['id']}")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def handle_decision(callback: CallbackQuery):
    action, req_id = callback.data.split("_")
    req_id = int(req_id)
    
    requests = await get_pending_requests()
    current_req = next((r for r in requests if r['id'] == req_id), None)
    
    if action == "approve":
        await update_request_status(req_id, "approved")
        if current_req:
            await add_to_scammers_base(
                chat_id=current_req['chat_id'],
                username=current_req['target_username'],
                rating="clown",
                proof=current_req['reason']
            )
        await callback.message.edit_text("Понял, заявка принята и была добавлена в базу данных.")
    else:
        await update_request_status(req_id, "rejected")
        await callback.message.edit_text("Хорошо, заявка отказана и не была добавлена.")
        
    next_requests = await get_pending_requests()
    if next_requests:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Показать следующую ➡️", callback_data="admin_view_now")]
        ])
        await callback.message.answer("В очереди есть еще необработанные заявки.", reply_markup=kb)
        
    await callback.answer()
