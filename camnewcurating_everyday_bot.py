#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
까로나 뉴스봇 MVP 1 - 캄보디아 뉴스 브리핑 봇 (올인원)

실행 흐름:
    [매일 오전 11시 cron 실행]
        -> Google News RSS 수집 (현지 영문 / 한국 언론)
        -> 24시간 이내 필터링 + 제목 중복 제거
        -> [현지 영문 7꼭지] Gemini 배치 호출로 한국어 코멘트 + 중요도 부여
           (제목은 번역하지 않고 영어 원문 그대로 전달)
        -> [한국 언론 3꼭지] 제목 + 출처만 간단히 (Gemini 미사용)
        -> 텔레그램으로 두 메시지 따로 전송 (현지 먼저 -> 한국 나중)

설계 문서: cambodia_news_bot_design.md
주의: 모델은 gemini-3-flash-preview 를 사용하므로 신버전 SDK(google-genai)가 필요합니다.

설치:
    pip install feedparser google-genai python-dotenv requests

환경변수 (.env):
    GEMINI_API_KEY=...
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...
    GEMINI_MODEL=gemini-3-flash-preview   (선택, 기본값 있음)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from dotenv import load_dotenv

# 신버전 통합 SDK (gemini-3 계열 지원)
from google import genai
from google.genai import types


# ─────────────────────────────────────────────────────────────
# 0. 환경설정 로드
# ─────────────────────────────────────────────────────────────
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview").strip()

# 표시용 시간대 - 캄보디아 시간 (ICT, UTC+7). 캄보디아는 서머타임 없음.
LOCAL_TZ = timezone(timedelta(hours=7))


# ─────────────────────────────────────────────────────────────
# 1. 수집 설정 (언제든 수정/추가 가능)
# ─────────────────────────────────────────────────────────────
# group 키로 텔레그램 출력/처리 방식을 구분한다.
#   local_en : 캄보디아 현지 영문 -> 원문 제목 + 한국어 코멘트
#   korea_ko : 한국 언론 -> 제목 + 출처만
RSS_SOURCES = [
    {
        "name": "캄보디아 현지 (영문)",
        "group": "local_en",
        "url": "https://news.google.com/rss/search?q=cambodia&hl=en",
    },
    {
        "name": "한국 언론 (캄보디아)",
        "group": "korea_ko",
        "url": "https://news.google.com/rss/search?q=캄보디아&hl=ko&gl=KR",
    },
    {
        "name": "한국 언론 (프놈펜)",
        "group": "korea_ko",
        "url": "https://news.google.com/rss/search?q=프놈펜&hl=ko&gl=KR",
    },
]

# 그룹별 최종 노출 꼭지 수
LOCAL_COUNT = 7        # 현지 영문 꼭지 수
KOREA_COUNT = 3        # 한국 언론 꼭지 수
LOCAL_POOL = 12        # 현지: 중요도 선별을 위해 일단 더 모으는 풀 크기

RECENT_HOURS = 24      # 최근 N시간 이내만
REQUEST_TIMEOUT = 20   # HTTP 타임아웃(초)

# 중요도 표시 이모지 (현지 영문용)
IMPORTANCE_EMOJI = {"상": "⭐", "중": "▪️", "하": "▫️"}
IMPORTANCE_ORDER = {"상": 0, "중": 1, "하": 2}


# ─────────────────────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camnewsbot")


# ─────────────────────────────────────────────────────────────
# 2. RSS 수집
# ─────────────────────────────────────────────────────────────
def _entry_published(entry):
    """RSS entry에서 발행시각(UTC aware datetime)을 추출. 실패 시 None."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_source(entry, title):
    """기사 출처(매체명) 추출.

    Google News RSS는 <source> 태그로 매체명을 준다.
    없으면 제목 끝의 ' - 매체명' 패턴에서 추출.
    """
    src = entry.get("source")
    if isinstance(src, dict):
        name = (src.get("title") or "").strip()
        if name:
            return name
    if " - " in title:
        return title.rsplit(" - ", 1)[1].strip()
    return ""


def _clean_title(title, source):
    """Google News 제목 끝의 ' - 매체명'을 제거해 원문 헤드라인만 남긴다."""
    if source and title.endswith(f" - {source}"):
        return title[: -len(f" - {source}")].strip()
    return title


def _sort_recent(articles):
    """발행시각 최신순 정렬 (시각 없는 항목은 뒤로)."""
    far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return sorted(
        articles,
        key=lambda a: a["published"] or far_past,
        reverse=True,
    )


def collect_articles():
    """RSS 소스를 돌며 그룹별 기사를 수집한다.

    반환: (local_articles, korea_articles)
      - 24시간 이내, 제목 기준 전역 중복 제거
      - 현지(local_en): 최신 LOCAL_POOL개까지 모음 (이후 중요도로 7개 선별)
      - 한국(korea_ko): 최신 KOREA_COUNT개
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RECENT_HOURS)

    seen_titles = set()
    buckets = {"local_en": [], "korea_ko": []}

    for src in RSS_SOURCES:
        log.info("RSS 수집: %s", src["name"])
        try:
            feed = feedparser.parse(src["url"])
        except Exception as e:  # noqa: BLE001
            log.warning("  RSS 파싱 실패 (%s): %s", src["name"], e)
            continue

        count = 0
        for entry in feed.entries:
            raw_title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not raw_title or not link:
                continue

            published = _entry_published(entry)
            if published is not None and published < cutoff:
                continue

            source = _entry_source(entry, raw_title)
            title = _clean_title(raw_title, source)

            norm = title.lower()
            if norm in seen_titles:
                continue
            seen_titles.add(norm)

            buckets[src["group"]].append({
                "title": title,
                "link": link,
                "source": source,
                "group": src["group"],
                "published": published,
            })
            count += 1

        log.info("  -> %d개 수집", count)

    local = _sort_recent(buckets["local_en"])[:LOCAL_POOL]
    korea = _sort_recent(buckets["korea_ko"])[:KOREA_COUNT]
    log.info("수집 완료: 현지 후보 %d개 / 한국 %d개", len(local), len(korea))
    return local, korea


# ─────────────────────────────────────────────────────────────
# 3. Gemini 배치 처리 (현지 영문 전용: 한국어 코멘트 + 중요도)
# ─────────────────────────────────────────────────────────────
LOCAL_SYSTEM_PROMPT = """\
당신은 캄보디아 뉴스 큐레이션 편집 보조 AI입니다.
입력으로 영문 캄보디아 뉴스 기사 제목 목록(번호 포함)이 주어집니다.
각 기사에 대해 다음을 작성하세요.

- comment: 한국 교민/투자자/편집장 관점에서 이 기사가 왜 중요한지, 무엇을 시사하는지
           한국어 한 문장 코멘트. (※ 기사 제목 자체를 번역하지 말고, 코멘트만 작성)
- importance: 뉴스 중요도를 "상" / "중" / "하" 중 하나로 분류
    (상: 정책·경제·대형 프로젝트 등 파급력 큰 핵심 뉴스,
     중: 참고할 만한 일반 뉴스,
     하: 단신·가벼운 소식)

반드시 아래 JSON 형식 하나만 출력하세요. 그 외 텍스트 금지.
{
  "items": [
    {"index": 1, "comment": "...", "importance": "상"},
    ...
  ]
}
입력된 기사 개수와 동일한 개수의 항목을 index 순서대로 반환하세요.
"""


def enrich_local(articles):
    """현지 영문 기사에 한국어 코멘트 + 중요도를 채운다.

    실패 시 코멘트는 빈 문자열, 중요도는 '중' 기본값으로 넘어간다.
    """
    if not articles:
        return articles

    lines = [f"{i}. {a['title']}" for i, a in enumerate(articles, start=1)]
    user_prompt = "다음 영문 기사들을 처리하세요:\n\n" + "\n".join(lines)

    client = genai.Client(api_key=GEMINI_API_KEY)

    raw = None
    for attempt in range(1, 3):  # 최대 2회 시도
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=LOCAL_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )
            raw = (resp.text or "").strip()
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini 호출 실패 (시도 %d): %s", attempt, e)
            time.sleep(2 * attempt)

    parsed = _parse_items_json(raw) if raw else {}

    for i, a in enumerate(articles, start=1):
        item = parsed.get(i, {})
        a["comment"] = (item.get("comment") or "").strip()
        imp = (item.get("importance") or "중").strip()
        a["importance"] = imp if imp in IMPORTANCE_EMOJI else "중"

    return articles


def _parse_items_json(raw):
    """Gemini가 돌려준 JSON 문자열을 {index: item} 딕셔너리로 변환."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("Gemini JSON 파싱 실패: %s", e)
        return {}

    result = {}
    for item in data.get("items", []):
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        result[idx] = item
    return result


def select_local(articles):
    """현지 기사를 중요도순(상>중>하), 동순위는 최신순으로 정렬해 상위 LOCAL_COUNT개 선별."""
    far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
    ranked = sorted(
        articles,
        key=lambda a: (
            IMPORTANCE_ORDER.get(a.get("importance", "중"), 1),
            -(a["published"] or far_past).timestamp(),
        ),
    )
    return ranked[:LOCAL_COUNT]


# ─────────────────────────────────────────────────────────────
# 3-b. (v1.2) 현지 영문 심층 요약 - Gemini Google 검색 그라운딩
# ─────────────────────────────────────────────────────────────
DEEP_SUMMARY_PROMPT = """\
당신은 캄보디아 뉴스를 한국 교민/투자자에게 브리핑하는 편집 보조 AI입니다.
아래 영문 기사 1건에 대해, Google 검색으로 실제 기사 내용을 확인한 뒤 한국어로 작성하세요.

기사 제목: {title}
출처: {source}

작성 규칙:
- 검색으로 기사 내용을 확인할 수 없으면 제목만으로 추측하지 말고, 요약 자리에 "(원문 확인 필요)"라고만 쓰세요.
- 사실에 근거해서만 작성하고, 모르는 내용을 지어내지 마세요.
- 반드시 한국어로 작성하세요.

아래 형식 그대로 출력하세요 (다른 말, 머리말, 마크다운 금지):
요약: <기사 핵심을 한국어 2~3문장으로>
의미: <한국 교민/투자자/편집장에게 주는 함의 한 문장>
"""


def deep_summarize_local(articles):
    """(v1.2) 선별된 현지 영문 기사에 검색 기반 한국어 요약/의미를 채운다.

    Gemini Google 검색 그라운딩으로 기사별 1회 호출.
    실패 시 enrich_local()에서 만든 기존 코멘트로 폴백한다.
    """
    if not articles:
        return articles

    client = genai.Client(api_key=GEMINI_API_KEY)

    for a in articles:
        summary, insight = "", ""
        prompt = DEEP_SUMMARY_PROMPT.format(
            title=a["title"],
            source=a.get("source") or "(미상)",
        )
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.3,
                ),
            )
            summary, insight = _parse_summary_insight((resp.text or "").strip())
            log.info("심층 요약 완료: %s", a["title"][:40])
        except Exception as e:  # noqa: BLE001
            log.warning("심층 요약 실패(폴백 사용) [%s...]: %s", a["title"][:30], e)

        # 폴백: 요약 실패 시 배치 단계의 코멘트를 요약 자리에 사용
        a["summary"] = summary or a.get("comment") or ""
        a["insight"] = insight
        time.sleep(0.5)  # 호출 간 약간의 간격

    return articles


def _parse_summary_insight(text):
    """'요약: ... / 의미: ...' 형식 텍스트를 (summary, insight)로 분리."""
    if not text:
        return "", ""
    summary, insight = text, ""
    if "의미:" in text:
        before, after = text.split("의미:", 1)
        summary = before
        insight = after.strip().splitlines()[0].strip() if after.strip() else ""
    # '요약:' 라벨 제거 + 공백/줄바꿈 정리(2~3문장 한 단락으로)
    summary = " ".join(summary.replace("요약:", "").split())
    insight = " ".join(insight.split())
    return summary, insight


# ─────────────────────────────────────────────────────────────
# 4. 텔레그램 메시지 포맷
# ─────────────────────────────────────────────────────────────
def _escape(text):
    """텔레그램 HTML 파싱용 최소 이스케이프."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now_strings():
    now_local = datetime.now(LOCAL_TZ)
    return now_local.strftime("%m.%d"), now_local.strftime("%H:%M")


def build_local_message(articles):
    """메시지 1 - 캄보디아 현지 (영문). 원문 제목 + 한국어 요약 + 교민 의미 + 출처. (v1.2)"""
    date_str, time_str = _now_strings()
    lines = [
        f"🇰🇭 <b>캄보디아 현지 뉴스 (영문)</b> ({date_str} / {time_str})",
        "━━━━━━━━━━━━━━━",
        "",
    ]
    for n, a in enumerate(articles, start=1):
        emoji = IMPORTANCE_EMOJI.get(a.get("importance", "중"), "▪️")
        lines.append(f"{emoji} [{n}] {_escape(a['title'])}")
        # 심층 요약(v1.2). 없으면 기존 코멘트로 폴백.
        summary = a.get("summary") or a.get("comment") or ""
        if summary:
            lines.append(f"   {_escape(summary)}")
        if a.get("insight"):
            lines.append(f"   💡 {_escape(a['insight'])}")
        meta = []
        if a.get("source"):
            meta.append(f"📰 {_escape(a['source'])}")
        meta.append(f'🔗 <a href="{a["link"]}">링크</a>')
        lines.append("   " + "  ".join(meta))
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"총 {len(articles)}건 · 🤖 까로나 뉴스봇 v1.2-beta")
    return "\n".join(lines)


def build_korea_message(articles):
    """메시지 2 - 한국 언론 속 캄보디아. 제목 + 출처만 간단히."""
    date_str, time_str = _now_strings()
    lines = [
        f"🇰🇷 <b>한국 언론 속 캄보디아</b> ({date_str} / {time_str})",
        "━━━━━━━━━━━━━━━",
        "",
    ]
    for n, a in enumerate(articles, start=1):
        lines.append(f"▪️ [{n}] {_escape(a['title'])}")
        meta = []
        if a.get("source"):
            meta.append(f"📰 {_escape(a['source'])}")
        meta.append(f'🔗 <a href="{a["link"]}">링크</a>')
        lines.append("   " + "  ".join(meta))
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"총 {len(articles)}건 · 🤖 까로나 뉴스봇 v1.2-beta")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 5. 텔레그램 전송 (Bot HTTP API 직접 호출)
# ─────────────────────────────────────────────────────────────
def send_telegram(text):
    """텔레그램 sendMessage API 호출. 4096자 초과 시 분할 전송."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_message(text, limit=3800)

    for i, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                log.error("텔레그램 전송 실패 (%d/%d): %s %s",
                          i, len(chunks), r.status_code, r.text)
                return False
            log.info("텔레그램 전송 성공 (%d/%d)", i, len(chunks))
        except requests.RequestException as e:
            log.error("텔레그램 전송 예외: %s", e)
            return False
        if len(chunks) > 1:
            time.sleep(1)
    return True


def _split_message(text, limit=3800):
    """줄 단위로 메시지를 limit 이하 청크로 분할."""
    if len(text) <= limit:
        return [text]
    chunks, current, length = [], [], 0
    for line in text.split("\n"):
        if length + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


# ─────────────────────────────────────────────────────────────
# 6. 메인
# ─────────────────────────────────────────────────────────────
def check_env():
    missing = [k for k, v in {
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing:
        log.error("환경변수 누락: %s (.env 파일을 확인하세요)", ", ".join(missing))
        return False
    return True


def main():
    log.info("===== 까로나 뉴스봇 시작 (model=%s) =====", GEMINI_MODEL)

    if not check_env():
        sys.exit(1)

    # 1) 수집
    local, korea = collect_articles()

    if not local and not korea:
        date_str, _ = _now_strings()
        send_telegram(
            f"📰 캄보디아 뉴스 브리핑\n오늘({date_str})은 최근 24시간 내 새 기사가 없습니다."
        )
        log.info("수집된 기사 없음. 종료.")
        return

    sent_ok = True

    # 2) 메시지 1 - 현지 영문 (중요도 선별 → v1.2 검색 기반 심층 요약)
    if local:
        local = enrich_local(local)          # 배치: 중요도 + 폴백용 코멘트
        local = select_local(local)          # 중요도순 상위 7개 선별
        local = deep_summarize_local(local)  # v1.2: 검색 그라운딩 심층 요약
        msg1 = build_local_message(local)
        log.info("메시지 1 (현지 영문 %d건) 전송", len(local))
        sent_ok &= send_telegram(msg1)
        time.sleep(1)  # 메시지 간 간격
    else:
        log.info("현지 영문 기사 없음 - 메시지 1 생략")

    # 3) 메시지 2 - 한국 언론 (제목 + 출처)
    if korea:
        msg2 = build_korea_message(korea)
        log.info("메시지 2 (한국 언론 %d건) 전송", len(korea))
        sent_ok &= send_telegram(msg2)
    else:
        log.info("한국 언론 기사 없음 - 메시지 2 생략")

    log.info("===== 종료 (전송 %s) =====", "성공" if sent_ok else "실패")
    if not sent_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
