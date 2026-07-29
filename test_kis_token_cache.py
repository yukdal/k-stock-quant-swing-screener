"""
KIS 액세스 토큰 캐시 복구 로직 검증.

스케줄러(systemd)와 수동 실행이 동시에 캐시 파일을 쓰면 JSON이 깨져
`Extra data: line 1 column 400 (char 399)` 같은 오류가 발생하는데,
예전 코드는 이 예외를 처리하지 않아 깨진 캐시가 영구히 복구되지 않았습니다.

네트워크 호출은 목업으로 대체하므로 API 키 없이 실행됩니다.

실행: python test_kis_token_cache.py
"""
import sys
import json
import time
sys.stdout.reconfigure(encoding='utf-8')

import kis_api

failures = []


def check(label, condition, detail=""):
    print(f"{'✅' if condition else '❌'} {label}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(label)


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"access_token": "NEW_TOKEN", "expires_in": 86400}

    @property
    def text(self):
        return json.dumps(self.json())


_post_calls = []
kis_api.requests.post = lambda *a, **k: (_post_calls.append(1), _FakeResponse())[1]

TOKEN_FILE = kis_api.TOKEN_FILE
TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)


def reset():
    _post_calls.clear()
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


print("=" * 60)
print("1. 정상 캐시는 재사용한다")
print("=" * 60)

reset()
TOKEN_FILE.write_text(json.dumps({"access_token": "CACHED", "expires_at": time.time() + 3600}))
token = kis_api.get_access_token()
check("캐시된 토큰 반환", token == "CACHED", str(token))
check("토큰 재발급 요청 없음", len(_post_calls) == 0, f"{len(_post_calls)}회")

print()
print("=" * 60)
print("2. 만료된 캐시는 재발급한다")
print("=" * 60)

reset()
TOKEN_FILE.write_text(json.dumps({"access_token": "OLD", "expires_at": time.time() - 10}))
token = kis_api.get_access_token()
check("새 토큰 반환", token == "NEW_TOKEN", str(token))
check("재발급 요청 1회", len(_post_calls) == 1, f"{len(_post_calls)}회")

print()
print("=" * 60)
print("3. 깨진 캐시에서 복구한다 (이번 수정의 핵심)")
print("=" * 60)

# 동시 쓰기로 JSON 객체가 두 번 이어붙은 상황을 재현
valid = json.dumps({"access_token": "A" * 350, "expires_at": time.time() + 3600})
corrupted_cases = {
    "JSON 두 개 연결 (Extra data)": valid + valid,
    "중간 잘림": valid[: len(valid) // 2],
    "빈 파일": "",
    "JSON 아님": "not json at all",
}

for label, content in corrupted_cases.items():
    reset()
    TOKEN_FILE.write_text(content)
    try:
        token = kis_api.get_access_token()
        ok = token == "NEW_TOKEN"
        check(f"{label} → 재발급 성공", ok, str(token))
    except Exception as e:
        check(f"{label} → 재발급 성공", False, f"예외 발생: {type(e).__name__}: {e}")

print()
print("=" * 60)
print("4. 재발급 후 캐시가 정상 JSON으로 저장된다")
print("=" * 60)

reset()
TOKEN_FILE.write_text(valid + valid)  # 깨진 상태에서 시작
kis_api.get_access_token()
try:
    saved = json.loads(TOKEN_FILE.read_text())
    check("캐시 파일이 유효한 JSON", True)
    check("저장된 토큰 값 일치", saved.get("access_token") == "NEW_TOKEN", str(saved.get("access_token")))
    check("만료시각 미래", saved.get("expires_at", 0) > time.time(), str(saved.get("expires_at")))
except Exception as e:
    check("캐시 파일이 유효한 JSON", False, f"{type(e).__name__}: {e}")

# 임시 파일이 남지 않아야 한다
leftovers = list(TOKEN_FILE.parent.glob("kis_token.*.tmp"))
check("임시 파일 잔여 없음", len(leftovers) == 0, str(leftovers))

reset()

print()
print("=" * 60)
if failures:
    print(f"❌ 실패 {len(failures)}건:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("🎉 전체 통과")
