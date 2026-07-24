"""
리포트 파일을 읽어 설정된 채널로 전송.

CLI 사용법: python3 notify.py <report_file_path>
라이브러리 사용: from notify import get_notifier; get_notifier().send("...")
  (fetch_and_score.py가 실패 알림을 보낼 때 이렇게 임포트해서 씀)

채널 선택: NOTIFY_CHANNEL 환경변수 (기본값: telegram)
  - telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 필요
  - slack:    SLACK_WEBHOOK_URL 필요
  - file:     그냥 로컬 파일에 누적 저장 (테스트용)

개선 사항:
  - 텔레그램 parse_mode=Markdown 제거. 게시물 제목에 _ * [ ] 등이 섞이면
    레거시 Markdown 파서가 400 에러를 내면서 조용히 실패하는 문제가 있었음.
    plain text로 전송하고, 링크는 그냥 URL 문자열로 노출.
  - logging 모듈로 구조화된 로그 출력.

새 채널을 추가하려면 Notifier를 상속한 클래스를 만들고
get_notifier()에 분기만 추가하면 됨.
"""

import os
import sys
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wsb_notify")


class Notifier:
    def send(self, text: str) -> bool:
        raise NotImplementedError


class TelegramNotifier(Notifier):
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않았어요")
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        ok = True
        for chunk in _split_text(text, 3800):
            try:
                resp = requests.post(url, data={
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                    # parse_mode 의도적으로 생략: 게시물 제목의 특수문자로 인한
                    # Markdown 파싱 실패를 막기 위해 plain text로 전송
                }, timeout=15)
                if resp.status_code != 200:
                    log.error(f"텔레그램 전송 실패 ({resp.status_code}): {resp.text}")
                    ok = False
            except requests.RequestException as e:
                log.error(f"텔레그램 요청 예외: {e}")
                ok = False
        return ok


class SlackNotifier(Notifier):
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send(self, text: str) -> bool:
        if not self.webhook_url:
            log.error("SLACK_WEBHOOK_URL이 설정되지 않았어요")
            return False

        ok = True
        for chunk in _split_text(text, 3800):
            try:
                resp = requests.post(self.webhook_url, json={"text": chunk}, timeout=15)
                if resp.status_code != 200:
                    log.error(f"슬랙 전송 실패 ({resp.status_code}): {resp.text}")
                    ok = False
            except requests.RequestException as e:
                log.error(f"슬랙 요청 예외: {e}")
                ok = False
        return ok


class FileNotifier(Notifier):
    def __init__(self, path="wsb_daily_report_archive.md"):
        self.path = path

    def send(self, text: str) -> bool:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"\n\n{'='*50}\n{text}")
            return True
        except OSError as e:
            log.error(f"파일 저장 실패: {e}")
            return False


def get_notifier() -> Notifier:
    channel = os.environ.get("NOTIFY_CHANNEL", "telegram")
    if channel == "telegram":
        return TelegramNotifier(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
        )
    elif channel == "slack":
        return SlackNotifier(os.environ.get("SLACK_WEBHOOK_URL", ""))
    elif channel == "file":
        return FileNotifier()
    else:
        raise ValueError(f"알 수 없는 채널: {channel}")


def _split_text(text: str, limit: int):
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:]
    chunks.append(text)
    return chunks


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 notify.py <report_file_path>", file=sys.stderr)
        sys.exit(1)

    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        log.error(f"리포트 파일을 읽을 수 없어요: {e}")
        sys.exit(1)

    notifier = get_notifier()
    ok = notifier.send(text)
    if ok:
        log.info("전송 완료")
    else:
        log.error("전송 실패")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
