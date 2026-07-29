"""
15:45 마감 리포트 및 인포그래픽 렌더링 회귀 테스트.

네트워크(야후 파이낸스) 호출과 텔레그램 발송을 모두 목업으로 대체하므로
CI 환경에서 외부 의존 없이 실행됩니다. Playwright 브라우저도 필요하지 않습니다.

실행: python test_report_render.py
"""
import sys
import datetime
sys.stdout.reconfigure(encoding='utf-8')

from jinja2 import Environment, FileSystemLoader

import index_closing as ic
import infographic_generator as ig

failures = []


def check(label, condition, detail=""):
    print(f"{'✅' if condition else '❌'} {label}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(label)


AS_OF = datetime.date(2026, 7, 28)
MOCK = {
    "KOSPI":      {"current_close": 3210.55, "point_change": 24.31,  "pct_change": 0.76,  "local_high_pct": -0.78,  "unit": "pt",  "market": "KR", "as_of": None},
    "KOSDAQ":     {"current_close": 812.44,  "point_change": -6.12,  "pct_change": -0.75, "local_high_pct": -16.06, "unit": "pt",  "market": "KR", "as_of": None},
    "S&P 500":    {"current_close": 6421.30, "point_change": -18.90, "pct_change": -0.29, "local_high_pct": -1.44,  "unit": "pt",  "market": "US", "as_of": AS_OF},
    "NASDAQ 100": {"current_close": 23890.12,"point_change": 112.40, "pct_change": 0.47,  "local_high_pct": -2.13,  "unit": "pt",  "market": "US", "as_of": AS_OF},
    "TQQQ":       {"current_close": 92.45,   "point_change": 1.20,   "pct_change": 1.32,  "local_high_pct": -31.87, "unit": "usd", "market": "US", "as_of": AS_OF},
    "QLD":        {"current_close": 148.02,  "point_change": -0.95,  "pct_change": -0.64, "local_high_pct": -12.40, "unit": "usd", "market": "US", "as_of": AS_OF},
}

print("=" * 60)
print("1. 종목 메타데이터 정합성")
print("=" * 60)

check("INDICES에 6개 종목 등록", len(ic.INDICES) == 6, str(len(ic.INDICES)))
for name in ["KOSPI", "KOSDAQ", "S&P 500", "NASDAQ 100", "TQQQ", "QLD"]:
    check(f"{name} 등록됨", name in ic.INDICES)

for name, meta in ic.INDICES.items():
    check(f"{name} 메타 필드 완비",
          all(k in meta for k in ("ticker", "market", "unit", "flag", "slug")))
    check(f"{name} market 값 유효", meta["market"] in ("KR", "US"), meta["market"])
    check(f"{name} unit 값 유효", meta["unit"] in ("pt", "usd"), meta["unit"])

# slug는 인포그래픽 CSS 클래스로 쓰이므로 중복되면 안 된다
slugs = [m["slug"] for m in ic.INDICES.values()]
check("slug 중복 없음", len(slugs) == len(set(slugs)), str(slugs))

check("레버리지 ETF 규칙이 INDICES에 존재",
      all(n in ic.INDICES for n in ic.LEVERAGE_ETF_RULES))

print()
print("=" * 60)
print("2. 숫자 포맷팅 (지수 pt / ETF USD)")
print("=" * 60)

check("지수 종가 포맷", ic.format_price(3210.55, "pt") == "3,210.55", ic.format_price(3210.55, "pt"))
check("ETF 종가 포맷", ic.format_price(92.45, "usd") == "$92.45", ic.format_price(92.45, "usd"))
check("지수 등락 포맷", ic.format_change_block(MOCK["KOSPI"]) == "▲24.31pt, +0.76%",
      ic.format_change_block(MOCK["KOSPI"]))
check("ETF 등락 포맷", ic.format_change_block(MOCK["TQQQ"]) == "▲$1.20, +1.32%",
      ic.format_change_block(MOCK["TQQQ"]))
check("ETF 하락 등락 포맷", ic.format_change_block(MOCK["QLD"]) == "▼$0.95, -0.64%",
      ic.format_change_block(MOCK["QLD"]))

print()
print("=" * 60)
print("3. 15:45 텍스트 리포트 생성")
print("=" * 60)

sent = []
ic.fetch_index_data = lambda name, ticker=None: MOCK[name]
ic.generate_index_macro_comment = lambda: "테스트용 매크로 한줄평입니다."
ic.send_telegram_message = lambda m, **k: sent.append(m) or True
ig.generate_and_send_infographic = lambda *a, **k: None

ic.execute_index_closing()

check("리포트 1건 발송", len(sent) == 1, f"{len(sent)}건")
report = sent[0] if sent else ""

check("국내 섹션 존재", "국내 지수" in report)
check("미국 섹션 존재", "미국 지수" in report)
check("미국 기준일 표기", "7/28 종가 기준" in report, "기준일 문구 누락")
for name in MOCK:
    check(f"{name} 행 포함", f"**{name}**" in report)
check("ETF 달러 표기 반영", "$92.45 (▲$1.20, +1.32%)" in report)
check("지수 pt 표기 유지", "3,210.55 (▲24.31pt, +0.76%)" in report)
check("매크로 한줄평 포함", "테스트용 매크로 한줄평입니다." in report)

print()
print("=" * 60)
print("4. 인포그래픽 카드 컨텍스트 및 템플릿 렌더링")
print("=" * 60)

env = Environment(loader=FileSystemLoader("templates"))
template = env.get_template("infographic_widget.html")

# 일부 종목 데이터 수집 실패 상황도 함께 검증
mock_with_failure = dict(MOCK)
mock_with_failure["QLD"] = None

rendered = {}
for group in ig.CARD_GROUPS:
    names = [n for n in mock_with_failure if ic.get_index_meta(n)["market"] == group["key"]]
    cards = [ig.build_card_context(n, mock_with_failure[n]) for n in names]

    check(f"[{group['key']}] 카드 수", len(cards) == (2 if group["key"] == "KR" else 4), str(len(cards)))
    for c in cards:
        check(f"[{group['key']}] {c['name']} 카드 필드 완비",
              all(k in c for k in ("name", "slug", "flag", "close", "change", "color", "lh", "comment")))
        check(f"[{group['key']}] {c['name']} AI 진단 문구 존재", bool(c["comment"]))

    html = template.render(
        title=group["title"],
        subtitle=group["subtitle"].format(date_str="2026년 07월 29일"),
        cards=cards,
        macro_comment="테스트용 매크로 한줄평입니다.",
        date_str="2026년 07월 29일",
    )
    rendered[group["key"]] = html
    check(f"[{group['key']}] 템플릿 렌더 성공", len(html) > 1000, f"{len(html)}자")

# 데이터 수집 실패 종목 폴백
qld_card = ig.build_card_context("QLD", None)
check("데이터 실패 시 N/A 표기", qld_card["close"] == "N/A", qld_card["close"])
check("데이터 실패 시 안내 문구", qld_card["comment"] == "데이터 수집 지연", qld_card["comment"])

# 카드 4개 이상이면 캔버스를 넓히는 wide 클래스가 붙어야 한다
check("국내판(2장)은 wide 미적용", 'class=""' in rendered["KR"] or "wide" not in rendered["KR"].split("<div")[0])
check("미국판(4장)은 wide 적용", 'class="wide"' in rendered["US"])
check("미국판 dense 레이아웃 적용", "cards-container dense" in rendered["US"])
check("미국판 기준일 배지 렌더", "7/28 종가 기준" in rendered["US"])

# 신규 종목 CSS 클래스가 템플릿에 정의되어 있는지
css = (open("templates/infographic_widget.html", encoding="utf-8").read())
for slug in ("nasdaq100", "tqqq", "qld"):
    check(f".card.{slug} 스타일 정의", f".card.{slug}::before" in css)

print()
print("=" * 60)
if failures:
    print(f"❌ 실패 {len(failures)}건:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("🎉 전체 통과")
