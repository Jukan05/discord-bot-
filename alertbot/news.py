"""Google 뉴스 RSS 키워드 검색."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import feedparser
import requests

from .common import Item, is_excluded, strip_html

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search"


def _get(session: requests.Session, params: dict) -> Optional[requests.Response]:
    for wait in (0, 5, 15):
        if wait:
            time.sleep(wait)
        try:
            r = session.get(RSS_URL, params=params, timeout=20)
        except requests.RequestException as e:
            log.debug("뉴스 요청 오류: %s", e)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            log.debug("뉴스 %d, 재시도", r.status_code)
            continue
        if r.ok:
            return r
        log.debug("뉴스 %d", r.status_code)
        return None
    return None


def fetch(cfg: dict, keywords: List[str], excludes: List[str], session: requests.Session) -> List[Item]:
    lookback = cfg.get("lookback", "1d")
    limit = int(cfg.get("max_per_keyword", 10))
    by_id: Dict[str, Item] = {}
    seen_titles: Dict[str, str] = {}

    for n, kw in enumerate(keywords):
        if n:
            time.sleep(1.5)  # Google 뉴스 429 방지
        r = _get(session, {"q": f"{kw} when:{lookback}", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
        if r is None:
            log.warning("뉴스 조회 실패 (%s) → 다음 실행에서 다시 시도", kw)
            continue
        feed = feedparser.parse(r.content)
        for e in feed.entries[:limit]:
            publisher = (e.get("source") or {}).get("title", "")
            title = e.get("title", "").strip()
            while publisher and title.endswith(" - " + publisher):
                title = title[: -len(" - " + publisher)].rstrip()
            snippet = strip_html(e.get("summary", ""))
            # RSS 발췌는 대개 "제목 + 언론사"뿐이라 그 경우 버림 (관련 기사 묶음일 때만 유지)
            rest = snippet.replace(title, "").replace(publisher, "").replace("-", "").strip()
            if len(rest) < 20:
                snippet = ""
            if is_excluded(f"{title} {snippet}", excludes):
                continue
            item_id = "news:" + (e.get("id") or e.get("link", ""))
            norm = "".join(title.split()).lower()
            # 같은 기사가 여러 키워드에 걸리면 키워드만 합침
            existing = by_id.get(item_id) or by_id.get(seen_titles.get(norm, ""))
            if existing:
                if kw not in existing.keywords:
                    existing.keywords.append(kw)
                continue
            published = None
            if e.get("published_parsed"):
                published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            by_id[item_id] = Item(
                id=item_id,
                source="news",
                title=title,
                url=e.get("link", ""),
                keywords=[kw],
                published=published,
                publisher=publisher,
                text=snippet,
            )
            seen_titles[norm] = item_id

    log.info("뉴스: %d건", len(by_id))
    return list(by_id.values())
