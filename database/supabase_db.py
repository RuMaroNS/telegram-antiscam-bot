import os
from supabase import create_client, Client

# Инициализация клиента Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

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
    Создает новую заявку на модерацию в таблице moderation_requests.
    """
    try:
        cleaned_username = target_user.lower().replace("@", "").strip() if target_user else "unknown"
        
        data = {
            "chat_id": chat_id,
            "target_username": cleaned_username,
            "target_user_id": target_user_id or 0,
            "reporter_id": reporter_id,
            "reporter_name": reporter_name,
            "req_type": req_type,
            "reason": reason,
            "media_file_id": media_file_id,
            "status": "pending"
        }
        supabase.table("moderation_requests").insert(data).execute()
    except Exception as e:
        print(f"Ошибка Supabase при создании заявки на модерацию: {e}")


async def get_pending_requests():
    """
    Возвращает список всех активных заявок (со статусом 'pending').
    """
    try:
        response = supabase.table("moderation_requests").select("*").eq("status", "pending").order("id").execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Ошибка Supabase при получении активных заявок: {e}")
        return []


async def update_request_status(req_id: int, status: str):
    """
    Обновляет статус заявки (approved / rejected).
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
    Ищет пользователя в основной базе скамеров по ID или по логину.
    """
    try:
        if user_id and user_id != 0:
            response = supabase.table("scammers").select("*").eq("user_id", user_id).execute()
            if response.data:
                return response.data[0]

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
    Умный апдейт/добавление скамеров. Защищен от дублирования нулевых ID.
    """
    try:
        cleaned_username = username.lower().replace("@", "").strip() if username else None
        
        clown_inc = 1 if req_type == "rating_clown" else 0
        suspect_inc = 1 if req_type == "rating_suspect" else 0
        good_inc = 1 if req_type == "rating_good" else 0

        # Если ТГ-айди нет, ищем и обновляем строго по юзернейму
        if not user_id or user_id == 0:
            response = supabase.table("scammers").select("*").eq("current_username", cleaned_username).execute()
            if response.data:
                existing = response.data[0]
                supabase.table("scammers").update({
                    "clown_count": existing.get("clown_count", 0) + clown_inc,
                    "suspect_count": existing.get("suspect_count", 0) + suspect_inc,
                    "good_count": existing.get("good_count", 0) + good_inc,
                    "has_proof": existing.get("has_proof", False) or has_proof,
                    "proof_text": proof_text if has_proof else existing.get("proof_text")
                }).eq("current_username", cleaned_username).execute()
            else:
                supabase.table("scammers").insert({
                    "user_id": 0,
                    "current_username": cleaned_username,
                    "clown_count": clown_inc,
                    "suspect_count": suspect_inc,
                    "good_count": good_inc,
                    "has_proof": has_proof,
                    "proof_text": proof_text
                }).execute()
            return

        # Если нормальный ID есть — работаем по ID
        existing_user = await get_user_by_id_or_username(user_id=user_id, username=cleaned_username)

        if existing_user:
            updated_fields = {
                "current_username": cleaned_username or existing_user.get("current_username"),
                "clown_count": existing_user.get("clown_count", 0) + clown_inc,
                "suspect_count": existing_user.get("suspect_count", 0) + suspect_inc,
                "good_count": existing_user.get("good_count", 0) + good_inc,
                "has_proof": existing_user.get("has_proof", False) or has_proof,
                "proof_text": proof_text if proof_text else existing_user.get("proof_text")
            }
            if user_id and not existing_user.get("user_id"):
                updated_fields["user_id"] = user_id

            supabase.table("scammers").update(updated_fields).eq("id", existing_user["id"]).execute()
        else:
            supabase.table("scammers").insert({
                "user_id": user_id,
                "current_username": cleaned_username,
                "clown_count": clown_inc,
                "suspect_count": suspect_inc,
                "good_count": good_inc,
                "has_proof": has_proof,
                "proof_text": proof_text
            }).execute()

    except Exception as e:
        print(f"Ошибка Supabase при сохранении/обновлении данных по ID: {e}")


# =====================================================================
# 3. РАБОТА С КЭШЕМ ПОЛЬЗОВАТЕЛЕЙ (ТАБЛИЦА users)
# =====================================================================

async def get_cached_user_by_username(username: str):
    """
    Ищет ID во внутренней OSINT-базе 'users' по юзернейму (Лицо в лицо).
    """
    try:
        cleaned_username = username.lower().replace("@", "").strip()
        response = supabase.table("users").select("user_id").eq("username", cleaned_username).execute()
        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        print(f"Ошибка Supabase при поиске в кэше users: {e}")
        return None
