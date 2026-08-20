"""
텔레그램 이미지 발송 로직 검증.

실제 사례: 채팅방 하나(1749189327)에서 사용자가 봇을 차단해
403 Forbidden이 반환됐는데, 예전 코드는 이를 전체 실패로 처리해
정상 수신한 채팅방까지 포함한 전원에게 "렌더링 오류" 알림을 보냈습니다.

네트워크 호출은 목업으로 대체하므로 API 키 없이 실행됩니다.

실행: python test_telegram_photo.py
"""
import sys
import json
sys.stdout.reconfigure(encoding='utf-8')

import notifier
import chat_manager

failures = []


def check(label, condition, detail=""):
    print(f"{'✅' if condition else '❌'} {label}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(label)


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload, ensure_ascii=False)


def ok_photo(file_id="FILE_ID_1"):
    return _Resp(200, {"ok": True, "result": {"photo": [{"file_id": "small"}, {"file_id": file_id}]}})


FORBIDDEN = _Resp(403, {"ok": False, "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user"})
RATE_LIMITED = _Resp(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})
SERVER_ERROR = _Resp(500, {"ok": False, "error_code": 500, "description": "Internal Server Error"})

notifier.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
notifier.time.sleep = lambda s: None  # 재시도 대기는 건너뛴다

removed_ids = []
chat_manager.remove_chat_id = lambda cid: (removed_ids.append(str(cid)), True)[1]
notifier.remove_chat_id = chat_manager.remove_chat_id

posts = []


def install_responses(responses, chat_ids):
    """responses: 호출 순서대로 돌려줄 응답 목록"""
    posts.clear()
    removed_ids.clear()
    notifier.get_all_chat_ids = lambda: list(chat_ids)
    seq = list(responses)

    def fake_post(url, data=None, files=None, timeout=None, json=None):
        posts.append({"data": data, "has_file": files is not None})
        return seq.pop(0) if seq else ok_photo()

    notifier.requests.post = fake_post


print("=" * 60)
print("1. 차단된 채팅방(403)이 있어도 나머지는 정상 발송된다")
print("=" * 60)

# 실제 로그와 동일한 구성: 4개 정상 + 1개 차단
CHATS = ["8708637944", "-5147192029", "-5169137374", "1749189327", "-5183702639"]
install_responses([ok_photo(), ok_photo(), ok_photo(), FORBIDDEN, ok_photo()], CHATS)

result = notifier.send_telegram_photo(b"fake-image-bytes")

check("정상 발송 4건", len(result["sent"]) == 4, str(result["sent"]))
check("차단 채팅방은 failed에 들어가지 않음", len(result["failed"]) == 0, str(result["failed"]))
check("차단 채팅방이 removed에 기록됨", result["removed"] == ["1749189327"], str(result["removed"]))
check("차단 채팅방이 목록에서 제거됨", removed_ids == ["1749189327"], str(removed_ids))

print()
print("=" * 60)
print("2. file_id 재사용 — 이미지는 한 번만 업로드한다")
print("=" * 60)

uploads = [p for p in posts if p["has_file"]]
reuses = [p for p in posts if not p["has_file"] and p["data"].get("photo")]
check("업로드는 1회", len(uploads) == 1, f"{len(uploads)}회")
check("나머지는 file_id로 재전송", len(reuses) == 4, f"{len(reuses)}건")
check("재전송에 올바른 file_id 사용",
      all(p["data"].get("photo") == "FILE_ID_1" for p in reuses),
      str([p["data"].get("photo") for p in reuses]))

print()
print("=" * 60)
print("3. 429(속도 제한)는 재시도한다")
print("=" * 60)

install_responses([RATE_LIMITED, ok_photo()], ["111"])
result = notifier.send_telegram_photo(b"fake-image-bytes")
check("재시도 후 발송 성공", result["sent"] == ["111"], str(result))
check("총 2회 요청", len(posts) == 2, f"{len(posts)}회")

print()
print("=" * 60)
print("4. 일시적 오류(500)는 재시도 후 실패로 기록된다")
print("=" * 60)

install_responses([SERVER_ERROR], ["222"])
result = notifier.send_telegram_photo(b"fake-image-bytes")
check("failed에 기록", len(result["failed"]) == 1, str(result["failed"]))
check("제거되지 않음 (일시적 오류)", result["removed"] == [], str(result["removed"]))

print()
print("=" * 60)
print("5. 인포그래픽: 부분 실패 시 오류 알림을 보내지 않는다")
print("=" * 60)

import infographic_generator as ig

alerts = []
notifier.send_telegram_message = lambda m, **k: alerts.append(m) or True
ig.render_infographic = lambda cards, title, subtitle, macro_comment: b"fake-png"

MOCK_KR = {
    "KOSPI":  {"current_close": 3210.55, "point_change": 24.31, "pct_change": 0.76,
               "local_high_pct": -0.78, "unit": "pt", "market": "KR", "as_of": None},
    "KOSDAQ": {"current_close": 812.44, "point_change": -6.12, "pct_change": -0.75,
               "local_high_pct": -16.06, "unit": "pt", "market": "KR", "as_of": None},
}

# (a) 일부 채팅방 실패 → 알림 없음
alerts.clear()
ig.send_telegram_photo = lambda b: {"sent": ["1", "2"], "failed": [("3", "timeout")], "removed": []}
ig.generate_and_send_infographic(MOCK_KR, "테스트 한줄평")
check("부분 실패 시 오류 알림 없음", len(alerts) == 0, f"{len(alerts)}건: {alerts[:1]}")

# (b) 차단 채팅방만 있고 나머지 성공 → 알림 없음
alerts.clear()
ig.send_telegram_photo = lambda b: {"sent": ["1", "2"], "failed": [], "removed": ["3"]}
ig.generate_and_send_infographic(MOCK_KR, "테스트 한줄평")
check("차단 채팅방 제거 시 오류 알림 없음", len(alerts) == 0, f"{len(alerts)}건")

# (c) 전부 실패 → 알림 발송, 문구에 브라우저 언급 없어야 함
alerts.clear()
ig.send_telegram_photo = lambda b: {"sent": [], "failed": [("1", "timeout"), ("2", "timeout")], "removed": []}
ig.generate_and_send_infographic(MOCK_KR, "테스트 한줄평")
check("전체 실패 시 오류 알림 발송", len(alerts) == 1, f"{len(alerts)}건")
if alerts:
    check("실패 사유가 메시지에 포함", "timeout" in alerts[0], alerts[0][:100])
    check("렌더링 성공을 명시", "이미지 생성은 정상 완료" in alerts[0], alerts[0][:100])
    check("브라우저 문제로 오인 유도하지 않음", "Playwright" not in alerts[0], alerts[0][:100])

# (d) 렌더링 자체가 실패하면 브라우저 문제로 안내
alerts.clear()
def _boom(*a, **k):
    raise RuntimeError("Executable doesn't exist")
ig.render_infographic = _boom
ig.generate_and_send_infographic(MOCK_KR, "테스트 한줄평")
check("렌더링 실패 시 알림 발송", len(alerts) == 1, f"{len(alerts)}건")
if alerts:
    check("렌더링 실패는 브라우저 문제로 안내", "Playwright" in alerts[0], alerts[0][:100])

print()
print("=" * 60)
if failures:
    print(f"❌ 실패 {len(failures)}건:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("🎉 전체 통과")
