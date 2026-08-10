"""
리포트 파일을 읽어 설정된 채널로 전송.

CLI 사용법:
  python3 notify.py <report_file_path>        # 리포트 전송
  python3 notify.py --set-token <BOT_TOKEN>   # 봇 토큰 저장 (chat id는 자동 탐지)
라이브러리 사용: import notify; notify.send("...")
  (fetch_and_score.py가 실패 알림을 보낼 때 이렇게 임포트해서 씀)

채널 선택: NOTIFY_CHANNEL 환경변수 (기본값: telegram)
  - telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 필요
  - slack:    SLACK_WEBHOOK_URL 필요
  - file:     그냥 로컬 파일에 누적 저장 (테스트용)

텔레그램 자격증명은 환경변수가 우선이고, 없으면 --set-token으로 저장해둔
~/.cache/wsb_briefing/telegram.json 을 씁니다. 리포지토리에는 저장하지 않아요.

텔레그램은 parse_mode 없이 plain text로 보냄. 게시물 제목에 _ * [ ] 등이
섞이면 레거시 Markdown 파서가 400을 내면서 조용히 실패하기 때문.
"""

import os
import sys
import json
import stat
import logging
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wsb_notify")

CHUNK_LIMIT  = 3800
ARCHIVE_PATH = "wsb_daily_report_archive.md"
CONFIG_PATH  = Path.home() / ".cache" / "wsb_briefing" / "telegram.json"


def _telegram_creds() -> tuple[str, str]:
    """환경변수 우선, 없으면 --set-token으로 저장해둔 파일."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if (not token or not chat_id) and CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        token = token or saved.get("token", "")
        chat_id = chat_id or str(saved.get("chat_id", ""))
    return token, chat_id


def set_token(token: str, chat_id: str = ""):
    """봇 토큰을 검증해 저장. chat_id를 안 주면 getUpdates로 자동 탐지."""
    me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    if not me.get("ok"):
        log.error(f"토큰이 유효하지 않아요: {me.get('description', me)}")
        sys.exit(1)
    log.info(f"봇 확인: @{me['result']['username']}")

    if not chat_id:
        updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
        chats = [
            (u.get("message") or u.get("channel_post") or {}).get("chat", {}).get("id")
            for u in updates.get("result", [])
        ]
        chat_id = next((str(c) for c in reversed(chats) if c), "")
        if not chat_id:
            log.error(
                f"chat id를 찾지 못했어요. 텔레그램에서 @{me['result']['username']} 에게 "
                "아무 메시지나 한 번 보낸 뒤 다시 실행해주세요 "
                "(봇은 먼저 말을 걸 수 없어요)."
            )
            sys.exit(1)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"token": token, "chat_id": chat_id}), encoding="utf-8")
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    log.info(f"저장 완료: {CONFIG_PATH} (chat_id={chat_id})")


def _split_text(text: str, limit: int) -> list:
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:]
    chunks.append(text)
    return chunks


def send(text: str) -> bool:
    """NOTIFY_CHANNEL이 가리키는 채널로 전송. 새 채널은 여기 elif 한 줄로 추가."""
    channel = os.environ.get("NOTIFY_CHANNEL", "telegram")
    chunks = _split_text(text, CHUNK_LIMIT)

    if channel == "file":
        with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n\n{'=' * 50}\n{text}")
        return True

    if channel == "telegram":
        token, chat_id = _telegram_creds()
        if not token or not chat_id:
            log.error(
                "텔레그램 자격증명이 없어요. 환경변수(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)를 "
                "설정하거나 `python3 notify.py --set-token <BOT_TOKEN>`으로 저장해주세요"
            )
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payloads = [
            {"data": {"chat_id": chat_id, "text": c, "disable_web_page_preview": True}}
            for c in chunks
        ]
    elif channel == "slack":
        url = os.environ.get("SLACK_WEBHOOK_URL", "")
        if not url:
            log.error("SLACK_WEBHOOK_URL이 설정되지 않았어요")
            return False
        payloads = [{"json": {"text": c}} for c in chunks]
    else:
        raise ValueError(f"알 수 없는 채널: {channel}")

    ok = True
    for payload in payloads:
        try:
            resp = requests.post(url, timeout=15, **payload)
            if resp.status_code != 200:
                log.error(f"{channel} 전송 실패 ({resp.status_code}): {resp.text}")
                ok = False
        except requests.RequestException as e:
            log.error(f"{channel} 요청 예외: {e}")
            ok = False
    return ok


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 notify.py <report_file_path>\n"
              "        python3 notify.py --set-token <BOT_TOKEN> [CHAT_ID]", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--set-token":
        if len(sys.argv) < 3:
            print("사용법: python3 notify.py --set-token <BOT_TOKEN> [CHAT_ID]", file=sys.stderr)
            sys.exit(1)
        set_token(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        return

    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        log.error(f"리포트 파일을 읽을 수 없어요: {e}")
        sys.exit(1)

    ok = send(text)
    log.info("전송 완료") if ok else log.error("전송 실패")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
