from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

KST = timezone(timedelta(hours=9))

USER_AGENT = "keyword-alert-bot/1.0"


def now_kst() -> datetime:
    return datetime.now(KST)


@dataclass
class Item:
    id: str
    source: str  # "dart" | "news"
    title: str
    url: str
    keywords: List[str]
    published: Optional[datetime] = None
    publisher: str = ""
    text: str = ""  # 요약에 쓸 본문/발췌
    summary: str = ""
    extra: dict = field(default_factory=dict)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def match_keywords(text: str, keywords: Iterable[str]) -> List[str]:
    low = text.lower()
    return [k for k in keywords if k and k.lower() in low]


def is_excluded(text: str, excludes: Iterable[str]) -> bool:
    return bool(match_keywords(text, excludes))


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
