"""
r/wallstreetbets 후보 게시물 수집 + 점수 계산
→ /tmp/wsb_candidates.json 에 점수 내림차순으로 저장

수집 경로: Reddit 공식 Atom 피드 + apewisdom 티커 집계 (둘 다 인증 불필요).
  Reddit API(OAuth)는 데이터센터 IP에서 개발자 토큰 없이는 403이라 못 씀.
  RSS는 같은 IP에서 토큰 없이 열리지만 rate limit이 빡빡해서(토큰 버킷,
  60초를 띄워도 429가 남) 요청을 하루 1회 1건으로 줄이고 재시도를 길게 둠.

RSS로 얻는 것: id, 제목, 본문, 작성자, 작성시각, permalink, hot 순위
RSS에 없는 것: score, num_comments, upvote_ratio, 플레어
  → hot 피드의 "순서" 자체가 Reddit의 랭킹(투표수+참여속도 반영)이라
    engagement/velocity 대용으로 씀. 최근에 올라왔는데 이미 상위 =
    빠르게 뜨는 글이므로 hot 순위 × 신선도로 velocity가 사실상 복원됨.

점수 기준(가중치):
  - hot_rank    0.50  피드에서의 위치 (Reddit 자체 랭킹)
  - freshness   0.25  published 기준 신선도
  - ticker_trend 0.25 apewisdom 언급수 + 24시간 순위 변동(모멘텀)
                      apewisdom 실패 시 로컬 티커 카운트로 폴백

기존 유지 사항:
  - 티커 인식: 실제 NASDAQ/NYSE/AMEX 전체 티커 목록과 대조 (주 1회 캐시)
  - 네트워크 호출에 재시도(exponential backoff) 적용
  - 최근 N일간 이미 내보낸 게시물 ID는 후보에서 제외 (중복 방지)
  - "내보냄" 표시는 리포트에 실제로 들어간 글만 `--mark-sent`로 별도 표시
  - 티커 오탐 감소: 흔한 영단어와 겹치는 티커는 `$` 접두사가 있을 때만 인정
  - cron 중복 실행 방지용 lock 파일
  - 파이프라인 실패 시 notify.py를 통해 실패 알림 전송

사전 준비:
  pip install requests          # praw 불필요
  (Reddit 자격증명 불필요. 텔레그램 관련 환경변수는 notify.py 참고)

사용법:
  python3 fetch_and_score.py                    # 후보 수집 + 점수 계산
  python3 fetch_and_score.py --mark-sent ID...  # 지정한 게시물 id를 "전송 완료"로 표시
"""

import os
import re
import sys
import html
import json
import math
import time
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests

# ── 로깅 설정 ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wsb_fetch")

# ── 설정 ─────────────────────────────────────────────────
SUBREDDIT_NAME = "wallstreetbets"
FETCH_POOL_SIZE = 100
TOP_N_CANDIDATES = 20
OUTPUT_PATH = "/tmp/wsb_candidates.json"

RSS_URL = f"https://www.reddit.com/r/{SUBREDDIT_NAME}/hot/.rss?limit={FETCH_POOL_SIZE}"
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1"
# 데이터센터 IP에서 기본 UA로 가면 봇 탐지에 더 잘 걸림
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CACHE_DIR = Path.home() / ".cache" / "wsb_briefing"
TICKER_CACHE_PATH = CACHE_DIR / "tickers_cache.json"
SENT_IDS_PATH = CACHE_DIR / "sent_ids.json"
LOCK_PATH = CACHE_DIR / "fetch.lock"
TICKER_LIST_URL = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
TICKER_CACHE_MAX_AGE_DAYS = 7
SENT_ID_RETENTION_DAYS = 3   # 이 기간 내 표시된 글은 다시 후보에 안 올림
LOCK_STALE_SECONDS = 30 * 60  # 이보다 오래된 lock은 죽은 프로세스로 간주하고 정리

WEIGHTS = {"hot_rank": 0.50, "freshness": 0.25, "ticker_trend": 0.25}

# hot 순위 → 점수 감쇠 상수. Reddit의 hot 점수는 순위에 따라 지수적으로
# 떨어지는데 순위를 선형 정규화하면 상위권이 뭉개져서(4위와 16위 차이가
# 0.07밖에 안 됨) 변별력이 없다. exp(-(rank-1)/K)로 상위권을 벌린다.
#   K=20 기준: 1위 1.00 / 5위 0.82 / 10위 0.64 / 20위 0.39 / 50위 0.09
HOT_RANK_DECAY = 20.0

# apewisdom 상위 몇 개까지를 "트렌딩"으로 볼지, 순위가 이만큼 뛰면 모멘텀 가점
APEWISDOM_TOP_N = 25
MOMENTUM_RANK_JUMP = 5
MOMENTUM_BONUS = 0.2

# 매일 고정으로 올라오는 스티키 메가스레드. RSS엔 stickied 플래그가 없어서
# 제목 패턴으로 거른다 (안 거르면 항상 hot 최상단을 차지해 후보를 잡아먹음)
MEGATHREAD_PATTERN = re.compile(
    r"^\s*(daily discussion thread|weekly earnings thread|what are your moves"
    r"|most anticipated earnings|daily thread|weekend discussion"
    r"|moves tomorrow)",
    re.IGNORECASE,
)

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

# 흔한 영단어/약어와 우연히 겹치는 실제 티커들. $ 접두사 없이 등장하면
# 본문이 그냥 그 단어를 쓴 것일 확률이 높아서, $ 없이는 티커로 인정하지 않음
AMBIGUOUS_WORD_TICKERS = {
    # 일반 영단어
    "A", "I", "IT", "SO", "ON", "GO", "ALL", "ARE", "CAT", "KEY", "NOW",
    "FOR", "ONE", "CAN", "NEW", "DAY", "LOW", "OPEN", "REAL", "WELL",
    "GOOD", "PLAY", "FAST", "TRUE", "NEXT", "FREE", "SAFE", "EASY",
    "MOVE", "GAIN", "LOSS", "HOLD", "SELL", "BUY", "CALL", "PUT", "RUN",
    "YOU", "HERE",
    # 금융/WSB 약어 — 실제 티커지만 본문에선 약어로 쓰이는 쪽이 압도적
    #   FCF=free cash flow, COO=최고운영책임자, IMO=in my opinion 등
    #   (LNG는 Cheniere Energy 티커로 정상 언급이 많아 일부러 뺐음)
    "FCF", "IMO", "COO", "CEO", "CFO", "EPS", "ATH", "IPO", "ROI",
    "GDP", "CPI", "FED", "ETF", "SEC", "IRS", "USA", "YOLO", "TLDR",
    "EOD", "OTM", "ITM",
}

# 1~2글자 토큰은 오탐이 압도적이라("S&P 500"이 S와 P로 쪼개지는 등)
# $ 접두사를 요구한다. 단 apewisdom 상위권(= trend_scores 키)에 있으면
# 실제로 활발히 거론되는 티커이므로 예외로 인정한다 (예: MU 145회).
SHORT_TICKER_MAX_LEN = 2

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
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


def extract_tickers(text: str, valid_tickers: set, short_whitelist=None):
    """본문에서 티커를 뽑는다.

    short_whitelist: $ 없이도 인정할 1~2글자 티커 집합 (apewisdom 상위권).
      None이면 1~2글자는 $ 접두사가 있을 때만 인정한다.
    """
    text = text or ""
    short_ok = short_whitelist or set()
    dollar_hits = set(TICKER_DOLLAR_PATTERN.findall(text))
    bare_hits = {
        t for t in TICKER_BARE_PATTERN.findall(text)
        if t not in AMBIGUOUS_WORD_TICKERS
        and (len(t) > SHORT_TICKER_MAX_LEN or t in short_ok)
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


# ── RSS 파싱 헬퍼 ────────────────────────────────────────
def _strip_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html.unescape(text).split())


def _parse_body(content_html: str) -> str:
    """셀프포스트 본문만 뽑는다. 링크/이미지 게시물이면 빈 문자열.

    Reddit Atom의 content는 <table>로 감싼 미리보기 + 작성자/링크 푸터인데,
    셀프포스트일 때만 그 안에 <!-- SC_OFF --><div class="md">본문</div>
    <!-- SC_ON --> 이 끼어 있다.
    """
    m = re.search(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", content_html, re.S)
    return _strip_html(m.group(1)) if m else ""


def _parse_external_url(content_html: str, permalink: str):
    """링크 게시물의 외부 URL. 셀프포스트면 None."""
    m = re.search(r'<a href="([^"]+)">\s*\[link\]\s*</a>', content_html)
    if not m:
        return None
    url = html.unescape(m.group(1))
    # 셀프포스트는 [link]가 자기 permalink를 가리킨다
    return None if url.rstrip("/") == permalink.rstrip("/") else url


@retry(times=6, base_delay=15, exceptions=(requests.RequestException,))
def _download_rss() -> str:
    resp = requests.get(
        RSS_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, text/xml"},
        timeout=25,
    )
    # 429가 잦다. raise_for_status가 HTTPError를 던져서 위 retry가 백오프함
    resp.raise_for_status()
    if "<entry" not in resp.text:
        raise ValueError("피드에 entry가 없음 (차단 페이지를 받았을 가능성)")
    return resp.text


def fetch_posts(seen_ids: set) -> list:
    """hot 피드를 파싱해 후보 게시물 목록을 만든다 (hot 순위 포함)."""
    feed = _download_rss()
    entries = ET.fromstring(feed).findall("a:entry", ATOM_NS)
    log.info(f"RSS 수신: entry {len(entries)}개")

    posts = []
    skipped_mega = 0
    for rank, e in enumerate(entries):
        def txt(tag):
            node = e.find(f"a:{tag}", ATOM_NS)
            return (node.text or "").strip() if node is not None else ""

        # RSS의 id는 't3_1vocawt' 형식. praw 시절 sent_ids와 --mark-sent가
        # 접두사 없는 '1vocawt'를 쓰므로 벗겨서 통일한다
        post_id = txt("id").removeprefix("t3_")
        title = txt("title")
        if not post_id or not title:
            continue
        if MEGATHREAD_PATTERN.match(title):
            skipped_mega += 1
            continue
        if post_id in seen_ids:
            continue

        link_node = e.find("a:link", ATOM_NS)
        permalink = link_node.attrib.get("href", "") if link_node is not None else ""
        author_node = e.find("a:author/a:name", ATOM_NS)
        content_node = e.find("a:content", ATOM_NS)
        content_html = html.unescape(content_node.text or "") if content_node is not None else ""

        published = txt("published") or txt("updated")
        try:
            created = datetime.fromisoformat(published)
        except ValueError:
            created = datetime.now(timezone.utc)

        posts.append({
            "id": post_id,
            "title": html.unescape(title),
            "body": _parse_body(content_html),
            "url": _parse_external_url(content_html, permalink),
            "permalink": permalink,
            "author": (author_node.text or "").strip() if author_node is not None else "",
            "created": created,
            "hot_rank": rank + 1,
        })

    log.info(
        f"메가스레드 제외 {skipped_mega}개 / 중복 제외 후 신규 후보 {len(posts)}개"
    )
    return posts


# ── apewisdom 티커 트렌드 ────────────────────────────────
@retry(times=3, base_delay=3, exceptions=(requests.RequestException,))
def _download_apewisdom() -> dict:
    resp = requests.get(APEWISDOM_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise ValueError("apewisdom 응답에 results가 비어 있음")
    return {
        r["ticker"]: {
            "mentions": int(r.get("mentions") or 0),
            "upvotes": int(r.get("upvotes") or 0),
            "rank": int(r.get("rank") or 0),
            "rank_24h_ago": int(r["rank_24h_ago"]) if r.get("rank_24h_ago") else None,
        }
        for r in results
    }


def load_ticker_trend():
    """{티커: 0~1 점수} 와 상위 티커 목록. 실패하면 (None, None)."""
    try:
        data = _download_apewisdom()
    except Exception as e:
        log.warning(f"apewisdom 조회 실패, 로컬 티커 카운트로 폴백해요: {e}")
        return None, None

    top = sorted(data.items(), key=lambda kv: kv[1]["mentions"], reverse=True)[:APEWISDOM_TOP_N]
    if not top:
        return None, None

    max_mentions = max(v["mentions"] for _, v in top) or 1
    scores = {}
    for ticker, v in top:
        score = v["mentions"] / max_mentions
        prev = v["rank_24h_ago"]
        if prev is not None and prev - v["rank"] >= MOMENTUM_RANK_JUMP:
            score = min(1.0, score + MOMENTUM_BONUS)
        scores[ticker] = score

    log.info(
        f"apewisdom 티커 트렌드 로드: 상위 {len(scores)}개 "
        f"(1위 {top[0][0]} {top[0][1]['mentions']}회)"
    )
    return scores, [(t, v["mentions"]) for t, v in top]


# ── 점수 계산 ────────────────────────────────────────────
def _minmax(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def score_and_collect(posts, valid_tickers, trend_scores):
    if not posts:
        return [], Counter()

    now = datetime.now(timezone.utc)

    # hot 순위: 지수 감쇠. minmax를 쓰지 않는 이유는 위 HOT_RANK_DECAY 주석 참고
    # (이미 0~1이고, "몇 위인가"의 절대적 의미를 보존해야 함)
    hot_norm = [math.exp(-(p["hot_rank"] - 1) / HOT_RANK_DECAY) for p in posts]
    # 신선도: 오래될수록 낮게
    age_hours = [max((now - p["created"]).total_seconds() / 3600, 0.0) for p in posts]
    fresh_norm = _minmax([-a for a in age_hours])

    # apewisdom 상위권은 1~2글자 티커의 $ 생략을 허용하는 화이트리스트로도 쓴다
    short_whitelist = set(trend_scores) if trend_scores else None

    ticker_counter = Counter()
    post_tickers = []
    for p in posts:
        tickers = extract_tickers(
            p["title"] + " " + p["body"], valid_tickers, short_whitelist
        )
        post_tickers.append(tickers)
        ticker_counter.update(set(tickers))

    # apewisdom이 죽었으면 로컬 카운트 상위 10개를 트렌딩으로 간주 (구 동작)
    local_top = {t for t, _ in ticker_counter.most_common(10)}

    scored = []
    for i, p in enumerate(posts):
        if trend_scores:
            ticker_bonus = max(
                (trend_scores.get(t, 0.0) for t in post_tickers[i]), default=0.0
            )
        else:
            ticker_bonus = 1.0 if set(post_tickers[i]) & local_top else 0.0

        final_score = (
            hot_norm[i] * WEIGHTS["hot_rank"]
            + fresh_norm[i] * WEIGHTS["freshness"]
            + ticker_bonus * WEIGHTS["ticker_trend"]
        )
        scored.append((final_score, p, post_tickers[i]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:TOP_N_CANDIDATES], ticker_counter


def build_output(scored, ticker_counter, trending):
    items = []
    for final_score, p, tickers in scored:
        items.append({
            "id": p["id"],
            "title": p["title"],
            "body": p["body"],
            "url": p["url"],
            "permalink": p["permalink"],
            "author": p["author"],
            "created_kst": p["created"].astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
            "hot_rank": p["hot_rank"],
            "tickers": sorted(set(tickers)),
            "final_score": round(final_score, 4),
        })
    return {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "trending_tickers": trending if trending else ticker_counter.most_common(5),
        "trending_source": "apewisdom" if trending else "local",
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

    posts = fetch_posts(seen_ids)
    if not posts:
        raise RuntimeError("후보 게시물이 하나도 없어요 (피드가 비었거나 전부 중복)")

    trend_scores, trending = load_ticker_trend()
    scored, ticker_counter = score_and_collect(posts, valid_tickers, trend_scores)
    output = build_output(scored, ticker_counter, trending)

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
