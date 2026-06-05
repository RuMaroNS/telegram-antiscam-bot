import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import TelegramObject, Message
from dotenv import load_dotenv
from handlers import router
from database.supabase_db import supabase

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# =====================================================================
# MIDDLEWARE ДЛЯ ПАССИВНОГО СБОРА ЮЗЕРОВ (ЛИЦО В ЛИЦО)
# =====================================================================
class TrackUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        # Проверяем, что пришло именно сообщение и оно от реального человека
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            username = event.from_user.username.replace("@", "").strip() if event.from_user.username else None
            full_name = event.from_user.full_name
            
            # Сохраняем только если есть юзернейм, чтобы наполнять OSINT-кэш
            if username:
                try:
                    supabase.table("users").upsert({
                        "user_id": user_id,
                        "username": username.lower(), # Сохраняем в нижнем регистре для удобного поиска
                        "full_name": full_name
                    }, on_conflict="user_id").execute()
                except Exception as e:
                    logger.error(f"Ошибка пассивного кэширования юзера в Middleware: {e}")
                    
        # Пропускаем апдейт дальше к твоим хэндлерам кнопок
        return await handler(event, data)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    # Регистрируем наш Middleware прямо на роутер сообщений
    router.message.middleware(TrackUserMiddleware())
    
    dp.include_router(router)
    
    print("Бот успешно запущен в комбо-режиме...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
