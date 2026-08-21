---
name: rddt-daily-briefing
description: r/wallstreetbets의 오늘 주요 게시물을 선별해 한국어로 요약하고 텔레그램(또는 설정된 채널)으로 전송한다. cron으로 매일 자동 실행하기 위한 스킬.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_and_score.py *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/notify.py *) Read(/tmp/wsb_candidates.json) Write(/tmp/wsb_report.md)
---

# WSB 일일 브리핑

## 1단계: 후보 게시물 수집 + 점수 계산

다음 명령을 실행해 오늘의 후보 게시물을 가져와.
hot 순위/신선도/티커트렌드 3가지 기준으로 이미 점수가 계산되고,
최근 3일 내 이미 내보낸 게시물은 자동으로 제외된 상태로
`/tmp/wsb_candidates.json`에 점수 내림차순으로 저장돼:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_and_score.py
```

**이 명령이 0이 아닌 종료 코드를 반환하면 즉시 멈춰.** 스크립트가 이미
실패 알림을 전송했으니 너는 이후 단계를 진행하지 말고 무엇이 실패했는지
한 줄로만 보고해.

## 2단계: JSON 읽기

Read 도구로 `/tmp/wsb_candidates.json`을 읽어. 각 게시물 항목에는
id, title, body, url, permalink, author, created_kst, hot_rank,
tickers, final_score가 들어 있어. 최상위에는 trending_tickers
(티커별 언급수 목록)와 trending_source도 있어.

`hot_rank`는 Reddit hot 피드에서의 순위(1이 최상위)라 투표수/댓글수 대신
쓰는 인기 지표야. 투표수·댓글수·플레어는 이 수집 경로에서 제공되지 않아.
**id는 5단계에서 필요하니 상위 8개를 고를 때 따로 기억해둬.**

## 3단계: 상위 8개 게시물 요약 (네가 직접 작성)

점수 상위 8개를 골라 각각에 대해 한국어로 2~3문장 요약을 작성해.
- 투자 조언처럼 들리지 않게, "무슨 일이 있었는지 / 무슨 주장인지"만 담백하게 요약
- body가 비어 있으면(이미지/링크 게시물) 제목과 url에서 읽히는 것만 담백하게
  쓰고, 추측으로 내용을 지어내지 마. 짧아도 괜찮아
- 과장하거나 확정적 어조로 쓰지 말 것 (예: "폭등할 것" 대신 "폭등 가능성을 주장함")

## 4단계: 리포트 조립

아래 형식으로 마크다운 리포트를 작성해 `/tmp/wsb_report.md`에 Write 도구로 저장해:

```
📊 WSB 일일 브리핑 — {오늘 날짜, YYYY-MM-DD}

🔥 오늘 언급 많은 티커: {티커1}({횟수}), {티커2}({횟수}), ...

1. {제목}
🔥 hot #{hot_rank} · {created_kst}
{네가 쓴 2~3문장 요약}
{permalink}

2. {제목}
...
```

"오늘 언급 많은 티커"는 JSON 최상위의 `trending_tickers`를 그대로 써
(서브레딧 전체 집계라 후보 20개만 세는 것보다 정확해). 상위 5개까지만 넣어.

## 5단계: 전송

리포트를 전송해:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/notify.py /tmp/wsb_report.md
```

전송 채널은 notify.py가 `NOTIFY_CHANNEL` 환경변수(기본값 telegram)를 읽어서
자동으로 결정해.

**전송이 성공한 경우에만**, 2단계에서 기억해둔 2단계에서 고른 8개 게시물의
id로 "전송 완료" 표시를 남겨. 후보 20개 전체가 아니라 **실제로 리포트에
들어간 8개만** 표시해야 해 (표시 안 된 나머지는 내일 다시 후보로 나올 수
있어야 하니까):

```
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_and_score.py --mark-sent {id1} {id2} {id3} {id4} {id5} {id6} {id7} {id8}
```

notify.py가 실패했다면 이 단계는 건너뛰고(다음 실행 때 같은 글이 다시
후보에 오를 수 있게), 성공/실패 여부를 마지막에 한 줄로 알려줘.
