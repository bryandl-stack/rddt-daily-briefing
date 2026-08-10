# WSB 일일 브리핑 — Claude Code 스킬

## 설치

1. 이 폴더 전체를 개인 스킬 디렉토리로 복사:
   ```bash
   mkdir -p ~/.claude/skills
   cp -r rddt-daily-briefing ~/.claude/skills/
   ```

2. Python 패키지 설치:
   ```bash
   pip install requests
   ```

3. 텔레그램 봇 설정 — 둘 중 편한 쪽으로:

   **(A) `--set-token`으로 저장 (chat id 자동 탐지)**
   ```bash
   # 먼저 텔레그램에서 봇에게 아무 메시지나 한 번 보내세요.
   # 봇은 먼저 말을 걸 수 없어서, 이 메시지가 있어야 chat id가 생깁니다.
   python3 scripts/notify.py --set-token <BOT_TOKEN>
   ```
   토큰을 검증한 뒤 chat id를 자동으로 찾아 `~/.cache/wsb_briefing/telegram.json`
   (권한 0600)에 저장해요. 리포지토리에는 저장하지 않습니다.
   chat id를 직접 알고 있으면 `--set-token <BOT_TOKEN> <CHAT_ID>`로 넘겨도 돼요.

   **(B) 환경변수** (`~/.wsb_env` 같은 별도 파일로 분리 권장) — 이쪽이 우선합니다:
   ```bash
   export TELEGRAM_BOT_TOKEN="..."      # @BotFather 로 발급
   export TELEGRAM_CHAT_ID="..."
   export NOTIFY_CHANNEL="telegram"     # telegram / slack / file
   ```

4. Reddit 인증 — Reddit이 비인증 `.json` 접근을 403으로 막아서 둘 중 하나가 필요해요.
   요청 코드는 한 벌이고, 아래 환경변수 유무로 어느 쪽을 쓸지가 자동으로 갈립니다.

   **(A) 공식 API** — 승인받았다면 이쪽이 안정적이에요. `praw` 패키지는 필요 없고
   스크립트가 토큰을 직접 발급해요 (client_credentials, 읽기 전용):
   ```bash
   export REDDIT_CLIENT_ID="..."        # https://www.reddit.com/prefs/apps → script 타입
   export REDDIT_CLIENT_SECRET="..."
   export REDDIT_USERNAME="..."         # User-Agent 표기용. Reddit 정책상 넣어두는 게 안전
   ```

   **(B) 브라우저 쿠키** — API 승인 전 임시 방편:
   ```bash
   # F12 → Application → Cookies → https://www.reddit.com 에서 값 복사
   export REDDIT_COOKIES='{"loid":"...","session_tracker":"...","csv":"..."}'
   # 또는 같은 JSON을 ~/.cache/wsb_briefing/cookies.json 에 저장
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
- `cookies.json`: Reddit 쿠키 (API 키를 안 쓸 때 필수). 브라우저에서 로그인된
  상태의 `loid`, `session_tracker`, `csv`, `edgebucket` 등을 담아두면 돼요.
- `telegram.json`: `--set-token`으로 저장한 봇 토큰 + chat id (권한 0600).
  환경변수가 설정돼 있으면 그쪽이 우선합니다.
- `fetch.lock`: `flock`으로 잡는 중복 실행 방지용 락 파일. cron이 겹쳐
  돌아도 두 번째 실행은 즉시 종료돼요. 프로세스가 죽으면 커널이 락을 자동
  해제하므로 남은 파일을 손으로 지울 일은 없어요.

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
  바꾸면 돼요. 새 채널(이메일, 디스코드 등) 추가는 `scripts/notify.py`의
  `send()`에 `elif` 한 줄 추가로 끝나요.
- 파이프라인이 실패하면(Reddit 403, 티커 목록 다운로드 실패 등)
  `fetch_and_score.py`가 자체적으로 실패 알림을 전송하고 종료 코드 1을
  반환해요. cron 로그(`~/wsb_cron_error.log`)에서 원인을 확인하세요.
- `fcntl`을 쓰기 때문에 리눅스/macOS 전용이에요 (출력 경로도 `/tmp` 고정).
