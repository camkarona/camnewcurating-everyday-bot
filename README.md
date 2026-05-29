# 까로나 뉴스봇 MVP 1 — 캄보디아 뉴스 브리핑 봇

매일 오전 11시, Google News RSS에서 캄보디아 관련 뉴스를 수집해
Gemini로 요약·중요도 분류한 뒤 텔레그램으로 브리핑을 보냅니다.

> 자동 발행이 아닌 **편집장 보조** 도구입니다. (사람의 판단 유지)

---

## 파일 구성

| 파일 | 설명 |
|------|------|
| `camnewcurating_everyday_bot.py` | 올인원 메인 스크립트 (수집→요약→전송) |
| `.env.example` | 환경변수 템플릿 (복사해서 `.env`로 사용) |
| `requirements.txt` | 의존 패키지 |
| `cambodia_news_bot_design.md` | 원본 설계 문서 |

---

## 1. 설치

```bash
pip install -r requirements.txt
```

> 설계 문서의 `google-generativeai` 대신 신버전 통합 SDK `google-genai`를 사용합니다.
> (모델 `gemini-3-flash-preview`가 신버전 SDK에서만 안정적으로 동작하기 때문)

## 2. 환경변수 설정

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

`.env`를 열어 값을 채웁니다.

| 변수 | 설명 |
|------|------|
| `GEMINI_API_KEY` | Gemini API 키 (기존 /go 봇 것 재사용 가능) |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 전송 대상 chat_id (개인 채팅 / 비공개 그룹) |
| `GEMINI_MODEL` | (선택) 기본값 `gemini-3-flash-preview` |

**chat_id 확인법**: 봇에게 메시지를 한 번 보낸 뒤
`https://api.telegram.org/bot<봇토큰>/getUpdates` 접속 → `chat.id` 확인.

## 3. 수동 실행 (테스트)

```bash
python3 camnewcurating_everyday_bot.py
```

## 4. 자동 실행 (AWS Lightsail cron)

```bash
crontab -e
```

아래 한 줄 추가 (매일 오전 11시):

```cron
0 11 * * * cd /home/ubuntu/news_bot && /usr/bin/python3 camnewcurating_everyday_bot.py >> /home/ubuntu/news_bot/bot.log 2>&1
```

> cron은 서버 시간대를 따릅니다. 서버가 UTC면 KST 11시는 `0 2 * * *`.
> 메시지 본문의 표시 시각은 코드에서 KST로 고정되어 있습니다.

---

## 수집/출력 설정 바꾸기

`camnewcurating_everyday_bot.py` 상단에서 조정합니다.

- `RSS_SOURCES` — RSS 소스/그룹 추가·수정
- `MAX_PER_SOURCE` — 소스별 수집 개수 (기본 5)
- `RECENT_HOURS` — 최근 N시간 필터 (기본 24)
- `GROUP_HEADERS` / `GROUP_ORDER` — 텔레그램 출력 그룹 헤더·순서

---

## 동작 흐름

```
RSS 수집 (소스별 5개)
  -> 24시간 이내 + 제목 중복 제거
  -> Gemini 배치 1회 호출 (요약 + 인사이트 + 중요도 상/중/하 JSON)
  -> 그룹별(🇰🇭 영문현지 / 🇰🇷 한국언론) 정리, 중요도순 정렬
  -> 텔레그램 전송 (4096자 초과 시 자동 분할)
```

## 다음 단계 (MVP 2 예고)

브리핑 메시지에 버튼 추가 → 기사 초안 생성 → 채널 원클릭 발행.
