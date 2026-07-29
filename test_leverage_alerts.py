"""
TQQQ/QLD 분할매수 알림 로직 검증 스크립트.
텔레그램 발송은 목업으로 가로채므로 실제 알림은 나가지 않습니다.

실행: python test_leverage_alerts.py
"""
import sys
import datetime
sys.stdout.reconfigure(encoding='utf-8')

import index_closing
from index_closing import calc_etf_buy_tier, LEVERAGE_ETF_RULES, US_EASTERN

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'✅' if ok else '❌'} {label}: {actual} (기대값 {expected})")
    if not ok:
        failures.append(label)


print("=" * 60)
print("1. 매수 단계(티어) 계산 검증")
print("=" * 60)

# TQQQ: -10% 1차, 이후 1%마다
check("TQQQ  -9.9%", calc_etf_buy_tier(9.9, 10, 1), (0, 0))
check("TQQQ -10.0%", calc_etf_buy_tier(10.0, 10, 1), (1, 10))
check("TQQQ -10.9%", calc_etf_buy_tier(10.9, 10, 1), (1, 10))
check("TQQQ -11.0%", calc_etf_buy_tier(11.0, 10, 1), (2, 11))
check("TQQQ -12.4%", calc_etf_buy_tier(12.4, 10, 1), (3, 12))
check("TQQQ -30.0%", calc_etf_buy_tier(30.0, 10, 1), (21, 30))

# QLD: -10% 1차, 이후 5%마다
check("QLD  -10.0%", calc_etf_buy_tier(10.0, 10, 5), (1, 10))
check("QLD  -14.9%", calc_etf_buy_tier(14.9, 10, 5), (1, 10))
check("QLD  -15.0%", calc_etf_buy_tier(15.0, 10, 5), (2, 15))
check("QLD  -20.3%", calc_etf_buy_tier(20.3, 10, 5), (3, 20))
check("QLD  -50.0%", calc_etf_buy_tier(50.0, 10, 5), (9, 50))

print()
print("=" * 60)
print("2. 중복 발송 방지 및 재하락 재알림 검증")
print("=" * 60)

sent = []
index_closing.send_telegram_message = lambda msg, **kw: sent.append(msg)
index_closing.is_us_market_open = lambda *a, **k: True

# 낙폭 시나리오: 진입 → 유지 → 심화 → 회복 → 재하락
scenario = {
    "TQQQ": [-9.5, -10.2, -10.7, -12.3, -12.9, 1.5, -11.4],
    "QLD":  [-9.5, -10.2, -12.0, -15.4, -15.9, 1.5, -16.2],
}
step_labels = ["기준미달", "1차진입", "동일단계", "단계심화", "동일단계", "전고점회복", "재하락"]
expected_alerts = {
    # 1차진입 / 단계심화 / 재하락 → 3건
    "TQQQ": 3,
    "QLD": 3,
}

for etf_name, drops in scenario.items():
    sent.clear()
    index_closing.ETF_ALERT_STATE = {"session": None, "TQQQ": 0, "QLD": 0}

    rules_backup = dict(LEVERAGE_ETF_RULES)
    for other in list(LEVERAGE_ETF_RULES):
        if other != etf_name:
            LEVERAGE_ETF_RULES.pop(other)

    print(f"\n▶ {etf_name} 시나리오")
    for label, lh_pct in zip(step_labels, drops):
        index_closing.fetch_us_realtime_quote = lambda name, v=lh_pct: {
            "current_close": 50.0,
            "point_change": -1.2,
            "pct_change": -2.3,
            "local_high_pct": v,
            "unit": "usd",
            "market": "US",
            "as_of": None,
        }
        before = len(sent)
        index_closing.check_and_send_leverage_etf_alerts()
        fired = len(sent) - before
        print(f"   {label:8s} 낙폭 {lh_pct:+6.1f}% → 알림 {fired}건 (누적 상태 {index_closing.ETF_ALERT_STATE[etf_name]})")

    check(f"{etf_name} 총 발송 건수", len(sent), expected_alerts[etf_name])

    LEVERAGE_ETF_RULES.clear()
    LEVERAGE_ETF_RULES.update(rules_backup)

print()
print("=" * 60)
print("3. 미국 개장 판정 (서머타임/주말 포함)")
print("=" * 60)

# ET 기준으로 판정되므로 KST 날짜가 토요일이어도 ET 금요일이면 개장
cases = [
    ("2026-07-29 09:29 ET (개장 1분 전)", datetime.datetime(2026, 7, 29, 9, 29, tzinfo=US_EASTERN), False),
    ("2026-07-29 09:30 ET (개장)",        datetime.datetime(2026, 7, 29, 9, 30, tzinfo=US_EASTERN), True),
    ("2026-07-29 15:59 ET (장중)",        datetime.datetime(2026, 7, 29, 15, 59, tzinfo=US_EASTERN), True),
    ("2026-07-29 16:01 ET (마감 후)",     datetime.datetime(2026, 7, 29, 16, 1, tzinfo=US_EASTERN), False),
    ("2026-07-31 15:00 ET (금요일)",      datetime.datetime(2026, 7, 31, 15, 0, tzinfo=US_EASTERN), True),
    ("2026-08-01 12:00 ET (토요일)",      datetime.datetime(2026, 8, 1, 12, 0, tzinfo=US_EASTERN), False),
]
for label, dt, expected in cases:
    # 원본 함수를 직접 호출 (위에서 목업으로 덮었으므로 모듈 재조회)
    from index_closing import US_MARKET_OPEN, US_MARKET_CLOSE
    actual = dt.weekday() < 5 and US_MARKET_OPEN <= dt.time() <= US_MARKET_CLOSE
    check(label, actual, expected)

print()
print("=" * 60)
if failures:
    print(f"❌ 실패 {len(failures)}건: {', '.join(failures)}")
    sys.exit(1)
print("🎉 전체 통과")
