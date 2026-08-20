import json
from pathlib import Path
from config import TELEGRAM_CHAT_ID, BASE_DIR

CHAT_IDS_FILE = BASE_DIR / "chat_ids.json"

def load_chat_ids():
    """
    Load chat IDs from JSON file. If it doesn't exist, initialize with the default from config.
    """
    if not CHAT_IDS_FILE.exists():
        initial_ids = []
        if TELEGRAM_CHAT_ID:
            initial_ids.append(str(TELEGRAM_CHAT_ID))
        save_chat_ids(initial_ids)
        return initial_ids
        
    try:
        with open(CHAT_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Error loading chat IDs: {e}")
        return []

def save_chat_ids(chat_ids):
    """
    Save the list of chat IDs to JSON file.
    """
    try:
        with open(CHAT_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(set(chat_ids)), f, indent=4)
    except Exception as e:
        print(f"⚠️ Error saving chat IDs: {e}")

def add_chat_id(chat_id):
    """
    Add a new chat ID if it doesn't exist. Returns True if added, False if already exists.
    """
    chat_ids = load_chat_ids()
    chat_id_str = str(chat_id)
    if chat_id_str not in chat_ids:
        chat_ids.append(chat_id_str)
        save_chat_ids(chat_ids)
        return True
    return False

def remove_chat_id(chat_id):
    """
    채팅방 ID를 목록에서 제거합니다. (봇이 차단·추방되어 영구 발송 불가일 때 사용)
    제거하면 True, 원래 없었으면 False를 반환합니다.
    사용자가 다시 받고 싶으면 봇에게 /start를 보내면 재등록됩니다.
    """
    chat_ids = load_chat_ids()
    chat_id_str = str(chat_id)
    if chat_id_str not in chat_ids:
        return False
    chat_ids.remove(chat_id_str)
    save_chat_ids(chat_ids)
    print(f"🗑️ 발송 불가 채팅방을 목록에서 제거했습니다: {chat_id_str}")
    return True

def get_all_chat_ids():
    """
    Get all registered chat IDs.
    """
    return load_chat_ids()
