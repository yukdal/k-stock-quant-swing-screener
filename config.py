import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

# API Keys
DART_API_KEY = os.getenv("DART_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_ENV = os.getenv("KIS_ENV", "PROD")
KIWOOM_APP_KEY = os.getenv("KIWOOM_APP_KEY")
KIWOOM_APP_SECRET = os.getenv("KIWOOM_APP_SECRET")
TOSS_APP_KEY = os.getenv("TOSS_APP_KEY")
TOSS_APP_SECRET = os.getenv("TOSS_APP_SECRET")
# KRX 정보데이터시스템(data.krx.co.kr) 로그인 정보 (pykrx 수급/지수 조회용, 선택사항)
# load_dotenv가 .env의 값을 환경변수로 올려주므로 pykrx가 자동으로 인식합니다.
KRX_ID = os.getenv("KRX_ID")
KRX_PW = os.getenv("KRX_PW")

# Model Configuration
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Cache Directories
CACHE_DIR = BASE_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
DART_CORP_MAP_PATH = CACHE_DIR / "dart_corp_code_map.json"
# 급락 경보 / 분할매수 알림의 발송 이력(도달한 최고 단계).
# 봇을 재시작해도 같은 단계 알림이 다시 나가지 않도록 파일로 보존합니다.
ALERT_STATE_PATH = CACHE_DIR / "alert_state.json"

# Logging configuration
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE_PATH = LOG_DIR / "screener.log"

def validate_config(test_mode=False):
    """Validate that required variables are present. In test mode, missing keys will only warn."""
    missing = []
    if not DART_API_KEY:
        missing.append("DART_API_KEY")
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        missing.append("GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not KIS_APP_KEY:
        missing.append("KIS_APP_KEY")
    if not KIS_APP_SECRET:
        missing.append("KIS_APP_SECRET")
        
    if missing:
        msg = f"Missing environment variables in .env: {', '.join(missing)}"
        if test_mode:
            print(f"⚠️  [WARNING] {msg}. Some functions may fail during test run.")
            return False
        else:
            print(f"❌ [ERROR] {msg}. Please check your .env file.")
            sys.exit(1)
    return True
