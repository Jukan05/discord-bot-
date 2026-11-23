# 공시·뉴스 키워드 Discord 알림 봇

DART 전자공시와 Google 뉴스에서 지정한 키워드가 나오면 Discord 채널로 알림을 보내는 경량 봇입니다.
요약은 **Claude 구독(Pro/Max)** 으로 Claude Code CLI를 헤드리스 실행해서 만들기 때문에 API 키가 필요 없습니다.
GitHub Actions cron으로 30분마다 실행되며, 서버를 따로 띄우지 않습니다.

```
GitHub Actions (cron) ─▶ DART list.json ─┐
                      ─▶ Google 뉴스 RSS ─┼─▶ 키워드 매칭 ─▶ 중복 제거(state/seen.json)
                                          │        └─▶ 공시 원문 → claude -p (구독) 요약
                                          └────────────────────▶ Discord 웹훅 (embed)
```

## 준비물 (3가지 값)

| Secret 이름 | 얻는 방법 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord 채널 ⚙️ 편집 → 연동 → 웹후크 → 새 웹후크 → **웹후크 URL 복사** |
| `DART_API_KEY` | [opendart.fss.or.kr](https://opendart.fss.or.kr) 회원가입 → 인증키 신청 (무료, 하루 20,000건) |
| `CLAUDE_CODE_OAUTH_TOKEN` | 로컬 터미널에서 아래 두 명령 실행 → 구독 계정으로 로그인하면 토큰이 출력됨 |

```bash
npm install -g @anthropic-ai/claude-code
```

```bash
claude setup-token
```

> 요약에 쓰는 사용량은 구독 플랜 한도에서 차감됩니다. 기본 모델은 `haiku`이고, 본문이 있는 항목(주로 공시)만 요약하므로 사용량은 적습니다.
> 토큰이 없거나 요약에 실패하면 요약 없이 제목·링크만 보냅니다.

## 배포 (GitHub Actions)

1. 이 폴더를 GitHub 레포로 push 합니다.
2. 레포 **Settings → Secrets and variables → Actions** 에 위 3개 값을 등록합니다.
3. **Actions 탭 → alert-bot → Run workflow** 로 한 번 수동 실행합니다.
   - 첫 실행은 **시드 모드**입니다. 현재 있는 공시와 뉴스를 "이미 본 것"으로만 기록하고 알림은 보내지 않습니다(알림 폭탄 방지).
   - 이후 30분마다 새 항목만 전송합니다.

**실행 간격**: private 레포의 무료 Actions 한도(월 2,000분)에 맞춰 30분 간격으로 설정했습니다.
레포를 public으로 바꾸면 Actions가 무제한 무료라서 `.github/workflows/alert.yml`의 cron을 `*/15 * * * *`로 줄여도 됩니다(Secret은 공개되지 않지만 `config.yaml` 키워드와 `state/seen.json`은 보입니다).

## 설정 (`config.yaml`)

- `keywords`: 공시는 "회사명 + 보고서명", 뉴스는 Google 뉴스 검색어로 매칭합니다.
- `exclude_keywords`: 이 단어가 들어간 항목은 제외합니다(예: `기재정정`).
- `dart.watch_corps`: 키워드와 상관없이 모든 공시를 받을 회사(회사명 또는 종목코드).
- `dart.markets`: `Y` 유가증권, `K` 코스닥, `N` 코넥스, `E` 기타.
- `summary.backend`: `claude_cli`(구독 요약) 또는 `none`.

설정을 바꾸고 push 하면 다음 실행부터 반영됩니다.

## 로컬 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env   # 값 채우기
```

```bash
.venv/bin/python -m alertbot.main --dry-run --no-seed
```

- `--dry-run`: Discord로 보내지 않고 콘솔에만 출력하며, 상태도 저장하지 않습니다.
- `--seed`: 현재 항목을 본 것으로만 기록합니다.
- 로컬에서 `claude`에 로그인되어 있으면 토큰 없이도 요약됩니다.

## 동작 방식 메모

- **DART**: 접수번호(rcept_no)가 시간순으로 증가하는 점을 이용합니다. 최신순으로 페이지를 넘기다가 지난번 위치(watermark)를 지나면 1페이지 더 보고 멈춥니다. 그래서 평소에는 실행당 API 호출이 2회 정도입니다. 늦게 목록에 올라온 공시도 그 여분 페이지에서 잡힙니다.
- **중복 방지**: 보낸 항목 ID는 `state/seen.json`에 14일간 보관하고, 워크플로가 매 실행 후 커밋합니다.
- **전송 실패·건수 초과**: 보내지 못한 항목은 seen에 기록하지 않으므로 다음 실행에서 다시 보냅니다(`max_items_per_run` 기본 20).
- **뉴스 요약 한계**: Google 뉴스 RSS는 대부분 제목만 주기 때문에, 제목만 있는 뉴스는 요약하지 않고 그대로 보냅니다.
- **보안**: 요약용 Claude는 도구를 모두 끈 상태(`--tools ""`)로 실행되므로, 기사나 공시 본문에 이상한 지시가 들어 있어도 명령을 실행할 수 없습니다.
