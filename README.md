# WSB 일일 브리핑 — Claude Code 스킬

## 설치

1. 이 폴더 전체를 개인 스킬 디렉토리로 복사:
   ```bash
   mkdir -p ~/.claude/skills
   cp -r rddt-daily-briefing ~/.claude/skills/
   ```

2. Python 패키지 설치:
   ```bash
   pip install praw requests
   ```

3. 환경변수 설정 (`~/.wsb_env` 같은 별도 파일로 분리 권장):
   ```bash
   export REDDIT_CLIENT_ID="..."
   export REDDIT_CLIENT_SECRET="..."
   export REDDIT_USERNAME="..."         # User-Agent에 포함됨. Reddit API 정책상
                                         # 실제 계정명이 없으면 rate-limit/차단 위험이 있음
   export TELEGRAM_BOT_TOKEN="..."      # @BotFather 로 발급
   export TELEGRAM_CHAT_ID="..."        # 봇과 대화 후 getUpdates로 확인
   export NOTIFY_CHANNEL="telegram"     # telegram / slack / file
   ```

## 스킬 테스트 (수동 실행)

터미널에서 `claude`를 실행한 뒤:
```
/rddt-daily-briefing
```
정상 동작하면 텔레그램으로 리포트가 오고, `/tmp/wsb_candidates.json`과
`/tmp/wsb_report.md`가 생성된 걸 확인할 수 있어요.

## cron 등록 (비대화형 실행)

`disable-model-invocation: true`로 설정되어 있어서 프롬프트에 스킬 이름을
직접 넣어 호출해야 해요:

```bash
crontab -e
```

다음 줄 추가 (매일 오전 8시 KST = UTC 기준 전날 23시, 서버가 UTC 기준일 때):

```
0 23 * * * source ~/.wsb_env && cd ~ && \
  /usr/local/bin/claude -p "/rddt-daily-briefing" \
  --allowedTools "Bash,Read,Write" \
  --permission-mode acceptEdits \
  --model claude-sonnet-5 \
  --output-format json \
  | tee -a ~/wsb_cron_raw.log \
  | jq -r '"[" + (now | strftime("%Y-%m-%d %H:%M:%S")) + "] cost=$" + (.total_cost_usd|tostring)' \
  >> ~/wsb_cost.log 2>> ~/wsb_cron_error.log
```

- `--model claude-sonnet-5`로 모델을 고정하면 실행마다 기본 모델이 바뀌는
  걸 막고 비용을 예측 가능하게 만들어요. 더 가벼운 모델로 충분하다고
  판단되면 값만 바꾸면 돼요.
- `--output-format json` + `jq`로 매 실행의 `total_cost_usd`를
  `~/wsb_cost.log`에 누적해서 비용 추이를 볼 수 있어요. `jq` 설치 필요
  (`apt install jq` 등).
- 원본 출력 전체는 `~/wsb_cron_raw.log`, stderr는 `~/wsb_cron_error.log`로
  분리해뒀어요.

## 캐시 / 상태 파일

`~/.cache/wsb_briefing/`에 파일이 자동 생성돼요:
- `tickers_cache.json`: NASDAQ/NYSE/AMEX 전체 티커 목록 (주 1회 자동 갱신)
- `sent_ids.json`: 최근 3일간 **실제로 리포트에 포함되어 전송된** 게시물 ID
  (중복 방지용). `fetch_and_score.py`는 더 이상 후보 20개를 전부 자동으로
  여기 기록하지 않아요 — SKILL.md 5단계에서 텔레그램 전송이 성공한 뒤,
  실제로 리포트에 실린 8개만 `fetch_and_score.py --mark-sent <id...>`로
  명시적으로 기록해요. 그래야 상위 8개에 못 든 후보가 다음 날 다시 나올 수
  있고, 전송이 실패한 날은 그 게시물들이 "보냄" 처리되지 않아요.
- `fetch.lock`: 실행 중임을 표시하는 lock 파일. cron이 겹쳐 돌아도
  두 번째 실행은 즉시 종료돼요 (30분 넘게 남아있으면 죽은 프로세스로 보고
  자동 정리). 정상 종료 시 자동으로 지워지므로 평소엔 안 보여요.

문제가 생기면 이 디렉토리를 지우고 다시 실행해도 안전해요. 다음 실행 때
자동으로 재생성됩니다.

## 주의할 점

- **`--bare` 플래그를 쓰지 마세요.** bare 모드는 스킬 자동 탐색을 건너뛰기
  때문에 `~/.claude/skills/`에 있는 이 스킬을 찾지 못해요.
- **인증**: cron은 로그인 세션이 없는 환경에서 돌 수 있어서, Claude
  구독 로그인이 아니라 `ANTHROPIC_API_KEY` 환경변수로 인증하는 걸 권장해요
  (API 사용량만큼 별도 과금됩니다).
- **`--allowedTools`**를 지정 안 하면 매번 권한 확인 프롬프트가 떠서
  cron에서는 응답 없이 멈춰요. 위 예시처럼 명시적으로 열어줘야 해요.
- 스킬 안의 `allowed-tools`는 `python3 .../fetch_and_score.py`,
  `python3 .../notify.py`, 그리고 `/tmp/wsb_candidates.json` 읽기 /
  `/tmp/wsb_report.md` 쓰기만 사전 승인해요 (임의 경로의 Read/Write는 열려
  있지 않아요). CLI의 `--allowedTools "Bash,Read,Write"`는 세션 전체에
  대한 상위 허용이고, 실제로 어떤 파일을 건드릴 수 있는지는 스킬의
  `allowed-tools`가 더 좁게 제한해요.
- 채널을 텔레그램에서 다른 걸로 바꾸려면 `NOTIFY_CHANNEL` 환경변수만
  바꾸면 돼요. 새 채널(이메일, 디스코드 등) 추가는
  `scripts/notify.py`에 클래스 하나 추가 + `get_notifier()` 분기 추가로 끝나요.
- 파이프라인이 실패하면(Reddit API 장애, 티커 목록 다운로드 실패 등)
  `fetch_and_score.py`가 자체적으로 실패 알림을 전송하고 종료 코드 1을
  반환해요. cron 로그(`~/wsb_cron_error.log`)에서 원인을 확인하세요.
