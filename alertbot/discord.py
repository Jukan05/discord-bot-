"""Discord 웹훅 전송 (메시지당 최대 10개 embed, embed 총 6000자 제한 준수)."""
from __future__ import annotations

import logging
import time
from typing import Iterator, List

import requests

from . import marketcap
from .common import Item, truncate

log = logging.getLogger(__name__)

COLORS = {"dart": 0x1F6FEB, "news": 0x2DA44E}
MARKET = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타"}
MAX_EMBEDS = 10
MAX_CHARS = 5500  # 6000 한도에 여유


def build_embed(item: Item) -> dict:
    desc = item.summary or truncate(item.text, 500)
    fields = [{"name": "키워드", "value": truncate(", ".join(item.keywords), 1000), "inline": True}]
    if item.source == "dart":
        if item.publisher:
            fields.append({"name": "제출인", "value": truncate(item.publisher, 1000), "inline": True})
        mkt = MARKET.get(item.extra.get("corp_cls", ""))
        if item.extra.get("market_cap"):
            mkt = f"{mkt or ''} · 시총 {marketcap.fmt(item.extra['market_cap'])}".strip(" ·")
        if mkt:
            fields.append({"name": "시장", "value": mkt, "inline": True})
        footer = "DART 전자공시"
        if item.extra.get("rcept_dt"):
            d = item.extra["rcept_dt"]
            footer += f" · {d[:4]}-{d[4:6]}-{d[6:]}"
    else:
        footer = f"{item.publisher} · Google 뉴스" if item.publisher else "Google 뉴스"
    embed = {
        "title": truncate(("📢 " if item.source == "dart" else "📰 ") + item.title, 256),
        "url": item.url,
        "color": COLORS.get(item.source, 0x888888),
        "fields": fields,
        "footer": {"text": truncate(footer, 200)},
    }
    if desc:
        embed["description"] = truncate(desc, 2000)
    if item.published:
        embed["timestamp"] = item.published.isoformat()
    return embed


def _embed_size(e: dict) -> int:
    return (
        len(e.get("title", ""))
        + len(e.get("description", ""))
        + len(e["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))
    )


def chunk(items: List[Item]) -> Iterator[List[Item]]:
    batch: List[Item] = []
    size = 0
    for it in items:
        s = _embed_size(build_embed(it))
        if batch and (len(batch) >= MAX_EMBEDS or size + s > MAX_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += s
    if batch:
        yield batch


def send(webhook_url: str, items: List[Item], username: str = "") -> None:
    payload = {"embeds": [build_embed(i) for i in items]}
    if username:
        payload["username"] = username
    for attempt in range(5):
        r = requests.post(webhook_url, params={"wait": "true"}, json=payload, timeout=20)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 1))
            log.warning("Discord rate limit, %.1fs 대기", wait)
            time.sleep(wait + 0.2)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord {r.status_code}: {r.text[:300]}")
        return
    raise RuntimeError("Discord rate limit 재시도 초과")
