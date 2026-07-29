# =============================================================
# krx_api.py : KRX(한국거래소) 데이터 조회 모듈 (pykrx 라이브러리 사용)
# - KIS(한국투자증권), 키움 API가 모두 실패했을 때 사용하는 "최후의 예비 데이터 소스"
# - 일봉(OHLCV)은 로그인 없이 무료로 조회 가능
# - 투자자 수급/지수 데이터는 KRX 정보데이터시스템(data.krx.co.kr) 계정 필요
#   → .env 파일에 KRX_ID, KRX_PW 를 추가하면 자동으로 사용됨
# =============================================================

import datetime                      # 날짜 계산(오늘, N일 전)을 위한 표준 라이브러리
import config                        # ★중요: pykrx보다 먼저 import 해야 .env의 KRX_ID/KRX_PW가 환경변수로 로드됨
from pykrx import stock              # KRX 데이터를 가져오는 핵심 라이브러리 (pip install pykrx)


def fetch_krx_daily_price(ticker, days=1800):
    """
    KRX에서 일봉(일별 시세) 데이터를 가져옵니다. (로그인 불필요)
    반환 형식은 KIS API와 동일하게 맞춰서(quant_filter.py가 그대로 쓸 수 있게)
    '최신 날짜가 리스트 맨 앞'에 오는 dict 리스트로 반환합니다.
    예: [{"stck_bsop_date": "20260710", "stck_clpr": "278000", "stck_hgpr": "291500", "acml_vol": "28796159"}, ...]
    """
    try:
        today = datetime.date.today()                                    # 오늘 날짜를 구함
        start = (today - datetime.timedelta(days=days)).strftime("%Y%m%d")  # 약 1800일(달력 기준) 전 날짜 → 거래일 약 1200일 확보
        end = today.strftime("%Y%m%d")                                   # 조회 종료일 = 오늘

        # KRX에서 시가/고가/저가/종가/거래량 데이터를 DataFrame으로 받아옴 (날짜 오름차순: 과거 → 최신)
        df = stock.get_market_ohlcv(start, end, ticker)

        if df is None or df.empty:                                       # 데이터가 없으면(상장폐지, 잘못된 티커 등)
            return []                                                    # 빈 리스트를 돌려줘서 호출한 쪽이 "실패"로 처리하게 함

        result = []                                                      # 변환된 데이터를 담을 빈 리스트
        # KIS API는 "최신 날짜가 맨 앞"이므로, KRX 데이터(과거→최신)를 거꾸로 순회함
        for date_idx, row in df.iloc[::-1].iterrows():
            result.append({
                "stck_bsop_date": date_idx.strftime("%Y%m%d"),           # 거래일자 (예: "20260710")
                "stck_clpr": str(int(row["종가"])),                       # 종가 → KIS와 같은 키 이름/문자열 형태로 변환
                "stck_hgpr": str(int(row["고가"])),                       # 고가 → 위꼬리(장중 급등 후 하락) 판별에 사용됨
                "acml_vol": str(int(row["거래량"])),                      # 누적 거래량
            })
        return result                                                    # 완성된 리스트 반환
    except Exception as e:                                               # 네트워크 오류 등 어떤 예외가 나도
        print(f"❌ KRX daily price error for {ticker}: {e}")             # 에러 내용을 출력하고
        return []                                                        # 봇이 죽지 않도록 빈 리스트 반환


def fetch_krx_investor_trend(ticker):
    """
    KRX에서 최근 거래일의 투자자별(외국인/기관/개인) 순매수 '수량'을 가져옵니다.
    ⚠️ 이 기능은 KRX 정보데이터시스템 로그인(.env의 KRX_ID/KRX_PW)이 필요합니다.
    반환 형식은 KIS API(fetch_investor_trend)와 동일한 키 이름을 사용합니다.
    예: {"frgn_ntby_qty": "12345", "orgn_ntby_qty": "-500", "prsn_ntby_qty": "300"}
    """
    try:
        today = datetime.date.today()                                    # 오늘 날짜
        start = (today - datetime.timedelta(days=14)).strftime("%Y%m%d") # 휴장일을 감안해 넉넉히 2주 전부터 조회
        end = today.strftime("%Y%m%d")                                   # 조회 종료일 = 오늘

        # 투자자별 '순매수 거래량'을 날짜별로 조회 (컬럼 예: 기관합계, 기타법인, 개인, 외국인합계)
        df = stock.get_market_trading_volume_by_date(start, end, ticker)

        if df is None or df.empty:                                       # 로그인 실패 또는 데이터 없음
            return None                                                  # None을 돌려주면 수급 데이터 없이(0으로) 진행됨

        latest = df.iloc[-1]                                             # 가장 최근 거래일의 데이터 한 줄 선택

        def safe_get(row, col):                                          # 컬럼이 없어도 에러 나지 않게 하는 도우미 함수
            try:
                return str(int(row[col]))                                # 숫자를 KIS와 같은 "문자열" 형태로 변환
            except Exception:
                return "0"                                               # 해당 컬럼이 없으면 0으로 처리

        return {
            "frgn_ntby_qty": safe_get(latest, "외국인합계"),              # 외국인 순매수 수량
            "orgn_ntby_qty": safe_get(latest, "기관합계"),                # 기관 순매수 수량
            "prsn_ntby_qty": safe_get(latest, "개인"),                    # 개인 순매수 수량
        }
    except Exception as e:                                               # 로그인 미설정/네트워크 오류 등
        print(f"⚠️ KRX investor trend unavailable for {ticker}: {e} (KRX_ID/KRX_PW 미설정 시 정상)")
        return None                                                      # 실패해도 봇이 멈추지 않도록 None 반환


# KRX 지수 코드 (get_index_ohlcv 조회용)
KRX_INDEX_CODES = {
    "KOSPI": "1001",
    "KOSDAQ": "2001",
}


def fetch_krx_index_ohlcv(market_type="KOSPI", days=1825):
    """
    KRX에서 지수(KOSPI/KOSDAQ)의 일봉 OHLCV를 DataFrame으로 조회합니다.
    전고점(스윙 하이) 계산용이므로 기본 5년치(1825일)를 가져옵니다.

    pykrx는 KRX 로그인 세션이 없으면 일반 요청으로 조회를 시도하므로
    (website/comm/webio.py 참고) KRX_ID/KRX_PW 없이도 동작할 수 있습니다.
    조회에 실패하면 None을 반환하여 호출부가 다른 소스로 폴백하게 합니다.

    반환: '고가', '종가' 등의 컬럼을 가진 DataFrame (날짜 오름차순) 또는 None
    """
    try:
        index_code = KRX_INDEX_CODES.get(market_type.upper())
        if not index_code:
            print(f"⚠️ KRX 지수 코드가 등록되지 않은 종목입니다: {market_type}")
            return None

        today = datetime.date.today()
        start = (today - datetime.timedelta(days=days)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")

        df = stock.get_index_ohlcv(start, end, index_code)

        if df is None or df.empty:
            print(f"⚠️ KRX index OHLCV empty for {market_type}.")
            return None

        return df
    except Exception as e:
        # KRX 접속 실패/로그인 요구 등 어떤 예외가 나도 호출부가 폴백할 수 있게 None 반환
        print(f"⚠️ KRX index OHLCV unavailable for {market_type}: {e}")
        return None


def get_krx_current_index(market_type="KOSPI"):
    """
    KRX에서 지수(KOSPI/KOSDAQ)의 최근 종가와 전일 대비 등락을 계산해서 반환합니다.
    ⚠️ 이 기능도 KRX 로그인(.env의 KRX_ID/KRX_PW)이 필요합니다.
    반환 형식은 kis_api.get_kis_current_index와 동일합니다.
    예: {"current_close": 2750.12, "point_change": 15.2, "pct_change": 0.55}
    """
    try:
        # KRX 지수 코드: KOSPI는 "1001", KOSDAQ은 "2001"
        index_code = "1001" if market_type.upper() == "KOSPI" else "2001"

        today = datetime.date.today()                                    # 오늘 날짜
        start = (today - datetime.timedelta(days=14)).strftime("%Y%m%d") # 휴장일 감안 2주 전부터
        end = today.strftime("%Y%m%d")                                   # 오늘까지

        df = stock.get_index_ohlcv(start, end, index_code)               # 지수 일봉 데이터 조회

        if df is None or len(df) < 2:                                    # 최소 2일치(오늘+전일)가 있어야 등락 계산 가능
            return None

        current_close = float(df["종가"].iloc[-1])                       # 가장 최근 거래일 종가
        prev_close = float(df["종가"].iloc[-2])                          # 그 전 거래일 종가
        point_change = current_close - prev_close                        # 전일 대비 포인트 증감
        pct_change = (point_change / prev_close * 100) if prev_close > 0 else 0.0  # 전일 대비 등락률(%)

        return {
            "current_close": current_close,                              # 현재(최근) 지수
            "point_change": round(point_change, 2),                      # 포인트 증감 (소수 2자리)
            "pct_change": round(pct_change, 2),                          # 등락률 % (소수 2자리)
        }
    except Exception as e:                                               # 로그인 미설정 등 실패 시
        print(f"⚠️ KRX index unavailable for {market_type}: {e} (KRX_ID/KRX_PW 미설정 시 정상)")
        return None                                                      # None 반환 → 다른 소스(네이버 등)로 폴백


# 이 파일을 단독으로 실행하면(python krx_api.py) 간단한 자체 테스트를 수행합니다.
if __name__ == "__main__":
    print("=== KRX API 자체 테스트 ===")
    print("[1] 삼성전자(005930) 일봉 조회 테스트 (로그인 불필요)...")
    hist = fetch_krx_daily_price("005930")                               # 삼성전자 일봉 데이터 요청
    if hist:
        print(f"  ✅ {len(hist)}일치 수신. 최신 데이터: {hist[0]}")        # 성공 시 개수와 최신 데이터 출력
    else:
        print("  ❌ 일봉 조회 실패")

    print("[2] 삼성전자 투자자 수급 조회 테스트 (KRX 로그인 필요)...")
    inv = fetch_krx_investor_trend("005930")                             # 수급 데이터 요청
    print(f"  결과: {inv}")                                              # None이면 로그인 미설정 상태

    print("[3] KOSPI 지수 조회 테스트 (KRX 로그인 필요)...")
    idx = get_krx_current_index("KOSPI")                                 # 지수 데이터 요청
    print(f"  결과: {idx}")                                              # None이면 로그인 미설정 상태
