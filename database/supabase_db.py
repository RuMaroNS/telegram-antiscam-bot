import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Переменные репозитория GitHub
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

async def create_moderation_request(chat_id: int, target_user: str, reporter_id: int, reporter_name: str, req_type: str, reason: str, rating_type: str = None):
    data = {
        "chat_id": chat_id,
        "target_username": target_user,
        "reporter_id": reporter_id,
        "reporter_name": reporter_name,
        "req_type": req_type,
        "rating_type": rating_type,
        "reason": reason,
        "status": "pending"
    }
    res = supabase.table("moderation_requests").insert(data).execute()
    return res.data[0] if res.data else None

async def get_pending_requests():
    res = supabase.table("moderation_requests").select("*").eq("status", "pending").order("created_at").execute()
    return res.data

async def update_request_status(req_id: int, status: str):
    supabase.table("moderation_requests").update({"status": status}).eq("id", req_id).execute()

async def add_to_scammers_base(chat_id: int, username: str, rating: str, proof: str):
    data = {
        "chat_id": chat_id,
        "username": username,
        "final_rating": rating,
        "proof_text": proof
    }
    supabase.table("scammers_base").upsert(data, on_conflict="username").execute()
