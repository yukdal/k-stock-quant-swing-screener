import datetime
from zoneinfo import ZoneInfo
import yfinance as yf
from google import genai
from config import GEMINI_API_KEY, GEMINI_MODEL
from notifier import send_telegram_message

# 종목 메타데이터
#   market : "KR"(실시간 4중 폴백 조회) / "US"(yfinance 종가 조회)
#   unit   : "pt"(지수, 포인트 표기) / "usd"(ETF, 달러 표기)
#   slug   : 인포그래픽 템플릿의 CSS 클래스 및 변수 접두어
INDICES = {
    "KOSPI":      {"ticker": "^KS11", "market": "KR", "unit": "pt",  "flag": "🇰🇷", "slug": "kospi"},
    "KOSDAQ":     {"ticker": "^KQ11", "market": "KR", "unit": "pt",  "flag": "🇰🇷", "slug": "kosdaq"},
    "S&P 500":    {"ticker": "^GSPC", "market": "US", "unit": "pt",  "flag": "🇺🇸", "slug": "sp500"},
    "NASDAQ 100": {"ticker": "^NDX",  "market": "US", "unit": "pt",  "flag": "🇺🇸", "slug": "nasdaq100"},
    "TQQQ":       {"ticker": "TQQQ",  "market": "US", "unit": "usd", "flag": "🇺🇸", "slug": "tqqq"},
    "QLD":        {"ticker": "QLD",   "market": "US", "unit": "usd", "flag": "🇺🇸", "slug": "qld"},
}

# 레버리지 ETF 분할매수 알림 기준 (전고점 대비 낙폭 %)
#   TQQQ : -10%에서 1차 매수, 이후 1%마다 (-11% 2차, -12% 3차 ...)
#   QLD  : -10%에서 1차 매수, 이후 5%마다 (-15% 2차, -20% 3차 ...)
LEVERAGE_ETF_RULES = {
    "TQQQ": {"start": 10, "step": 1},
    "QLD":  {"start": 10, "step": 5},
}

# 국내 지수 급락 경보 / 분할매수 알림 기준 (전고점 대비 낙폭 %)
#   KOSPI  : -15%에서 1차 매수, 이후 5%마다 (-20% 2차, -25% 3차 ...)
#   KOSDAQ : -20%에서 1차 매수, 이후 5%마다 (-25% 2차, -30% 3차 ...)
# 코스닥은 코스피보다 낙폭이 통상 1.3~1.5배 크므로 진입 시작점을 더 높게 잡는다.
INDEX_CRASH_RULES = {
    "KOSPI":  {"start": 15, "step": 5},
    "KOSDAQ": {"start": 20, "step": 5},
}

# 전고점(기간 내 최고가) 탐색에 사용할 국내 지수 일봉 조회 기간 (달력 기준 약 5년)
KR_HISTORY_DAYS = 1825

US_EASTERN = ZoneInfo("America/New_York")
US_MARKET_OPEN = datetime.time(9, 30)
US_MARKET_CLOSE = datetime.time(16, 0)

# 급락 경보 / 레버리지 ETF 알림 상태.
# 값은 '이미 알린 최고 단계의 낙폭(%)'이며, 날짜로 초기화하지 않는다.
# 날짜마다 0으로 되돌리면 하락 구간에 머무는 동안 같은 단계 알림이 매일 반복되므로,
# 전고점을 회복(낙폭 0%)했을 때만 0으로 되돌려 다음 하락에 다시 울리게 한다.
# ⚠️ 프로세스를 재시작하면 메모리 상태가 0이 되어 현재 단계 알림이 1회 재발송된다.
CRASH_STATE = {
    "KOSPI": 0,
    "KOSDAQ": 0
}

SIDECAR_STATE = {
    "date": None,
    "KOSPI": False,
    "KOSDAQ": False
}

ETF_ALERT_STATE = {
    "TQQQ": 0,
    "QLD": 0,
}


def get_index_meta(name):
    """종목 메타데이터 조회. 미등록 종목도 안전하게 기본값을 돌려준다."""
    meta = INDICES.get(name)
    if meta:
        return meta
    return {
        "ticker": None,
        "market": "KR" if name in ("KOSPI", "KOSDAQ") else "US",
        "unit": "pt",
        "flag": "",
        "slug": name.lower().replace("&", "").replace(" ", ""),
    }


def is_us_market_open(now_et=None):
    """
    미국 정규장 개장 여부. 서머타임(EDT/EST)은 ZoneInfo가 자동 처리하므로
    KST 고정 시각(23:30~06:00)이 아닌 ET 기준으로 판정한다.
    주말 판정도 KST가 아닌 ET 날짜 기준이어야 한다 (금요일 밤 미국장 = KST 토요일 새벽).
    """
    now_et = now_et or datetime.datetime.now(US_EASTERN)
    if now_et.weekday() >= 5:
        return False
    return US_MARKET_OPEN <= now_et.time() <= US_MARKET_CLOSE


def find_peak_high(highs_series, current_price=None):
    """
    조회 기간(약 5년) 일봉 고가 중 '최고가'를 전고점으로 정의합니다.

    HTS 지수차트에 표시되는 '최고 9,385.59' 같은 값과 동일한 기준이며,
    전고점 대비 낙폭은 이 최고가를 기준으로 계산해야 실제 하락률과 일치합니다.
    (이전 방식은 앞뒤 15일 윈도우의 '직전 스윙 하이'를 찾았는데,
     고점을 찍고 내려온 뒤 소폭 반등하면 그 낮은 봉우리를 전고점으로 잡아
     낙폭이 실제보다 크게 축소되는 문제가 있었습니다.)

    current_price를 넘기면 신고가를 갱신 중인 날에도 낙폭이 양수가 되지 않도록
    현재가를 포함해 최고가를 계산합니다.
    """
    peak = None
    if highs_series is not None and len(highs_series) > 0:
        try:
            candidate = float(highs_series.max())
            if candidate == candidate:  # NaN 방지
                peak = candidate
        except (TypeError, ValueError):
            peak = None

    if current_price is not None:
        try:
            current_price = float(current_price)
            peak = current_price if peak is None else max(peak, current_price)
        except (TypeError, ValueError):
            pass

    return peak


def calc_local_high_pct(current_price, highs_series):
    """
    전고점(기간 내 최고가) 대비 낙폭(%)을 반환합니다.
    - 하락 중이면 음수 (예: 6808.21 / 전고점 9385.59 → -27.46)
    - 신고가이거나 데이터가 없으면 0.0
    """
    peak = find_peak_high(highs_series, current_price)
    if not peak or peak <= 0 or current_price is None:
        return 0.0
    pct = (float(current_price) - peak) / peak * 100
    return min(pct, 0.0)

def fetch_realtime_index_multi_api(name):
    """
    KOSPI/KOSDAQ 실시간 지수 조회를 KIS -> Kiwoom -> Toss -> Naver 순으로 시도합니다.
    """
    ticker_code = "0001" if name == "KOSPI" else "1001"
    
    # 1. KIS API
    try:
        from kis_api import get_kis_current_index
        kis_data = get_kis_current_index(ticker_code)
        if kis_data: return kis_data
    except Exception as e:
        print(f"❌ KIS API Index fallback failed for {name}: {e}")
        
    # 2. Kiwoom API
    try:
        from kiwoom_api import get_kiwoom_current_index
        kiwoom_data = get_kiwoom_current_index(name)
        if kiwoom_data: return kiwoom_data
    except Exception as e:
        print(f"❌ Kiwoom API Index fallback failed for {name}: {e}")
        
    # 3. Toss API
    try:
        from toss_api import get_toss_current_index
        toss_data = get_toss_current_index(name)
        if toss_data: return toss_data
    except Exception as e:
        print(f"❌ Toss API Index fallback failed for {name}: {e}")
        
    # 4. Naver Finance (최후 보루)
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{name}"
        import requests
        r = requests.get(url, timeout=10)
        data = r.json()['datas'][0]
        return {
            "current_close": float(data['closePrice'].replace(',', '')),
            "point_change": float(data['compareToPreviousClosePrice'].replace(',', '')),
            "pct_change": float(data['fluctuationsRatio'].replace(',', ''))
        }
    except Exception as e:
        print(f"❌ Naver Finance Index fallback failed for {name}: {e}")
        return None

def fetch_kr_index_high_series(name, ticker=None):
    """
    국내 지수(KOSPI/KOSDAQ)의 전고점 계산용 일봉 고가 시리즈를 반환합니다.

    1순위: KRX(pykrx) — 한국거래소 공식 데이터
    2순위: yfinance   — KRX 조회 실패 시에만 사용하는 폴백

    두 소스 모두 실패하면 None을 반환하며, 호출부는 전고점 대비를 0%로 처리합니다.
    """
    # 1. KRX (한국거래소)
    try:
        from krx_api import fetch_krx_index_ohlcv
        df = fetch_krx_index_ohlcv(name, days=KR_HISTORY_DAYS)
        if df is not None and len(df) > 0 and "고가" in df.columns:
            series = df["고가"].dropna()
            if len(series) > 0:
                print(f"📗 {name} 전고점 데이터 소스: KRX (pykrx), {len(series)}일치")
                return series
    except Exception as e:
        print(f"⚠️ KRX index history failed for {name}: {e}")

    # 2. yfinance 폴백
    ticker = ticker or get_index_meta(name)["ticker"]
    if not ticker:
        return None
    try:
        max_df = yf.Ticker(ticker).history(period="5y").dropna(subset=['High'])
        if len(max_df) > 0:
            print(f"📙 {name} 전고점 데이터 소스: yfinance (KRX 조회 실패로 폴백), {len(max_df)}일치")
            return max_df['High']
    except Exception as e:
        print(f"❌ yfinance history fallback failed for {name}: {e}")

    print(f"❌ {name} 전고점 일봉 데이터를 어떤 소스에서도 가져오지 못했습니다.")
    return None


def fetch_index_data(name, ticker=None):
    """
    특정 지수/ETF의 당일(또는 미국 전일) 종가 및 전고점(기간 내 최고가) 대비 낙폭을 계산합니다.
    ticker를 생략하면 INDICES 메타데이터에서 자동으로 조회합니다.
    """
    meta = get_index_meta(name)
    ticker = ticker or meta["ticker"]
    if not ticker:
        print(f"❌ No ticker registered for {name}.")
        return None

    market = meta["market"]
    unit = meta["unit"]

    try:
        as_of = None

        if market == "KR":
            # 국내 지수는 야후 파이낸스를 쓰지 않는다.
            #   당일 종가/등락 : KIS → 키움 → 토스 → 네이버 4중 폴백
            #   전고점 일봉    : KRX(pykrx) → (실패 시에만) 야후 폴백
            realtime_data = fetch_realtime_index_multi_api(name)
            if not realtime_data:
                raise Exception(f"All APIs failed to fetch {name} real-time data.")
            current_close = realtime_data['current_close']
            point_change = realtime_data['point_change']
            pct_change = realtime_data['pct_change']

            high_series = fetch_kr_index_high_series(name, ticker)
        else:
            # 미국 종목(S&P 500 / NASDAQ 100 / TQQQ / QLD)은 yfinance 종가를 사용한다.
            # KST 15:45 시점에는 미국장이 이미 마감된 상태이므로 '전일(미국 현지) 종가'가 잡힌다.
            idx = yf.Ticker(ticker)
            recent_df = idx.history(period="5d").dropna(subset=['Close'])
            if len(recent_df) < 2:
                return None
            current_close = recent_df['Close'].iloc[-1]
            prev_close = recent_df['Close'].iloc[-2]
            point_change = current_close - prev_close
            pct_change = (point_change / prev_close) * 100 if prev_close > 0 else 0.0
            try:
                as_of = recent_df.index[-1].date()
            except Exception:
                as_of = None

            # 52주 제한 없이 넉넉하게 5년치에서 최고가 탐색
            max_df = idx.history(period="5y").dropna(subset=['High'])
            high_series = max_df['High'] if len(max_df) > 0 else None

        # 전고점(기간 내 최고가) 대비 낙폭 분석
        local_high_pct = calc_local_high_pct(current_close, high_series)

        return {
            "current_close": current_close,
            "point_change": point_change,
            "pct_change": pct_change,
            "local_high_pct": local_high_pct,
            "unit": unit,
            "market": market,
            "as_of": as_of
        }
    except Exception as e:
        print(f"❌ Error fetching index data for {name} ({ticker}): {e}")
        return None


def fetch_us_realtime_quote(name):
    """
    미국 장중 실시간 시세 조회 (yfinance fast_info → 1분봉 폴백).
    전고점은 fetch_index_data와 동일하게 5년 일봉 최고가 기준으로 계산합니다.
    """
    meta = get_index_meta(name)
    ticker = meta["ticker"]
    if not ticker:
        return None

    try:
        t = yf.Ticker(ticker)
        price = None
        prev_close = None

        # 1. fast_info (실시간 최종 체결가)
        try:
            fi = t.fast_info
            price = float(fi["last_price"])
            prev_close = float(fi["previous_close"])
        except Exception as e:
            print(f"⚠️ fast_info unavailable for {name}: {e}")

        # 2. 1분봉 폴백
        if not price:
            intraday = t.history(period="1d", interval="1m").dropna(subset=['Close'])
            if len(intraday) > 0:
                price = float(intraday['Close'].iloc[-1])

        if not prev_close:
            daily = t.history(period="5d").dropna(subset=['Close'])
            if len(daily) >= 2:
                prev_close = float(daily['Close'].iloc[-2])

        if not price:
            print(f"❌ Failed to fetch real-time quote for {name} ({ticker}).")
            return None

        point_change = (price - prev_close) if prev_close else 0.0
        pct_change = (point_change / prev_close * 100) if prev_close else 0.0

        max_df = t.history(period="5y").dropna(subset=['High'])
        local_high_pct = calc_local_high_pct(price, max_df['High'] if len(max_df) > 0 else None)

        return {
            "current_close": price,
            "point_change": point_change,
            "pct_change": pct_change,
            "local_high_pct": local_high_pct,
            "unit": meta["unit"],
            "market": "US",
            "as_of": None
        }
    except Exception as e:
        print(f"❌ Error fetching US real-time quote for {name}: {e}")
        return None

def generate_index_macro_comment():
    """Gemini를 이용한 매크로 한줄평 생성"""
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        return "현재 시장의 변동성이 지속되고 있으며, 주요 지지선 및 저항선 부근에서의 리스크 관리가 필요한 국면입니다. (Mock Data)"
        
    import time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            prompt = (
                "당신은 20년 경력의 증권사 수석 매크로 애널리스트입니다. "
                "오늘 한국(KOSPI/KOSDAQ)과 미국(S&P 500/NASDAQ 100) 시장 마감 직후의 시장 국면, "
                "그리고 나스닥 레버리지 ETF(TQQQ/QLD)의 변동성 리스크에 대한 핵심 통찰을 "
                "딱 1줄의 숏 코멘트로 요약해서 작성해주세요."
            )
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return response.text.strip()
        except Exception as e:
            err_str = str(e)
            print(f"❌ Gemini Macro Comment Error (Attempt {attempt+1}/{max_retries}): {err_str}")
            if "503" in err_str or "429" in err_str:
                time.sleep(2)
                continue
            return f"시장 데이터 분석 중 일시적인 지연이 발생했습니다. (오류: {err_str[:100]})"
            
    return "시장 데이터 분석 중 일시적인 지연이 발생했습니다. (Gemini 서버 혼잡으로 인한 503 오류)"

def format_number(val, is_pct=False):
    """지수 소수점 및 기호 포맷팅"""
    if val is None: return "N/A"

    # 등락률, 포인트 모두 소수점 둘째자리 통일 적용
    sign = "▲" if val > 0 else ("▼" if val < 0 else "")
    plus = "+" if val > 0 else ""

    if is_pct:
        return f"{plus}{val:.2f}%"
    else:
        return f"{sign}{abs(val):.2f}"


def format_price(val, unit="pt"):
    """종가 표기. ETF(usd)는 달러 기호를 붙인다."""
    if val is None: return "N/A"
    return f"${val:,.2f}" if unit == "usd" else f"{val:,.2f}"


def format_change_block(data):
    """
    전일대비 등락 표기.
      지수(pt) : '▲12.34pt, +0.45%'
      ETF(usd) : '▲$1.20, +1.32%'
    """
    unit = data.get("unit", "pt")
    pt = data["point_change"]
    sign = "▲" if pt > 0 else ("▼" if pt < 0 else "")

    if unit == "usd":
        change = f"{sign}${abs(pt):.2f}"
    else:
        change = f"{sign}{abs(pt):.2f}pt"

    return f"{change}, {format_number(data['pct_change'], is_pct=True)}"

def execute_index_closing(*args, **kwargs):
    """오후 3시 45분에 단독 실행되는 메인 파이프라인"""
    today = datetime.date.today()
    if today.weekday() >= 5:
        print("📅 주말에는 지수 정산 모듈이 실행되지 않습니다.")
        return
        
    print(f"\n📈 [{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Index Closing Settlement...")
    
    try:
        # 1. 전 종목 데이터 수집
        indices_data = {}
        for name, meta in INDICES.items():
            indices_data[name] = fetch_index_data(name, meta["ticker"])

        def build_table(names, close_header):
            """국내/미국 섹션별 마크다운 표 생성"""
            lines = [
                f"| 종목명 | {close_header} | 직전 전고점 대비 |",
                "| :--- | :--- | :---: |"
            ]
            for name in names:
                data = indices_data.get(name)
                if data:
                    c_close = format_price(data['current_close'], data.get('unit', 'pt'))
                    change = format_change_block(data)
                    lh_chg = format_number(data['local_high_pct'], is_pct=True)
                    lines.append(f"| **{name}** | {c_close} ({change}) | {lh_chg} |")
                else:
                    lines.append(f"| **{name}** | 데이터 수집 지연 | - |")
            return lines

        kr_names = [n for n, m in INDICES.items() if m["market"] == "KR"]
        us_names = [n for n, m in INDICES.items() if m["market"] == "US"]

        # 미국 데이터의 실제 기준일(전일 종가) 표기
        us_as_of = next(
            (indices_data[n]['as_of'] for n in us_names
             if indices_data.get(n) and indices_data[n].get('as_of')),
            None
        )
        us_as_of_str = f"{us_as_of.month}/{us_as_of.day} 종가 기준" if us_as_of else "직전 거래일 종가 기준"

        report_lines = []
        report_lines.append("📊 [오후 3시 45분 국내외 주요 지수 마감 정산 (v5.0)]")
        report_lines.append("오늘 정규장 마감 직후 집계된 주요 지수의 위치와 변동성 데이터입니다. (데이터 교차검증 완료)\n")

        report_lines.append("🇰🇷 **국내 지수**")
        report_lines.extend(build_table(kr_names, "당일 종가 (전일대비)"))

        report_lines.append(f"\n🇺🇸 **미국 지수 · ETF** ({us_as_of_str})")
        report_lines.append("_미국장은 한국시간 새벽에 마감되므로 직전 거래일 종가입니다._")
        report_lines.extend(build_table(us_names, "종가 (전일대비)"))

        comment = generate_index_macro_comment()
        report_lines.append("\n💡 **매크로 한줄평 (Gemini Pro 분석)**")
        report_lines.append(f"- {comment}")
        report_lines.append("--------------------------------------")
        
        final_report = "\n".join(report_lines)
        
        # 텔레그램 발송 (기존 텍스트 알림)
        send_telegram_message(final_report)
        print("🎉 Index closing settlement text dispatched successfully!")
        
        # 인포그래픽 위젯 렌더링 및 텔레그램 이미지 발송 (병렬 추가)
        from infographic_generator import generate_and_send_infographic
        generate_and_send_infographic(indices_data, comment)

    except Exception as e:
        print(f"❌ Error occurred during index settlement execution: {e}")

def check_and_send_crash_alerts():
    """
    장중 15분 단위로 KOSPI/KOSDAQ의 전고점 대비 낙폭을 체크하여
    분할매수 단계 진입 시 알림을 발송합니다. (기준: INDEX_CRASH_RULES)
    """
    global CRASH_STATE

    print(f"🔍 [{datetime.datetime.now().strftime('%H:%M:%S')}] Checking Index Crash Alerts...")

    for name, rule in INDEX_CRASH_RULES.items():
        data = fetch_index_data(name, INDICES[name]["ticker"])
        if not data:
            continue

        lh_pct = data["local_high_pct"]

        # 전고점을 회복하면 상태를 초기화해, 다시 하락 돌파할 때 알림이 울리도록 한다.
        if lh_pct >= 0:
            CRASH_STATE[name] = 0
            continue

        drop_pct = abs(lh_pct)
        tier, level = calc_buy_tier(drop_pct, rule["start"], rule["step"])

        # 기준 미달이거나 이미 알린 단계 이하이면 조용히 넘어간다.
        # (낙폭이 얕아져도 상태를 낮추지 않으므로 같은 단계가 반복 발송되지 않는다)
        if tier <= 0 or level <= CRASH_STATE[name]:
            continue

        CRASH_STATE[name] = level

        c_close = f"{data['current_close']:,.2f}"
        pt_chg = format_number(data['point_change'], is_pct=False)
        pct_chg = format_number(data['pct_change'], is_pct=True)
        actual_drop_pct = f"{lh_pct:.2f}"

        from kis_api import get_etf_current_price

        if name == "KOSPI":
            etfs = [
                ("KODEX 200", "069500"),
                ("KODEX 레버리지", "122630")
            ]
        else:
            etfs = [
                ("KODEX 코스닥150", "229200"),
                ("KODEX 코스닥150레버리지", "233740")
            ]

        etf_messages = []
        for etf_name, etf_code in etfs:
            etf_data = get_etf_current_price(etf_code)
            if etf_data:
                price_str = f"{etf_data['price']:,}"
                chg = etf_data['change_rate']
                sign = "+" if chg > 0 else ""
                etf_messages.append(f"{etf_name} ({etf_code}): {price_str}원 ({sign}{chg:.2f}%)")
            else:
                etf_messages.append(f"{etf_name} ({etf_code}): 데이터 응답 지연")

        msg = f"🚨 시장 급락 경보 {name} 전고점 대비 -{level}% 돌파! <b>[{tier}차 매수 추천]</b> 🚨\n\n"
        msg += f"현재 지수가 전고점 대비 하락 구간에 진입했습니다.\n"
        msg += f"■ 현재 {name} 지수: {c_close} ({pt_chg}pt, {pct_chg})\n"
        msg += f"<b>■ 전고점 대비 하락률: {actual_drop_pct}%</b>\n\n"
        msg += f"※ 매수 기준: -{rule['start']}% 1차 진입 후 {rule['step']}% 하락마다 추가 매수\n"
        msg += "투심 악화 및 반대매매 물량 출회 가능성에 유의하시되, 룰 베이스 분할 매수 전략에 의거해 대응하시길 바랍니다.\n\n"
        msg += "💡 연동 상품 실시간 현재가:\n\n"
        msg += "\n\n".join(etf_messages)

        send_telegram_message(msg, parse_mode="HTML")
        print(f"🚨 Crash alert sent for {name}: -{level}% ({tier}차)")

def check_and_send_sidecar_alerts():
    """장중 90초 단위로 코스피 ±5%, 코스닥 ±6% 등락 여부를 체크하여 사이드카 자체 감지 알림 발송"""
    global SIDECAR_STATE
    today = datetime.date.today()
    
    # 매일 자정 상태 초기화
    if SIDECAR_STATE["date"] != today:
        SIDECAR_STATE["date"] = today
        SIDECAR_STATE["KOSPI"] = False
        SIDECAR_STATE["KOSDAQ"] = False

    print(f"🔍 [{datetime.datetime.now().strftime('%H:%M:%S')}] Checking Sidecar Alerts...")
    
    for name in ["KOSPI", "KOSDAQ"]:
        if SIDECAR_STATE[name]:
            continue # 당일 이미 발동되었으면 스킵

        data = fetch_index_data(name, INDICES[name]["ticker"])
        if not data: continue
        
        pct_change = data["pct_change"]
        threshold = 5.0 if name == "KOSPI" else 6.0
        
        if abs(pct_change) >= threshold:
            # 자체 사이드카 발동 조건 충족
            SIDECAR_STATE[name] = True
            
            c_close = f"{data['current_close']:,.2f}"
            pct_chg_str = format_number(pct_change, is_pct=True)
            current_time = datetime.datetime.now().strftime('%H:%M')
            
            sidecar_type = "매수" if pct_change > 0 else "매도"
            
            msg = f"🚨 {name} {sidecar_type} 사이드카 발동! (발동시간: {current_time}) 현재 지수: {c_close} ({pct_chg_str})"
            
            send_telegram_message(msg)
            print(f"🚨 Sidecar alert sent for {name}: {pct_change}%")

def calc_buy_tier(drop_pct, start, step):
    """
    전고점 대비 낙폭(양수 %)에 해당하는 분할매수 단계와 기준 낙폭을 반환합니다.
    국내 지수(INDEX_CRASH_RULES)와 레버리지 ETF(LEVERAGE_ETF_RULES)가 함께 사용합니다.
    기준 미달이면 (0, 0).

    예) TQQQ (start=10, step=1): 10.0% → (1차, 10), 11.5% → (2차, 11), 12.0% → (3차, 12)
        QLD  (start=10, step=5): 10.0% → (1차, 10), 15.2% → (2차, 15), 20.0% → (3차, 20)
        KOSPI(start=15, step=5): 14.9% → (0, 0),   15.0% → (1차, 15), 27.5% → (3차, 25)
    """
    if drop_pct < start:
        return 0, 0
    level = start + int((drop_pct - start) // step) * step
    tier = int((level - start) // step) + 1
    return tier, level


def check_and_send_leverage_etf_alerts():
    """
    미국 정규장 중 30분 단위로 TQQQ/QLD의 전고점 대비 낙폭을 체크하여
    분할매수 단계 진입 시 알림을 발송합니다. (기준: LEVERAGE_ETF_RULES)
    """
    global ETF_ALERT_STATE

    if not is_us_market_open():
        return

    now_et = datetime.datetime.now(US_EASTERN)
    print(f"🔍 [{now_et.strftime('%H:%M:%S')} ET] Checking Leverage ETF Buy Alerts...")

    for name, rule in LEVERAGE_ETF_RULES.items():
        quote = fetch_us_realtime_quote(name)
        if not quote: continue

        lh_pct = quote["local_high_pct"]

        # 전고점 회복 구간이면 상태를 초기화해, 다시 하락 돌파할 때 알림이 울리도록 한다.
        if lh_pct >= 0:
            ETF_ALERT_STATE[name] = 0
            continue

        drop_pct = abs(lh_pct)
        tier, level = calc_buy_tier(drop_pct, rule["start"], rule["step"])

        # 기준 미달이거나 이미 알린 단계 이하이면 조용히 넘어간다.
        # (낙폭이 얕아져도 상태를 낮추지 않으므로 같은 단계가 반복 발송되지 않는다)
        if tier <= 0 or level <= ETF_ALERT_STATE[name]:
            continue

        ETF_ALERT_STATE[name] = level

        price_str = format_price(quote['current_close'], quote.get('unit', 'usd'))
        change_str = format_change_block(quote)
        actual_drop = f"{lh_pct:.2f}"
        kst_time = datetime.datetime.now(ZoneInfo("Asia/Seoul")).strftime('%m/%d %H:%M')

        msg = f"🚨 {name} 전고점 대비 -{level}% 돌파! <b>[{tier}차 매수 추천]</b> 🚨\n\n"
        msg += f"나스닥100 레버리지 ETF {name}가 전고점 대비 하락 구간에 진입했습니다.\n"
        msg += f"■ 현재가: {price_str} ({change_str})\n"
        msg += f"<b>■ 전고점 대비 하락률: {actual_drop}%</b>\n"
        msg += f"■ 감지 시각: {kst_time} KST (미국 정규장 중)\n\n"
        msg += f"※ 매수 기준: -{rule['start']}% 1차 진입 후 {rule['step']}% 하락마다 추가 매수\n"
        msg += "레버리지 ETF는 변동성 잠식 리스크가 있으므로 룰 베이스 분할 매수 원칙을 지켜 대응하시기 바랍니다."

        send_telegram_message(msg, parse_mode="HTML")
        print(f"🚨 Leverage ETF buy alert sent for {name}: -{level}% ({tier}차)")


if __name__ == "__main__":
    execute_index_closing()
