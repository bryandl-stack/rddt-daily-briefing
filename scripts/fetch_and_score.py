"""
r/wallstreetbets 후보 게시물 수집 + 4가지 기준 점수 계산
→ /tmp/wsb_candidates.json 에 점수 내림차순으로 저장

수집: 요청 코드는 한 벌이고 인증만 두 갈래 (Reddit이 비인증 JSON을 403으로 막음).
  - REDDIT_CLIENT_ID/SECRET 있으면 → oauth.reddit.com (공식 API, praw 없이 토큰 직접 발급)
  - 없으면 → old.reddit.com + REDDIT_COOKIES / ~/.cache/wsb_briefing/cookies.json 쿠키

사용법:
  python3 fetch_and_score.py                    # 후보 수집 + 점수 계산
  python3 fetch_and_score.py --mark-sent ID...  # 지정한 게시물 id를 "전송 완료"로 표시
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import fcntl
import logging
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import notify

# ── 로깅 설정 ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wsb_fetch")

# ── 설정 ─────────────────────────────────────────────────
SUBREDDIT_NAME        = "wallstreetbets"
FETCH_POOL_SIZE       = 200
TOP_N_CANDIDATES      = 20
TOP_COMMENTS_PER_POST = 15
OUTPUT_PATH           = "/tmp/wsb_candidates.json"

CACHE_DIR         = Path.home() / ".cache" / "wsb_briefing"
TICKER_CACHE_PATH = CACHE_DIR / "tickers_cache.json"
SENT_IDS_PATH     = CACHE_DIR / "sent_ids.json"
COOKIE_PATH       = CACHE_DIR / "cookies.json"
LOCK_PATH         = CACHE_DIR / "fetch.lock"
TICKER_LIST_URL   = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
TICKER_CACHE_MAX_AGE_DAYS = 7
SENT_ID_RETENTION_DAYS    = 3

WEIGHTS = {"engagement": 0.35, "velocity": 0.30, "flair": 0.15, "ticker_trend": 0.20}
PRIORITY_FLAIRS = {"DD", "News", "Discussion", "Macro", "Gain", "Loss"}

MIN_SCORE     = 50   # 업보트 이 값 미만은 노이즈로 제외
MAX_AGE_HOURS = 36   # 이보다 오래된 게시물은 제외

# $TICKER 표기는 그대로 신뢰하고, 평문 대문자는 화이트리스트 + 차단목록 + 2글자 이상으로 거른다.
# ($ 표기만 쓰면 실측상 티커가 0개 잡힘 — WSB는 평문 표기가 대부분)
TICKER_DOLLAR_PATTERN = re.compile(r"\$([A-Z]{1,5})\b")
TICKER_BARE_PATTERN   = re.compile(r"\b([A-Z]{2,5})\b")
AMBIGUOUS_WORD_TICKERS = {
    "IT", "SO", "ON", "GO", "ALL", "ARE", "CAT", "KEY", "NOW", "YOU",
    "FOR", "ONE", "CAN", "NEW", "DAY", "LOW", "OPEN", "REAL", "WELL",
    "GOOD", "PLAY", "FAST", "TRUE", "NEXT", "FREE", "SAFE", "EASY",
    "MOVE", "GAIN", "LOSS", "HOLD", "SELL", "BUY", "CALL", "PUT", "RUN",
}
KST = timezone(timedelta(hours=9))

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
API_UA     = f"wsb_daily_briefing_skill/1.0 by u/{os.environ.get('REDDIT_USERNAME', 'unknown')}"

SESSION = requests.Session()
SESSION.headers["Accept"] = "application/json"
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
)))

_AUTH_HELP = """
Reddit이 요청을 거부했어요 (403). 비인증 접근은 막혀 있어서 둘 중 하나가 필요해요.

  [1] 공식 API (승인됐다면 이게 제일 안정적)
      export REDDIT_CLIENT_ID="..."
      export REDDIT_CLIENT_SECRET="..."
      export REDDIT_USERNAME="..."      # User-Agent 표기용

  [2] 브라우저 쿠키 (승인 전 임시)
      F12 → Application → Cookies → https://www.reddit.com 에서
      loid, session_tracker, csv 값을 복사한 뒤 둘 중 하나:
        export REDDIT_COOKIES='{"loid":"...","session_tracker":"...","csv":"..."}'
        또는 ~/.cache/wsb_briefing/cookies.json 에 같은 JSON으로 저장
"""


# ── 티커 화이트리스트 ────────────────────────────────────
def load_valid_tickers() -> set:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if TICKER_CACHE_PATH.exists():
        try:
            cached = json.loads(TICKER_CACHE_PATH.read_text(encoding="utf-8"))
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])).days
            if age_days < TICKER_CACHE_MAX_AGE_DAYS:
                log.info(f"티커 목록 캐시 사용 ({len(cached['tickers'])}개, {age_days}일 전 갱신)")
                return set(cached["tickers"])
        except Exception as e:
            log.warning(f"티커 캐시 파싱 실패, 재다운로드 시도: {e}")

    try:
        resp = SESSION.get(TICKER_LIST_URL, timeout=15)
        resp.raise_for_status()
        tickers = {line.strip() for line in resp.text.splitlines() if line.strip()}
        if len(tickers) < 1000:
            raise ValueError(f"티커 수가 비정상적으로 적음: {len(tickers)}개")
        TICKER_CACHE_PATH.write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "tickers": sorted(tickers)}),
            encoding="utf-8",
        )
        log.info(f"티커 목록 다운로드 완료 ({len(tickers)}개)")
        return tickers
    except Exception as e:
        if TICKER_CACHE_PATH.exists():
            log.warning(f"티커 목록 다운로드 실패({e}) → 만료된 캐시라도 사용")
            return set(json.loads(TICKER_CACHE_PATH.read_text(encoding="utf-8"))["tickers"])
        raise


def extract_tickers(text: str, valid_tickers: set) -> list:
    text = text or ""
    hits = set(TICKER_DOLLAR_PATTERN.findall(text)) | {
        t for t in TICKER_BARE_PATTERN.findall(text) if t not in AMBIGUOUS_WORD_TICKERS
    }
    return [t for t in hits if t in valid_tickers]


# ── 상태 파일 유틸 ───────────────────────────────────────
def load_sent_ids() -> set:
    if not SENT_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(SENT_IDS_PATH.read_text(encoding="utf-8"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=SENT_ID_RETENTION_DAYS)
        return {pid for pid, ts in data.items() if datetime.fromisoformat(ts) > cutoff}
    except Exception as e:
        log.warning(f"sent_ids 로드 실패, 빈 목록으로 시작: {e}")
        return set()


def save_sent_ids(new_ids: list):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if SENT_IDS_PATH.exists():
        try:
            existing = json.loads(SENT_IDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    now_iso = datetime.now(timezone.utc).isoformat()
    existing.update({pid: now_iso for pid in new_ids})
    cutoff = datetime.now(timezone.utc) - timedelta(days=SENT_ID_RETENTION_DAYS)
    existing = {pid: ts for pid, ts in existing.items() if datetime.fromisoformat(ts) > cutoff}
    SENT_IDS_PATH.write_text(json.dumps(existing), encoding="utf-8")


def acquire_lock():
    """flock 기반 중복 실행 방지. 프로세스가 죽으면 커널이 알아서 풀어준다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fcntl.flock(os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("이미 실행 중 (lock 획득 실패). 중복 실행 방지로 종료.")
        sys.exit(1)


# ── 수집 ─────────────────────────────────────────────────
def _load_cookies() -> dict:
    """REDDIT_COOKIES 환경변수(JSON) → cookies.json 파일 순. 없으면 빈 dict(=익명)."""
    env = os.environ.get("REDDIT_COOKIES", "").strip()
    if env:
        return json.loads(env)
    if COOKIE_PATH.exists():
        return json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    return {}


_TOKEN = None


def _endpoint() -> tuple[str, str, dict]:
    """(base, suffix, requests 인자). 자격증명 있으면 공식 OAuth API, 없으면 쿠키+old.reddit."""
    global _TOKEN
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not (cid and secret):
        return "https://old.reddit.com", ".json", {
            "headers": {"User-Agent": BROWSER_UA}, "cookies": _load_cookies(),
        }
    if _TOKEN is None:
        resp = SESSION.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, secret), data={"grant_type": "client_credentials"},
            headers={"User-Agent": API_UA}, timeout=15,
        )
        if resp.status_code == 401:
            print(_AUTH_HELP, file=sys.stderr)
            raise RuntimeError("Reddit 401: CLIENT_ID/SECRET이 올바르지 않음")
        resp.raise_for_status()
        _TOKEN = resp.json()["access_token"]
        log.info("Reddit OAuth 토큰 발급 완료")
    return "https://oauth.reddit.com", "", {
        "headers": {"User-Agent": API_UA, "Authorization": f"bearer {_TOKEN}"},
    }


def _get_json(path: str):
    """path 예: '/r/wallstreetbets/hot?limit=200'"""
    base, suffix, auth = _endpoint()
    head, _, query = path.partition("?")
    resp = SESSION.get(f"{base}{head}{suffix}?{query}", timeout=15, **auth)
    if resp.status_code in (401, 403):
        print(_AUTH_HELP, file=sys.stderr)
        raise RuntimeError(f"Reddit {resp.status_code}: 자격증명/쿠키가 없거나 만료됨")
    resp.raise_for_status()
    return resp.json()


def fetch_posts(seen_ids: set, pool_size: int) -> list[dict]:
    data = _get_json(f"/r/{SUBREDDIT_NAME}/hot?limit={pool_size}")
    now = time.time()
    return [
        d for d in (c["data"] for c in data["data"]["children"])
        if not d.get("stickied")
        and d["id"] not in seen_ids
        and d.get("score", 0) >= MIN_SCORE
        and (now - d["created_utc"]) <= MAX_AGE_HOURS * 3600
    ]


def _score_comment(d: dict, valid_tickers: set) -> float:
    """댓글 dict → 품질 점수 (음수: 제외)."""
    body = d.get("body", "").strip()
    if body in ("[deleted]", "[removed]") or len(body) < 15 or d.get("author") == "AutoModerator":
        return -1
    return (
        min(d.get("score", 0), 5000) / 5000 * 40
        + min(len(body), 300) / 300 * 15
        + (10 if d.get("is_submitter") else 0)
        + (10 if extract_tickers(body, valid_tickers) else 0)
        + min(d.get("total_awards_received", 0), 3) / 3 * 10
        - (5 if d.get("controversiality") == 1 else 0)
    )


def fetch_top_comments(post_id: str, n: int, valid_tickers: set) -> list[str]:
    try:
        data = _get_json(f"/comments/{post_id}?sort=confidence&limit={n * 3}")
    except Exception as e:
        log.warning(f"댓글 수집 실패 (id={post_id}): {e}")
        return []
    scored = [
        (sc, item["data"]["body"])
        for item in data[1]["data"]["children"]
        if item["kind"] == "t1" and (sc := _score_comment(item["data"], valid_tickers)) >= 0
    ]
    scored.sort(reverse=True)
    return [body for _, body in scored[:n]]


# ── 점수 계산 + 출력 ─────────────────────────────────────
def _minmax(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_and_collect(posts: list[dict], valid_tickers: set):
    if not posts:
        return [], Counter()

    now = time.time()
    engagement_raw = [p["score"] + p["num_comments"] * 2 for p in posts]
    velocity_raw = [
        e / max((now - p["created_utc"]) / 3600, 0.5)
        for e, p in zip(engagement_raw, posts)
    ]

    ticker_counter: Counter = Counter()
    post_tickers = []
    for p in posts:
        tickers = extract_tickers(f"{p['title']} {p.get('selftext', '')}", valid_tickers)
        post_tickers.append(tickers)
        ticker_counter.update(set(tickers))

    top_trending = {t for t, _ in ticker_counter.most_common(10)}
    eng_norm = _minmax(engagement_raw)
    vel_norm = _minmax(velocity_raw)

    scored = [
        (
            eng_norm[i] * WEIGHTS["engagement"]
            + vel_norm[i] * WEIGHTS["velocity"]
            + (WEIGHTS["flair"] if p.get("link_flair_text") in PRIORITY_FLAIRS else 0)
            + (WEIGHTS["ticker_trend"] if set(post_tickers[i]) & top_trending else 0)
            + (0.1 if p.get("is_self") else 0),
            p,
            post_tickers[i],
        )
        for i, p in enumerate(posts)
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:TOP_N_CANDIDATES], ticker_counter


def build_output(scored, ticker_counter, valid_tickers: set):
    items = []
    for final_score, p, tickers in scored:
        is_self = p.get("is_self", False)
        items.append({
            "id": p["id"],
            "title": p["title"],
            "body": p.get("selftext", "") if is_self else "",
            "url": None if is_self else p.get("url", ""),
            "permalink": f"https://reddit.com{p.get('permalink', '')}",
            "created_kst": datetime.fromtimestamp(p["created_utc"], tz=timezone.utc)
                .astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
            "num_comments": p["num_comments"],
            "score": p["score"],
            "upvote_ratio": p.get("upvote_ratio", 0.0),
            "flair": p.get("link_flair_text"),
            "tickers": sorted(set(tickers)),
            "top_comments": fetch_top_comments(p["id"], TOP_COMMENTS_PER_POST, valid_tickers),
            "final_score": round(final_score, 4),
        })
    return {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "trending_tickers": ticker_counter.most_common(5),
        "candidates": items,
    }


# ── 메인 ─────────────────────────────────────────────────
def send_failure_alert(error_message: str):
    try:
        notify.send(
            f"⚠️ WSB 일일 브리핑 실패\n"
            f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}\n\n"
            f"에러: {error_message}"
        )
        log.info("실패 알림 전송 완료")
    except Exception as e:
        log.error(f"실패 알림 전송도 실패함: {e}")


def mark_sent(ids: list):
    if not ids:
        log.error("--mark-sent 뒤에 게시물 id를 하나 이상 지정해야 해요")
        sys.exit(1)
    save_sent_ids(ids)
    log.info(f"{len(ids)}개 게시물을 전송 완료로 표시함: {', '.join(ids)}")


def run_pipeline():
    valid_tickers = load_valid_tickers()
    seen_ids = load_sent_ids()
    log.info(f"최근 {SENT_ID_RETENTION_DAYS}일 내 중복 제외 대상: {len(seen_ids)}개")

    posts = fetch_posts(seen_ids, FETCH_POOL_SIZE)
    log.info(f"수집 완료: {len(posts)}개")

    scored, ticker_counter = score_and_collect(posts, valid_tickers)
    output = build_output(scored, ticker_counter, valid_tickers)
    Path(OUTPUT_PATH).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info(
        f"완료: 후보 {len(output['candidates'])}개 → {OUTPUT_PATH} "
        f"(전송 완료 표시는 --mark-sent로 별도 진행)"
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--mark-sent":
        mark_sent(sys.argv[2:])
        return

    acquire_lock()
    try:
        run_pipeline()
    except Exception as e:
        log.exception("파이프라인 실패")
        send_failure_alert(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
