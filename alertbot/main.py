"""공시·뉴스 키워드 알림 봇 진입점.

    python -m alertbot.main            # 실제 실행
    python -m alertbot.main --dry-run  # 전송/상태저장 없이 콘솔 출력
    python -m alertbot.main --seed     # 현재 항목을 '본 것'으로만 표시 (첫 실행 기본값)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import List

import requests
import yaml

from . import dart, discord, marketcap, news
from .common import USER_AGENT, Item
from .state import State
from .summarize import summarize

log = logging.getLogger("alertbot")


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _sort_key(it: Item) -> str:
    if it.source == "dart":
        return it.extra.get("rcept_no", "")
    return it.published.strftime("%Y%m%d%H%M%S") if it.published else ""


def _pick_test_items(items: List[Item], n: int) -> List[Item]:
    """테스트용: 최근 공시 위주로 n건 (뉴스 1건 포함해 두 경로 모두 확인)."""
    newest = sorted(items, key=_sort_key, reverse=True)
    darts = [i for i in newest if i.source == "dart"]
    newses = [i for i in newest if i.source == "news"]
    picked = darts[: max(n - 1, 1)] + newses[:1]
    rest = [i for i in newest if i not in picked]
    return (picked + rest)[:n]


def run(args: argparse.Namespace) -> int:
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    keywords = [str(k) for k in cfg.get("keywords") or []]
    excludes = [str(k) for k in cfg.get("exclude_keywords") or []]
    state = State(args.state)
    seed = args.seed or (not state.exists and not args.no_seed)
    if seed:
        log.info("시드 모드: 현재 항목을 기록만 하고 알림은 보내지 않습니다.")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    items: List[Item] = []
    errors = 0

    dart_cfg = cfg.get("dart") or {}
    dart_key = os.environ.get("DART_API_KEY", "")
    watch = [str(c).strip() for c in dart_cfg.get("watch_corps") or []]
    min_cap = float(cfg.get("min_market_cap") or 0)
    sector = [str(n).strip() for n in cfg.get("sector_corps") or []]
    caps = marketcap.load(min_cap, session) if min_cap else None
    listed = marketcap.load_listed(session) if (caps is not None or sector) else None
    if sector:
        caps = marketcap.restrict(caps, sector, listed)
    if dart_cfg.get("enabled", True):
        if not dart_key:
            log.warning("DART_API_KEY 없음 → DART 건너뜀")
        else:
            try:
                items += dart.fetch(dart_cfg, keywords, excludes, dart_key, session, caps)
            except Exception as e:
                errors += 1
                log.error("DART 조회 실패: %s", e)

    news_cfg = cfg.get("news") or {}
    if news_cfg.get("enabled", True) and keywords:
        found = news.fetch(news_cfg, keywords, excludes, session)
        if caps is not None:
            found = marketcap.filter_news(found, caps, watch, listed)
        items += found

    if args.test:
        new_items = _pick_test_items(items, args.test)
        log.info("테스트 모드: 최근 매칭 %d건을 [테스트]로 전송 (상태는 저장 안 함)", len(new_items))
        for it in new_items:
            it.title = "[테스트] " + it.title
    else:
        new_items = [it for it in items if not state.is_seen(it.id)]
        log.info("새 항목 %d건 (전체 매칭 %d건)", len(new_items), len(items))

    if seed and not args.test:
        for it in new_items:
            state.mark(it.id)
        if not args.dry_run and not errors:
            state.save()
        return 1 if errors else 0

    new_items.sort(key=_sort_key)
    limit = int((cfg.get("summary") or {}).get("max_items_per_run", 20))
    if len(new_items) > limit:
        log.info("이번 실행은 %d건만 전송, 나머지 %d건은 다음 실행으로", limit, len(new_items) - limit)
        new_items = new_items[:limit]

    if dart_cfg.get("fetch_body", True) and dart_key:
        for it in new_items:
            if it.source == "dart":
                it.text = dart.fetch_body(it, dart_key, session)

    summarize(new_items, cfg.get("summary") or {})

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    username = (cfg.get("discord") or {}).get("username", "")
    for batch in discord.chunk(new_items):
        if args.dry_run or not webhook:
            if not webhook and not args.dry_run:
                log.warning("DISCORD_WEBHOOK_URL 없음 → 콘솔 출력만")
            for it in batch:
                print(f"\n[{it.source}] {it.title}\n  {it.url}\n  키워드: {', '.join(it.keywords)}\n  요약: {it.summary or '-'}")
            continue
        try:
            discord.send(webhook, batch, username)
        except Exception as e:
            errors += 1
            log.error("Discord 전송 실패: %s", e)
            break  # 실패분은 seen에 기록하지 않아 다음 실행에서 재시도
        log.info("Discord 전송 %d건", len(batch))
        if args.test:
            continue
        for it in batch:
            state.mark(it.id)
        state.save()  # 배치마다 저장 → 중간 실패해도 중복 전송 최소화

    return 1 if errors else 0


def main() -> None:
    p = argparse.ArgumentParser(description="공시·뉴스 키워드 Discord 알림 봇")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default="state/seen.json")
    p.add_argument("--dry-run", action="store_true", help="전송·상태 저장 없이 출력만")
    p.add_argument("--seed", action="store_true", help="현재 항목을 본 것으로만 기록")
    p.add_argument("--no-seed", action="store_true", help="상태 파일이 없어도 바로 전송")
    p.add_argument("--test", type=int, default=0, metavar="N", help="최근 매칭 N건을 [테스트]로 전송 (상태 저장 안 함)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
