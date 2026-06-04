import os
from supabase import create_client, Client

# Инициализация клиента Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Переменные окружения SUPABASE_URL или SUPABASE_KEY не заданы!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# =====================================================================
# 1. РАБОТА С ЗАЯВКАМИ НА МОДЕРАЦИЮ (ТАБЛИЦА moderation_requests)
# =====================================================================

async def create_moderation_request(
    chat_id: int, 
    target_user: str, 
    target_user_id: int = None, 
    reporter_id: int = None, 
    reporter_name: str = None, 
    req_type: str = "proof", 
    reason: str = "", 
    media_file_id: str = None
):
    """
    Создает новую заявку на модерацию реакции или пруфа в таблице moderation_requests.
    Сохраняет как текстовый юзернейм нарушителя, так и его цифровой ID (если удалось определить).
    """
    try:
        # Принудительно приводим юзернейм к нижнему регистру для порядка в заявках
        cleaned_username = target_user.lower().replace("@", "").strip() if target_user else "unknown"
        
        data = {
            "chat_id": chat_id,
            "target_username": f"@{cleaned_username}",
            "target_user_id": target_user_id,
            "reporter_id": reporter_id,
            "reporter_name": reporter_name,
            "req_type": req_type,
            "reason": reason,
            "media_file_id": media_file_id,
            "status": "pending"  # Статус по умолчанию — ожидает проверки
        }
        supabase.table("moderation_requests").insert(data).execute()
    except Exception as e:
        print(f"Ошибка Supabase при создании заявки на модерацию: {e}")


async def get_pending_requests():
    """
    Возвращает список всех активных заявок (со статусом 'pending'), 
    которые еще не обработал администратор.
    """
    try:
        response = supabase.table("moderation_requests").select("*").eq("status", "pending").order("id").execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Ошибка Supabase при получении активных заявок: {e}")
        return []


async def update_request_status(req_id: int, status: str):
    """
    Обновляет статус заявки (например, переводит в 'approved' или 'rejected') после решения админа.
    """
    try:
        supabase.table("moderation_requests").update({"status": status}).eq("id", req_id).execute()
    except Exception as e:
        print(f"Ошибка Supabase при обновлении статуса заявки #{req_id}: {e}")


# =====================================================================
# 2. РАБОТА С БАЗОЙ СКАМЕРОВ И РЕЙТИНГАМИ (ТАБЛИЦА scammers)
# =====================================================================

async def get_user_by_id_or_username(user_id: int = None, username: str = None):
    """
    Ищет пользователя в основной базе скамеров.
    
    1. Сначала проверяет по уникальному цифровому 'user_id' (если он передан). Это самый надежный способ.
    2. Если по ID запись не найдена или ID отсутствует (например, поиск идет просто по тексту в личке),
       поиск производится по текстовой колонке 'current_username' в нижнем регистре.
    """
    try:
        # Шаг 1: Ищем жестко по цифровому паспорту (ID)
        if user_id:
            response = supabase.table("scammers").select("*").eq("user_id", user_id).execute()
            if response.data:
                return response.data[0]

        # Шаг 2: Если по ID не нашли или ID равен None — ищем по очищенному тексту в нижнем регистре
        if username:
            cleaned_username = username.lower().replace("@", "").strip()
            response = supabase.table("scammers").select("*").eq("current_username", cleaned_username).execute()
            if response.data:
                return response.data[0]

        return None
    except Exception as e:
        print(f"Ошибка Supabase при поиске пользователя: {e}")
        return None


async def add_or_update_scammer_by_id(
    user_id: int, 
    username: str, 
    req_type: str, 
    proof_text: str = None, 
    has_proof: bool = False
):
    """
    Добавляет новую запись или инкрементирует счетчики реакций уже существующего 
    пользователя строго на основе его уникального 'user_id'.
    
    Даже если нарушитель изменит юзернейм в Telegram, все новые жалобы из групп будут 
    капать в его старую карточку, так как привязка идет по ID чата, а текстовый юзернейм
    просто обновляется на его самую свежую версию в нижнем регистре.
    """
    try:
        # Очищаем юзернейм до чистого lowercase-вида (например: 'r0bone')
        cleaned_username = username.lower().replace("@", "").strip() if username else None
        
        # Проверяем, есть ли уже этот человек в нашей базе данных
        existing_user = await get_user_by_id_or_username(user_id=user_id, username=cleaned_username)

        # Вычисляем, какую именно реакцию одобрил админ
        clown_inc = 1 if req_type == "rating_clown" else 0
        suspect_inc = 1 if req_type == "rating_suspect" else 0
        good_inc = 1 if req_type == "rating_good" else 0

        if existing_user:
            # Сценарий А: Пользователь найден. Обновляем статистику
            new_clown = existing_user.get("clown_count", 0) + clown_inc
            new_suspect = existing_user.get("suspect_count", 0) + suspect_inc
            new_good = existing_user.get("good_count", 0) + good_inc
            
            # Флаг наличия пруфов не должен сбрасываться, если он уже был True
            final_has_proof = existing_user.get("has_proof", False) or has_proof
            
            # Если прилетел новый текстовый пруф, обновляем поле, иначе оставляем старый текст пруфа
            final_proof_text = proof_text if proof_text else existing_user.get("proof_text")

            # Если у записи в базе не было user_id (старая запись), а сейчас мы его узнали — дописываем его
            updated_fields = {
                "current_username": cleaned_username or existing_user.get("current_username"),
                "clown_count": new_clown,
                "suspect_count": new_suspect,
                "good_count": new_good,
                "has_proof": final_has_proof,
                "proof_text": final_proof_text
            }
            
            if user_id and not existing_user.get("user_id"):
                updated_fields["user_id"] = user_id

            # Обновляем по внутреннему первичному ключу таблицы 'id'
            supabase.table("scammers").update(updated_fields).eq("id", existing_user["id"]).execute()

        else:
            # Сценарий Б: Пользователя вообще нет в базе. Создаем запись с нуля
            new_user_data = {
                "user_id": user_id,                  # Железный ID
                "current_username": cleaned_username, # Текст строчными буквами для поиска без учета регистра
                "clown_count": clown_inc,
                "suspect_count": suspect_inc,
                "good_count": good_inc,
                "has_proof": has_proof,
                "proof_text": proof_text
            }
            supabase.table("scammers").insert(new_user_data).execute()

    except Exception as e:
        print(f"Ошибка Supabase при сохранении/обновлении данных по ID: {e}")
