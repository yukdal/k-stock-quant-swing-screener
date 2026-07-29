import os
import json
import time
import requests
import datetime
from pathlib import Path
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ENV, CACHE_DIR

# Base URL setup
if KIS_ENV.upper() == "PROD":
    KIS_URL_BASE = "https://openapi.koreainvestment.com:9443"
else:
    KIS_URL_BASE = "https://openapivts.koreainvestment.com:29443"

TOKEN_FILE = CACHE_DIR / "kis_token.json"

def get_access_token():
    """
    Get Access Token from KIS. Uses cached token if valid.
    Tokens are valid for 24 hours.
    """
    # 캐시된 토큰 확인.
    # 스케줄러(systemd)와 수동 실행이 동시에 캐시를 쓰면 파일이 깨질 수 있으므로,
    # 읽기 실패는 예외를 올리지 않고 '캐시 없음'으로 처리해 새 토큰을 재발급한다.
    # (예외를 올리면 깨진 캐시가 복구되지 않아 KIS 조회가 영구히 실패한다)
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
            if time.time() < data.get("expires_at", 0):
                return data["access_token"]
        except Exception as e:
            print(f"⚠️ KIS 토큰 캐시를 읽을 수 없어 새로 발급합니다: {e}")
            try:
                TOKEN_FILE.unlink()
            except Exception:
                pass

    # Issue new token
    url = f"{KIS_URL_BASE}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }

    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
    except Exception as e:
        print(f"❌ KIS 토큰 요청 실패: {e}")
        return None

    if res.status_code != 200:
        print(f"❌ Failed to get KIS access token: {res.text}")
        return None

    try:
        token_data = res.json()
    except Exception as e:
        print(f"❌ KIS 토큰 응답을 해석할 수 없습니다: {e} / 응답: {res.text[:200]}")
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        print(f"❌ KIS 토큰 응답에 access_token이 없습니다: {res.text[:200]}")
        return None

    expires_in = token_data.get("expires_in", 86400) # usually 86400 sec (24h)

    # Save to cache.
    # 임시 파일에 쓴 뒤 os.replace로 원자적 교체 → 동시 실행 시에도 파일이 깨지지 않는다.
    cache_data = {
        "access_token": access_token,
        "expires_at": time.time() + expires_in - 300 # buffer of 5 mins
    }
    try:
        tmp_path = TOKEN_FILE.with_suffix(f".{os.getpid()}.tmp")
        with open(tmp_path, "w") as f:
            json.dump(cache_data, f)
        os.replace(tmp_path, TOKEN_FILE)
    except Exception as e:
        # 캐시 저장에 실패해도 토큰 자체는 유효하므로 그대로 사용한다
        print(f"⚠️ KIS 토큰 캐시 저장 실패 (동작에는 영향 없음): {e}")

    return access_token

def fetch_daily_price(ticker):
    """
    Fetch daily price history for technical indicators (MA1000, 5/20/60, volume ratio)
    Returns a dataframe-like structure or list of dicts.
    """
    token = get_access_token()
    if not token:
        return None
        
    url = f"{KIS_URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appKey": KIS_APP_KEY,
        "appSecret": KIS_APP_SECRET,
        "tr_id": "FHKST03010100", # 주식당일추이 API or 일자별 시세
    }
    
    # Actually, for 1000 days we need inquire-daily-itemchartprice (국내주식기간별시세)
    # TR_ID: FHKST03010100 (국내주식기간별시세)
    
    today = datetime.date.today()
    end_date = today.strftime("%Y%m%d")
    
    # We need to fetch 1000 days, API returns 100 at a time
    all_data = []
    
    for i in range(12): # 12 * 100 = 1200 trading days
        start_date = (datetime.datetime.strptime(end_date, "%Y%m%d") - datetime.timedelta(days=120)).strftime("%Y%m%d")
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", # 주식
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D", # 일봉
            "FID_ORG_ADJ_PRC": "0" # 수정주가 반영
        }
        
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("rt_cd") == "0":
                chunk = data.get("output2", [])
                if not chunk:
                    break
                all_data.extend(chunk)
                # Next end_date is the day before the last fetched date
                last_date_str = chunk[-1].get("stck_bsop_date")
                if last_date_str:
                    end_date = (datetime.datetime.strptime(last_date_str, "%Y%m%d") - datetime.timedelta(days=1)).strftime("%Y%m%d")
                else:
                    break
            else:
                break
        else:
            break
            
        time.sleep(0.1) # Rate limit protection
        
    return all_data

def fetch_kis_domestic_index(ticker_code="0001", days=5):
    """
    Fetch domestic index history from KIS API.
    ticker_code: "0001" (KOSPI), "1001" (KOSDAQ)
    TR_ID: FHKUP03500100 (국내업종 기간별추이)
    """
    token = get_access_token()
    if not token:
        return None
        
    url = f"{KIS_URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appKey": KIS_APP_KEY,
        "appSecret": KIS_APP_SECRET,
        "tr_id": "FHKUP03500100",
    }
    
    today = datetime.date.today()
    end_date = today.strftime("%Y%m%d")
    start_date = (today - datetime.timedelta(days=max(days, 10))).strftime("%Y%m%d")
    
    params = {
        "FID_COND_MRKT_DIV_CODE": "U", # 업종
        "FID_INPUT_ISCD": ticker_code,
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": end_date,
        "FID_PERIOD_DIV_CODE": "D"
    }
    
    res = requests.get(url, headers=headers, params=params, timeout=10)
    if res.status_code == 200:
        data = res.json()
        if data.get("rt_cd") == "0":
            output = data.get("output2", [])
            if output:
                # Filter valid rows
                output = [row for row in output if row.get('stck_bsop_date')]
                # Reverse to be chronological (oldest first)
                output.reverse()
                return output
        else:
            print(f"⚠️ KIS Index API Error for {ticker_code}: {data.get('msg1')}")
            return None
    return None

def fetch_kis_overseas_index(ticker_code="SPX", days=5):
    """
    Fetch overseas index history from KIS API.
    ticker_code: "SPX" (S&P 500), "NDX" (NASDAQ 100), "DJI" (Dow Jones)
    TR_ID: FHKST03030000 (해외주식 기간별추이)
    """
    token = get_access_token()
    if not token:
        return None
        
    url = f"{KIS_URL_BASE}/uapi/overseas-price/v1/quotations/dailyprice"
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appKey": KIS_APP_KEY,
        "appSecret": KIS_APP_SECRET,
        "tr_id": "FHKST03030000",
    }
    
    # KIS Overseas API usually requires different params depending on the market
    # N: 미국, H: 홍콩, S: 심천, T: 상해, V: 베트남
    params = {
        "AUTH": "",
        "EXCD": "NAS", # NAS or NYS depending on index, but for SPX usually it's handled. We'll try NAS or N.
        "SYMB": ticker_code,
        "GUBN": "0", # 0: 일, 1: 주, 2: 월
        "BYMD": "", # Blank for latest
        "MODP": "0" # 수정주가
    }
    
    # Wait, the dailyprice API endpoint for overseas might be different.
    # We will try the standard FHKST03030000
    res = requests.get(url, headers=headers, params=params, timeout=10)
    if res.status_code == 200:
        data = res.json()
        if data.get("rt_cd") == "0":
            output = data.get("output2", [])
            if output:
                # Filter valid rows
                output = [row for row in output if row.get('xymd')]
                output.reverse()
                return output
        else:
            print(f"⚠️ KIS Overseas Index API Error for {ticker_code}: {data.get('msg1')}")
            return None
    return None

def fetch_investor_trend(ticker):
    """
    Fetch foreign/institutional investor trend.
    TR_ID: FHKST01010900 (주식당일매매종합) - 외국인/기관 수급
    """
    token = get_access_token()
    if not token:
        return None
        
    url = f"{KIS_URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor"
    
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appKey": KIS_APP_KEY,
        "appSecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010900",
    }
    
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker
    }
    
    res = requests.get(url, headers=headers, params=params, timeout=10)
    if res.status_code == 200:
        data = res.json()
        if data.get("rt_cd") == "0":
            output = data.get("output", [])
            if output:
                return output[0] # latest day
        else:
            print(f"⚠️ KIS Investor API Error for {ticker}: {data.get('msg1')}")
            return None
    return None

if __name__ == "__main__":
    # Simple test
    print("Testing KIS API Token...")
    token = get_access_token()
    print("Token fetched." if token else "Failed to fetch token.")
    if token:
        print("Testing price fetch for 005930...")
        hist = fetch_daily_price("005930")
        if hist:
            print(f"Fetched {len(hist)} days. Latest: {hist[0]}")
        print("Testing investor trend for 005930...")
        inv = fetch_investor_trend("005930")
        if inv:
            print(f"Investor trend: {inv}")

def get_kis_current_index(ticker_code):
    """
    Fetch current real-time index price.
    ticker_code: "0001" (KOSPI), "1001" (KOSDAQ)
    TR_ID: FHPUP02100000 (주식업종현재가)
    """
    token = get_access_token()
    if token:
        url = f"{KIS_URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price"
        
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appKey": KIS_APP_KEY,
            "appSecret": KIS_APP_SECRET,
            "tr_id": "FHPUP02100000",
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "U", # 업종
            "FID_INPUT_ISCD": ticker_code
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    output = data.get("output", {})
                    if output:
                        current_close = float(output.get("bstp_nmix_prpr", 0))
                        point_change = float(output.get("bstp_nmix_prdy_vrss", 0))
                        pct_change = float(output.get("bstp_nmix_prdy_ctrt", 0.0))
                        return {
                            "current_close": current_close,
                            "point_change": point_change,
                            "pct_change": pct_change
                        }
            print(f"⚠️ KIS Index API Error for {ticker_code}: {res.text}")
        except Exception as e:
            print(f"❌ Error fetching current index for {ticker_code}: {e}")
            
    return None

def get_etf_current_price(ticker):
    """
    Fetch current real-time price for a specific ETF or stock.
    TR_ID: FHKST01010100 (주식현재가 시세)
    Provides a fallback to yfinance previous close if the API fails.
    """
    token = get_access_token()
    if token:
        url = f"{KIS_URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
        
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appKey": KIS_APP_KEY,
            "appSecret": KIS_APP_SECRET,
            "tr_id": "FHKST01010100",
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    output = data.get("output", {})
                    if output:
                        current_price = int(output.get("stck_prpr", 0))
                        change_rate = float(output.get("prdy_ctrt", 0.0))
                        return {
                            "price": current_price,
                            "change_rate": change_rate
                        }
            print(f"⚠️ KIS Price API Error for {ticker}: {res.text}")
        except Exception as e:
            print(f"❌ Error fetching current price for {ticker}: {e}")
            
    # Fallback 1: Kiwoom API
    try:
        from kiwoom_api import fetch_kiwoom_daily_price
        hist = fetch_kiwoom_daily_price(ticker)
        if hist and len(hist) >= 2:
            curr = float(hist[0]['stck_clpr'])
            prev = float(hist[1]['stck_clpr'])
            chg = ((curr - prev) / prev) * 100 if prev > 0 else 0.0
            return {"price": int(curr), "change_rate": round(chg, 2)}
    except Exception as e_kiwoom:
        print(f"❌ Kiwoom fallback failed for {ticker}: {e_kiwoom}")
            
    # Fallback 2: Toss API
    try:
        from toss_api import get_toss_current_price
        toss_data = get_toss_current_price(ticker)
        if toss_data:
            return toss_data
    except Exception as e_toss:
        print(f"❌ Toss fallback failed for {ticker}: {e_toss}")
            
    # Fallback 3: yfinance (previous close)
    try:
        import yfinance as yf
        suffix = ".KS" if ticker in ["069500", "122630"] else ".KQ"
        df = yf.Ticker(f"{ticker}{suffix}").history(period="5d")
        if not df.empty:
            curr = df['Close'].iloc[-1]
            prev = df['Close'].iloc[-2] if len(df) > 1 else curr
            chg = ((curr - prev) / prev) * 100 if prev > 0 else 0.0
            return {"price": int(curr), "change_rate": round(chg, 2)}
    except Exception as e2:
        print(f"❌ yfinance fallback failed for {ticker}: {e2}")
        
    return None

