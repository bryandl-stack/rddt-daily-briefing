"""
r/wallstreetbets 후보 게시물 수집 + 4가지 기준 점수 계산
→ /tmp/wsb_candidates.json 에 점수 내림차순으로 저장

개선 사항:
  - 티커 인식: 블랙리스트 대신 실제 NASDAQ/NYSE/AMEX 전체 티커 목록과 대조
    (주 1회 캐시 갱신, 다운로드 실패 시 캐시 또는 내장 폴백 리스트 사용)
  - 네트워크 호출에 재시도(exponential backoff) 적용
  - 최근 N일간 이미 내보낸 게시물 ID는 후보에서 제외 (중복 방지)
  - logging 모듈로 구조화된 로그 출력
  - 파이프라인 실패 시 notify.py를 통해 실패 알림 전송
  - "내보냄" 표시는 더 이상 이 스크립트가 자동으로 하지 않음. 실제로
    리포트에 포함된 게시물만 `--mark-sent`로 스킬(SKILL.md 5단계)이
    명시적으로 표시함 (표시 안 된 후보는 다음 실행 때 다시 나올 수 있음)
  - 댓글 수집(get_top_comments) 실패가 개별 게시물에 그치도록 격리 —
    한 게시물의 댓글 조회가 실패해도 전체 파이프라인이 죽지 않음
  - 티커 오탐 감소: 흔한 영단어와 겹치는 실제 티커(ON, SO, IT, ALL 등)는
    `$` 접두사가 있을 때만 인정
  - cron 중복 실행 방지용 lock 파일 추가
  - 중복 제외 필터링 후 후보가 너무 적으면 더 넓은 풀로 재시도

사전 준비:
  pip install praw requests
  export REDDIT_CLIENT_ID="..."
  export REDDIT_CLIENT_SECRET="..."
  export REDDIT_USERNAME="..."   # User-Agent에 포함됨 (Reddit API 정책 준수용)

사용법:
  python3 fetch_and_score.py                  # 후보 수집 + 점수 계산
  python3 fetch_and_score.py --mark-sent ID...  # 지정한 게시물 id를 "전송 완료"로 표시
"""

import os
import re
import sys
import json
import time
import logging
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests
import praw
from prawcore.exceptions import PrawcoreException

# ── 로깅 설정 ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wsb_fetch")

# ── 자격증명 / 설정 ──────────────────────────────────────
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "")
REDDIT_USER_AGENT = (
    f"wsb_daily_briefing_skill by u/{REDDIT_USERNAME}"
    if REDDIT_USERNAME
    else "wsb_daily_briefing_skill (REDDIT_USERNAME 미설정)"
)

SUBREDDIT_NAME = "wallstreetbets"
FETCH_POOL_SIZE = 100
TOP_N_CANDIDATES = 20
TOP_COMMENTS_PER_POST = 5
OUTPUT_PATH = "/tmp/wsb_candidates.json"

CACHE_DIR = Path.home() / ".cache" / "wsb_briefing"
TICKER_CACHE_PATH = CACHE_DIR / "tickers_cache.json"
SENT_IDS_PATH = CACHE_DIR / "sent_ids.json"
LOCK_PATH = CACHE_DIR / "fetch.lock"
TICKER_LIST_URL = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
TICKER_CACHE_MAX_AGE_DAYS = 7
SENT_ID_RETENTION_DAYS = 3   # 이 기간 내 표시된 글은 다시 후보에 안 올림
LOCK_STALE_SECONDS = 30 * 60  # 이보다 오래된 lock은 죽은 프로세스로 간주하고 정리

WEIGHTS = {"engagement": 0.35, "velocity": 0.30, "flair": 0.15, "ticker_trend": 0.20}
PRIORITY_FLAIRS = {"DD", "News", "Discussion", "Macro"}

# 다운로드 실패 + 캐시도 없을 때만 쓰는 최소 폴백 (대형주/WSB 단골 위주)
FALLBACK_TICKERS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AMD",
    "NFLX", "SPY", "QQQ", "PLTR", "GME", "AMC", "SOFI", "NIO", "COIN",
    "MSTR", "SMCI", "AVGO", "BABA", "INTC", "BA", "DIS", "JPM", "BAC",
    "XOM", "CVX", "WMT", "COST", "PYPL", "SNAP", "UBER", "LYFT", "RIVN",
    "LCID", "F", "GM", "T", "VZ", "PFE", "MRNA", "JNJ", "KO", "PEP",
}

TICKER_DOLLAR_PATTERN = re.compile(r"\$([A-Z]{1,5})\b")
TICKER_BARE_PATTERN = re.compile(r"\b([A-Z]{1,5})\b")

# 흔한 영단어와 우연히 겹치는 실제 티커들. $ 접두사 없이 등장하면 본문이
# 그냥 그 단어를 쓴 것일 확률이 높아서, 이 목록은 $ 없이는 티커로 인정하지 않음
AMBIGUOUS_WORD_TICKERS = {
    "A", "I", "IT", "SO", "ON", "GO", "ALL", "ARE", "CAT", "KEY", "NOW",
    "FOR", "ONE", "CAN", "NEW", "DAY", "LOW", "OPEN", "REAL", "WELL",
    "GOOD", "PLAY", "FAST", "TRUE", "NEXT", "FREE", "SAFE", "EASY",
    "MOVE", "GAIN", "LOSS", "HOLD", "SELL", "BUY", "CALL", "PUT", "RUN",
}

KST = timezone(timedelta(hours=9))


# ── 재시도 유틸 ──────────────────────────────────────────
def retry(times=3, base_delay=2, exceptions=(Exception,)):
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == times:
                        break
                    delay = base_delay * (2 ** (attempt - 1))
                    log.warning(
                        f"{func.__name__} 실패 ({attempt}/{times}): {e} "
                        f"→ {delay}초 후 재시도"
                    )
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


# ── 티커 화이트리스트 ────────────────────────────────────
def load_valid_tickers() -> set:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if TICKER_CACHE_PATH.exists():
        try:
            cached = json.loads(TICKER_CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            age_days = (datetime.now(timezone.utc) - fetched_at).days
            if age_days < TICKER_CACHE_MAX_AGE_DAYS:
                log.info(f"티커 목록 캐시 사용 ({len(cached['tickers'])}개, {age_days}일 전 갱신)")
                return set(cached["tickers"])
        except Exception as e:
            log.warning(f"티커 캐시 파싱 실패, 재다운로드 시도: {e}")

    try:
        tickers = _download_ticker_list()
        TICKER_CACHE_PATH.write_text(
            json.dumps({
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "tickers": sorted(tickers),
            }),
            encoding="utf-8",
        )
        log.info(f"티커 목록 다운로드 완료 ({len(tickers)}개), 캐시 갱신")
        return tickers
    except Exception as e:
        log.error(f"티커 목록 다운로드 실패: {e}")
        if TICKER_CACHE_PATH.exists():
            log.warning("만료된 캐시라도 사용")
            cached = json.loads(TICKER_CACHE_PATH.read_text(encoding="utf-8"))
            return set(cached["tickers"])
        log.warning(f"내장 폴백 리스트 사용 ({len(FALLBACK_TICKERS)}개, 인식률 낮음)")
        return set(FALLBACK_TICKERS)


@retry(times=3, base_delay=2, exceptions=(requests.RequestException,))
def _download_ticker_list() -> set:
    resp = requests.get(TICKER_LIST_URL, timeout=15)
    resp.raise_for_status()
    tickers = {line.strip() for line in resp.text.splitlines() if line.strip()}
    if len(tickers) < 1000:  # 이상 징후 (파일 포맷이 바뀌었거나 손상됨)
        raise ValueError(f"다운로드된 티커 수가 비정상적으로 적음: {len(tickers)}개")
    return tickers


def extract_tickers(text: str, valid_tickers: set):
    text = text or ""
    dollar_hits = set(TICKER_DOLLAR_PATTERN.findall(text))
    bare_hits = {
        t for t in TICKER_BARE_PATTERN.findall(text)
        if t not in AMBIGUOUS_WORD_TICKERS
    }
    candidates = dollar_hits | bare_hits
    return [t for t in candidates if t in valid_tickers]


# ── 중복 방지 ────────────────────────────────────────────
def load_sent_ids() -> set:
    if not SENT_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(SENT_IDS_PATH.read_text(encoding="utf-8"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=SENT_ID_RETENTION_DAYS)
        valid = {
            pid for pid, ts in data.items()
            if datetime.fromisoformat(ts) > cutoff
        }
        return valid
    except Exception as e:
        log.warning(f"sent_ids 로드 실패, 빈 목록으로 시작: {e}")
        return set()


def acquire_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            log.error(
                f"이미 실행 중인 것으로 보여요 (lock 파일이 {age:.0f}초 전에 "
                f"생성됨). 중복 실행을 막기 위해 종료해요."
            )
            sys.exit(1)
        log.warning(f"오래된 lock 파일 발견 ({age:.0f}초 전), 정리하고 진행해요")
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"lock 파일 정리 실패: {e}")


def save_sent_ids(new_ids: list):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if SENT_IDS_PATH.exists():
        try:
            existing = json.loads(SENT_IDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    for pid in new_ids:
        existing[pid] = now_iso

    # 오래된 항목 정리
    cutoff = datetime.now(timezone.utc) - timedelta(days=SENT_ID_RETENTION_DAYS)
    existing = {
        pid: ts for pid, ts in existing.items()
        if datetime.fromisoformat(ts) > cutoff
    }
    SENT_IDS_PATH.write_text(json.dumps(existing), encoding="utf-8")


# ── Reddit 수집 ──────────────────────────────────────────
def get_reddit():
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        raise RuntimeError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET 환경변수가 설정되지 않았어요")
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )


@retry(times=3, base_delay=3, exceptions=(PrawcoreException,))
def fetch_candidates(reddit, seen_ids: set, pool_size: int = FETCH_POOL_SIZE):
    subreddit = reddit.subreddit(SUBREDDIT_NAME)
    posts = []
    for submission in subreddit.hot(limit=pool_size):
        if submission.stickied:
            continue
        if submission.id in seen_ids:
            continue
        posts.append(submission)
    return posts


@retry(times=2, base_delay=2, exceptions=(PrawcoreException,))
def get_top_comments(submission, n):
    submission.comment_sort = "top"
    submission.comments.replace_more(limit=0)
    return [c.body for c in submission.comments[:n] if hasattr(c, "body")]


def _minmax(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def score_and_collect(posts, valid_tickers):
    if not posts:
        return [], Counter()

    now = datetime.now(timezone.utc).timestamp()

    engagement_raw = [p.score + p.num_comments * 2 for p in posts]
    velocity_raw = [
        (p.score + p.num_comments * 2) / max((now - p.created_utc) / 3600, 0.5)
        for p in posts
    ]

    ticker_counter = Counter()
    post_tickers = []
    for p in posts:
        tickers = extract_tickers(p.title + " " + (p.selftext or ""), valid_tickers)
        post_tickers.append(tickers)
        ticker_counter.update(set(tickers))

    top_trending = {t for t, _ in ticker_counter.most_common(10)}
    engagement_norm = _minmax(engagement_raw)
    velocity_norm = _minmax(velocity_raw)

    scored = []
    for i, p in enumerate(posts):
        flair_bonus = 1.0 if p.link_flair_text in PRIORITY_FLAIRS else 0.0
        ticker_bonus = 1.0 if set(post_tickers[i]) & top_trending else 0.0
        final_score = (
            engagement_norm[i] * WEIGHTS["engagement"]
            + velocity_norm[i] * WEIGHTS["velocity"]
            + flair_bonus * WEIGHTS["flair"]
            + ticker_bonus * WEIGHTS["ticker_trend"]
        )
        scored.append((final_score, p, post_tickers[i]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:TOP_N_CANDIDATES], ticker_counter


def build_output(scored, ticker_counter):
    items = []
    for final_score, p, tickers in scored:
        try:
            top_comments = get_top_comments(p, TOP_COMMENTS_PER_POST)
        except PrawcoreException as e:
            log.warning(f"댓글 수집 실패, 이 게시물은 빈 댓글로 대체해요 (id={p.id}): {e}")
            top_comments = []

        items.append({
            "id": p.id,
            "title": p.title,
            "body": p.selftext if p.is_self else "",
            "url": p.url if not p.is_self else None,
            "permalink": f"https://reddit.com{p.permalink}",
            "created_kst": datetime.fromtimestamp(p.created_utc, tz=timezone.utc)
                .astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
            "num_comments": p.num_comments,
            "score": p.score,
            "upvote_ratio": p.upvote_ratio,
            "flair": p.link_flair_text,
            "tickers": sorted(set(tickers)),
            "top_comments": top_comments,
            "final_score": round(final_score, 4),
        })
    return {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "trending_tickers": ticker_counter.most_common(5),
        "candidates": items,
    }


# ── 실패 알림 ────────────────────────────────────────────
def send_failure_alert(error_message: str):
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import notify
        text = (
            f"⚠️ WSB 일일 브리핑 실패\n"
            f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}\n\n"
            f"에러: {error_message}"
        )
        notify.get_notifier().send(text)
        log.info("실패 알림 전송 완료")
    except Exception as e:
        log.error(f"실패 알림 전송도 실패함: {e}")


# ── 메인 ─────────────────────────────────────────────────
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

    reddit = get_reddit()
    posts = fetch_candidates(reddit, seen_ids)
    if len(posts) < TOP_N_CANDIDATES:
        log.warning(
            f"중복 제외 후 후보가 {len(posts)}개뿐이라 더 넓은 풀로 재시도해요"
        )
        posts = fetch_candidates(reddit, seen_ids, pool_size=FETCH_POOL_SIZE * 2)
    log.info(f"수집된 신규 후보(중복 제외): {len(posts)}개")

    scored, ticker_counter = score_and_collect(posts, valid_tickers)
    output = build_output(scored, ticker_counter)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info(
        f"완료: 후보 {len(output['candidates'])}개 → {OUTPUT_PATH} "
        f"(전송 완료 표시는 리포트 발송 성공 후 --mark-sent로 별도 진행)"
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
    finally:
        release_lock()


if __name__ == "__main__":
    main()
