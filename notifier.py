import time
import requests
from config import TELEGRAM_BOT_TOKEN
from chat_manager import get_all_chat_ids, remove_chat_id

# 발송 재시도 설정
MAX_SEND_RETRIES = 3


def describe_telegram_error(response):
    """텔레그램 응답에서 사람이 읽을 수 있는 오류 설명을 뽑아냅니다."""
    try:
        data = response.json()
        return data.get("description") or response.text
    except Exception:
        return response.text


def is_permanently_unreachable(response):
    """
    해당 채팅방으로 다시 시도해도 소용없는 상태인지 판정합니다.
    403 Forbidden = 사용자가 봇을 차단했거나, 그룹에서 봇이 추방됐거나, 계정이 삭제된 경우.
    """
    if response.status_code != 403:
        return False
    return True


def post_to_telegram(url, *, data=None, files=None, timeout=30, label=""):
    """
    텔레그램 API에 POST하고, 일시적 실패는 재시도합니다.

    - 429(속도 제한): 응답의 retry_after만큼 대기 후 재시도
    - 네트워크 오류/타임아웃: 지수 백오프(2s, 4s)로 재시도
    - 그 외 응답은 그대로 돌려주어 호출부가 판단하게 합니다.

    반환: (response 또는 None, 마지막 예외 메시지 또는 None)
    """
    last_error = None

    for attempt in range(MAX_SEND_RETRIES):
        try:
            # files를 쓰는 경우 재시도마다 새로 구성해야 하므로 호출부에서 callable로 넘긴다
            payload_files = files() if callable(files) else files
            response = requests.post(url, data=data, files=payload_files, timeout=timeout)

            if response.status_code == 429:
                retry_after = 1
                try:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    pass
                if attempt < MAX_SEND_RETRIES - 1:
                    print(f"⏳ 텔레그램 속도 제한({label}). {retry_after}초 후 재시도합니다.")
                    time.sleep(min(retry_after, 30))
                    continue

            return response, None

        except Exception as e:
            last_error = str(e)
            if attempt < MAX_SEND_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                print(f"⚠️ 텔레그램 발송 오류({label}): {last_error[:120]} — {wait}초 후 재시도")
                time.sleep(wait)

    return None, last_error

def send_telegram_message(text, chat_id=None, parse_mode="Markdown"):
    """
    Send a message to all configured Telegram chats, or a specific chat_id if provided.
    If the message exceeds 4096 characters, split it into chunks.
    """
    chat_ids = [chat_id] if chat_id else get_all_chat_ids()
    if not TELEGRAM_BOT_TOKEN or not chat_ids:
        print("⚠️ Telegram bot token or chat IDs are missing. Skipping notification.")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Telegram max limit is 4096. We slice at 4000 to be safe and avoid breaking markdown tags.
    MAX_LENGTH = 4000
    chunks = []
    
    # 1. Pre-split by explicit delimiter if present
    pre_chunks = text.split("<!-- SPLIT_HERE -->")
    
    for pt in pre_chunks:
        pt = pt.strip()
        if not pt:
            continue
            
        if len(pt) <= MAX_LENGTH:
            chunks.append(pt)
        else:
            # 2. Split by newlines to preserve readable formatting
            lines = pt.split("\n")
            current_chunk = ""
            in_table = False
            table_header = ""
            
            for line in lines:
                # Detect markdown table separator line (e.g., | :--- | :--- |)
                if line.strip().startswith("|") and ":---" in line.replace(" ", ""):
                    in_table = True
                    # The previous line is the column names
                    prev_line = current_chunk.strip().split("\n")[-1] if current_chunk.strip() else ""
                    table_header = prev_line + "\n" + line + "\n"
                elif in_table and line.strip() != "" and not line.strip().startswith("|"):
                    in_table = False
                    
                if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    if in_table and table_header:
                        current_chunk = table_header
                    current_chunk += line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            
    success = True
    for chat_id in chat_ids:
        print(f"Sending Telegram notification to chat {chat_id} in {len(chunks)} message(s)...")
        for idx, chunk in enumerate(chunks, 1):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            try:
                r = requests.post(url, json=payload, timeout=15)

                # 봇이 차단·추방된 채팅방은 재시도해도 소용없으므로 목록에서 정리하고 다음 방으로
                if is_permanently_unreachable(r):
                    print(f"🚫 {chat_id}: 봇이 차단·추방되어 더 이상 발송할 수 없습니다. "
                          f"({describe_telegram_error(r)})")
                    remove_chat_id(chat_id)
                    break

                # If markdown parsing fails (e.g. unclosed asterisks), fall back to plain text
                if r.status_code != 200:
                    print(f"⚠️ Telegram returned status {r.status_code}. Retrying without Markdown parsing...")
                    payload.pop("parse_mode", None)
                    r = requests.post(url, json=payload, timeout=15)

                if r.status_code == 200:
                    print(f"✅ Telegram message {idx}/{len(chunks)} sent successfully to {chat_id}.")
                else:
                    print(f"❌ Telegram message {idx}/{len(chunks)} to {chat_id} failed: {r.text}")
                    success = False
            except Exception as e:
                print(f"❌ Exception sending to Telegram chat {chat_id}: {e}")
                success = False

    return success

def send_telegram_document(file_path):
    """
    Send a document file to all configured Telegram chats.
    """
    chat_ids = get_all_chat_ids()
    if not TELEGRAM_BOT_TOKEN or not chat_ids:
        print("⚠️ Telegram bot token or chat IDs are missing. Skipping document notification.")
        return False
        
    import os
    if not os.path.exists(file_path):
        print(f"⚠️ File not found: {file_path}")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    
    success = True
    for chat_id in chat_ids:
        print(f"Sending document {file_path} to Telegram chat {chat_id}...")
        try:
            with open(file_path, "rb") as f:
                files = {"document": f}
                payload = {"chat_id": chat_id}
                r = requests.post(url, data=payload, files=files, timeout=30)
                
            if r.status_code == 200:
                print(f"✅ Telegram document sent successfully to {chat_id}.")
            else:
                print(f"❌ Telegram document send to {chat_id} failed: {r.text}")
                success = False
        except Exception as e:
            print(f"❌ Exception sending document to Telegram chat {chat_id}: {e}")
            success = False
            
    return success

def send_telegram_photo(photo_bytes):
    """
    이미지(메모리상의 bytes)를 등록된 모든 텔레그램 채팅방에 발송합니다.

    - 첫 발송이 성공하면 텔레그램이 돌려준 file_id를 재사용해, 나머지 채팅방에는
      같은 이미지를 다시 업로드하지 않습니다. (업로드 1회로 축소)
    - 429(속도 제한)와 일시적 네트워크 오류는 재시도합니다.
    - 403(봇 차단/추방)은 재시도해도 소용없으므로 해당 채팅방을 목록에서 제거합니다.

    반환: {"sent": [chat_id...], "failed": [(chat_id, 사유)...], "removed": [chat_id...]}
    """
    result = {"sent": [], "failed": [], "removed": []}

    chat_ids = get_all_chat_ids()
    if not TELEGRAM_BOT_TOKEN or not chat_ids:
        print("⚠️ Telegram bot token or chat IDs are missing. Skipping photo notification.")
        return result

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    file_id = None  # 첫 업로드 성공 시 텔레그램이 부여하는 이미지 ID

    for chat_id in chat_ids:
        print(f"Sending photo to Telegram chat {chat_id}...")

        if file_id:
            # 이미 업로드된 이미지를 ID로 재전송 (업로드 없음)
            response, error = post_to_telegram(
                url, data={"chat_id": chat_id, "photo": file_id}, label=f"photo:{chat_id}"
            )
        else:
            # 재시도 때마다 파일 객체를 새로 만들어야 하므로 callable로 전달
            response, error = post_to_telegram(
                url,
                data={"chat_id": chat_id},
                files=lambda: {"photo": ("infographic.png", photo_bytes, "image/png")},
                label=f"photo:{chat_id}",
            )

        if response is None:
            print(f"❌ Exception sending photo to Telegram chat {chat_id}: {error}")
            result["failed"].append((chat_id, error or "unknown error"))
            continue

        if response.status_code == 200:
            print(f"✅ Telegram photo sent successfully to {chat_id}.")
            result["sent"].append(chat_id)

            # 첫 성공 시 file_id를 확보해 이후 채팅방에서 재사용
            if not file_id:
                try:
                    photos = response.json().get("result", {}).get("photo", [])
                    if photos:
                        file_id = photos[-1].get("file_id")
                except Exception:
                    pass
            continue

        reason = describe_telegram_error(response)
        print(f"❌ Telegram photo send to {chat_id} failed: {response.text}")

        if is_permanently_unreachable(response):
            # 차단·추방된 채팅방은 매 실행마다 실패하므로 목록에서 정리한다
            print(f"🚫 {chat_id}: 봇이 차단·추방되어 더 이상 발송할 수 없습니다. ({reason})")
            if remove_chat_id(chat_id):
                result["removed"].append(chat_id)
        else:
            result["failed"].append((chat_id, reason))

    return result
