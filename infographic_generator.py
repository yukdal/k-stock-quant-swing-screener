import os
import time
import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright
from notifier import send_telegram_photo

BASE_DIR = Path(__file__).resolve().parent

# 인포그래픽 이미지 분리 발송 그룹 (국내 1장 / 미국 1장)
CARD_GROUPS = [
    {
        "key": "KR",
        "title": "✨ 오후 3시 45분 국내 증시 마감 브리핑",
        "subtitle": "{date_str} 기준 · AI 기반 핵심 데이터 교차검증 완료"
    },
    {
        "key": "US",
        "title": "🌎 미국 증시 · 레버리지 ETF 브리핑",
        "subtitle": "{date_str} 발송 · 미국장 직전 거래일 종가 기준"
    },
]

def get_comment_for_index(name, point_change, pct_change, local_high_pct):
    if name == "KOSPI":
        if local_high_pct > -3:
            return "전고점 회복에 바짝 다가섰습니다."
        elif point_change > 0:
            return "상승세로 마감하며 분위기 반전을 시도하고 있습니다."
        else:
            return "하락 마감하며 저항선 돌파에 어려움을 겪고 있습니다."
    elif name == "KOSDAQ":
        if point_change > 0:
            return "당일 강세를 보였으나 전고점 대비로는 여전히 격차가 있습니다."
        else:
            return "약세를 보이며 하단 지지력을 시험받고 있습니다."
    elif name == "S&P 500":
        if point_change < 0:
            return "소폭 하락하며 전고점 대비 아래 위치를 기록했습니다."
        elif local_high_pct > -1:
            return "전고점을 돌파하거나 근접하며 강한 흐름을 유지하고 있습니다."
        else:
            return "상승세를 이어가며 견조한 흐름을 보이고 있습니다."
    elif name == "NASDAQ 100":
        if local_high_pct > -1:
            return "전고점 부근에서 기술주 주도의 강세 흐름을 유지하고 있습니다."
        elif point_change > 0:
            return "반등하며 기술주 투자심리가 개선되는 모습입니다."
        elif local_high_pct < -10:
            return "전고점 대비 조정 폭이 확대되며 기술주 변동성이 커지고 있습니다."
        else:
            return "하락 마감하며 전고점 회복까지 시간이 필요해 보입니다."
    elif name in ("TQQQ", "QLD"):
        # 레버리지 ETF는 전고점 대비 낙폭 구간을 매수 관점에서 안내
        if local_high_pct <= -50:
            return "전고점 대비 반토막 구간입니다. 원칙에 따른 분할 매수 대응 구간입니다."
        elif local_high_pct <= -30:
            return "깊은 조정 구간에 진입해 분할 매수 신호가 누적되고 있습니다."
        elif local_high_pct <= -10:
            return "분할 매수 1차 기준선을 이탈했습니다. 변동성 잠식에 유의하세요."
        elif point_change > 0:
            return "기초지수 강세에 레버리지가 더해지며 상승 폭을 키웠습니다."
        else:
            return "전고점 부근에서 등락 중입니다. 신규 진입은 신중할 구간입니다."
    return "시장 상황을 주시하고 있습니다."


def build_card_context(name, data):
    """단일 종목 데이터를 템플릿 카드 컨텍스트로 변환"""
    from index_closing import get_index_meta, format_price, format_change_block

    meta = get_index_meta(name)
    card = {
        "name": name,
        "slug": meta["slug"],
        "flag": meta["flag"],
    }

    if not data:
        card.update({
            "close": "N/A",
            "change": "- ",
            "color": "neutral",
            "lh": "-",
            "as_of": None,
            "comment": "데이터 수집 지연",
        })
        return card

    pt = data['point_change']
    lh_pct = data['local_high_pct']

    as_of = data.get('as_of')
    as_of_str = f"{as_of.month}/{as_of.day}" if as_of else None

    card.update({
        "close": format_price(data['current_close'], data.get('unit', 'pt')),
        "change": format_change_block(data),
        "color": "up" if pt > 0 else ("down" if pt < 0 else "neutral"),
        "lh": f"+{lh_pct:.2f}%" if lh_pct > 0 else f"{lh_pct:.2f}%",
        "as_of": as_of_str,
        "comment": get_comment_for_index(name, pt, data['pct_change'], lh_pct),
    })
    return card


def render_infographic(cards, title, subtitle, macro_comment):
    """카드 목록을 렌더링하여 PNG 바이트를 반환"""
    templates_dir = BASE_DIR / 'templates'
    templates_dir.mkdir(exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    template = env.get_template('infographic_widget.html')

    html_content = template.render(
        title=title,
        subtitle=subtitle,
        cards=cards,
        macro_comment=macro_comment,
        date_str=datetime.datetime.now().strftime("%Y년 %m월 %d일"),
    )

    print(f"🌐 Rendering HTML with Playwright... ({len(cards)} cards)")
    screenshot_bytes = None
    max_retries = 3
    for attempt in range(max_retries):
        browser = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-animations',
                        '--disable-gpu'
                    ]
                )
                # 카드가 많은 미국판은 캔버스(body.wide = 1640px)에 맞춰 뷰포트도 넓힌다
                viewport_width = 1720 if len(cards) > 3 else 1400
                page = browser.new_page(viewport={"width": viewport_width, "height": 1400})

                # Set HTML content and wait for fonts to load
                page.set_content(html_content, wait_until="load", timeout=30000)
                try:
                    page.evaluate("document.fonts.ready")
                except:
                    pass
                time.sleep(2) # Extra wait for web fonts to apply completely before screenshot

                # Select the widget-container to take a screenshot of just that element
                element = page.locator(".widget-container")

                # Element.screenshot()에서 계속 안정성 검사 대기(Timeout)가 발생하는 문제를 우회하기 위해
                # 요소의 절대 좌표(Bounding Box)를 계산하여 페이지 전체 스크린샷에서 해당 부분만 잘라냅니다.
                box = element.bounding_box()
                if box:
                    # 그림자(box-shadow)가 잘리지 않도록 상하좌우 여유 공간(패딩)을 추가합니다.
                    clip_box = {
                        "x": max(0, box["x"] - 20),
                        "y": max(0, box["y"] - 20),
                        "width": box["width"] + 40,
                        "height": box["height"] + 60
                    }
                    screenshot_bytes = page.screenshot(clip=clip_box, type="png")
                else:
                    # 좌표를 찾지 못한 경우 Fallback
                    screenshot_bytes = element.screenshot(animations="disabled", type="png", timeout=20000)

                browser.close()
                break # 성공 시 루프 탈출
        except Exception as inner_e:
            err_str = str(inner_e)
            print(f"⚠️ 인포그래픽 렌더링 시도 {attempt + 1}/{max_retries} 실패: {err_str[:200]}")
            try:
                if browser: browser.close()
            except: pass

            if "Executable doesn't exist" in err_str or "playwright install" in err_str:
                print("🔧 Playwright 브라우저 바이너리가 없습니다. 자동으로 설치를 시도합니다 (playwright install chromium)...")
                import sys, subprocess
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
                print("💡 참고: 리눅스 서버에서 라이브러리 종속성 문제가 발생할 경우 터미널에서 'playwright install-deps'를 실행해야 할 수 있습니다.")

            if attempt == max_retries - 1:
                raise inner_e # 최종 실패 시 예외 발생
            time.sleep(2)

    return screenshot_bytes


def generate_and_send_infographic(indices_data, macro_comment):
    """
    indices_data: dict with keys like "KOSPI", "KOSDAQ", "S&P 500", "NASDAQ 100", "TQQQ", "QLD"
    and values from fetch_index_data.

    국내/미국 두 장으로 나누어 렌더링하고 각각 텔레그램으로 발송합니다.
    한쪽 렌더링이 실패해도 나머지 한 장은 정상 발송됩니다.
    """
    from index_closing import get_index_meta

    date_str = datetime.datetime.now().strftime("%Y년 %m월 %d일")

    for group in CARD_GROUPS:
        group_names = [n for n in indices_data if get_index_meta(n)["market"] == group["key"]]
        if not group_names:
            continue

        try:
            cards = [build_card_context(n, indices_data[n]) for n in group_names]
            screenshot_bytes = render_infographic(
                cards,
                title=group["title"],
                subtitle=group["subtitle"].format(date_str=date_str),
                macro_comment=macro_comment
            )

            print(f"📸 Infographic image generated successfully. ({group['key']})")

            success = send_telegram_photo(screenshot_bytes)
            if not success:
                raise Exception("텔레그램 이미지 발송 API 호출에 실패했습니다. (터미널 로그를 확인해주세요)")

        except Exception as e:
            error_msg = (
                f"❌ 인포그래픽 이미지({group['key']}) 렌더링 중 오류가 발생했습니다:\n`{str(e)}`\n\n"
                "(Playwright/크롬 브라우저 실행 문제일 가능성이 높습니다)"
            )
            print(error_msg)
            from notifier import send_telegram_message
            send_telegram_message(error_msg)
