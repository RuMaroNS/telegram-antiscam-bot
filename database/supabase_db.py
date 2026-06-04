import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

async def create_moderation_request(chat_id: int, target_user: str, reporter_id: int, reporter_name: str, req_type: str, reason: str, media_file_id: str = None):
    data = {
        "chat_id": chat_id,
        "target_username": target_user,
        "reporter_id": reporter_id,
        "reporter_name": reporter_name,
        "req_type": req_type,
        "reason": reason,
        "media_file_id": media_file_id,
        "status": "pending"
    }
    res = supabase.table("moderation_requests").insert(data).execute()
    return res.data[0] if res.data else None

async def get_pending_requests():
    res = supabase.table("moderation_requests").select("*").eq("status", "pending").order("created_at").execute()
    return res.data

async def update_request_status(req_id: int, status: str):
    supabase.table("moderation_requests").update({"status": status}).eq("id", req_id).execute()

async def get_user_from_base(username: str):
    res = supabase.table("scammers_base").select("*").eq("username", username).execute()
    return res.data[0] if res.data else None

async def add_or_update_scammer(username: str, req_type: str, proof_text: str = None, has_proof: bool = False):
    user = await get_user_from_base(username)
    
    clown_add = 1 if req_type == "rating_clown" else 0
    suspect_add = 1 if req_type == "rating_suspect" else 0
    good_add = 1 if req_type == "rating_good" else 0
    
    if user:
        data = {
            "clown_count": user["clown_count"] + clown_add,
            "suspect_count": user["suspect_count"] + suspect_add,
            "good_count": user["good_count"] + good_add,
        }
        if has_proof:
            data["has_proof"] = True
            data["proof_text"] = proof_text
            
        supabase.table("scammers_base").update(data).eq("username", username).execute()
    else:
        data = {
            "username": username,
            "clown_count": clown_add,
            "suspect_count": suspect_add,
            "good_count": good_add,
            "has_proof": has_proof,
            "proof_text": proof_text if has_proof else None
        }
        supabase.table("scammers_base").insert(data).execute()
