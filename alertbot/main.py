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

from . import dart, discord, news
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
    new_watermark, new_floor = state.dart_watermark, state.dart_floor
    if dart_cfg.get("enabled", True):
        if not dart_key:
            log.warning("DART_API_KEY 없음 → DART 건너뜀")
        else:
            try:
                found, new_watermark, new_floor = dart.fetch(
                    dart_cfg, keywords, excludes, state.dart_watermark, state.dart_floor,
                    dart_key, session, seed=seed,
                )
                items += found
            except Exception as e:
                errors += 1
                log.error("DART 조회 실패: %s", e)

    news_cfg = cfg.get("news") or {}
    if news_cfg.get("enabled", True) and keywords:
        items += news.fetch(news_cfg, keywords, excludes, session)

    new_items = [it for it in items if not state.is_seen(it.id)]
    log.info("새 항목 %d건 (전체 매칭 %d건)", len(new_items), len(items))

    if seed:
        for it in new_items:
            state.mark(it.id)
        state.dart_watermark, state.dart_floor = new_watermark, new_floor
        if not args.dry_run:
            state.save()
        return 0

    new_items.sort(key=_sort_key)
    all_new = list(new_items)
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
        for it in batch:
            state.mark(it.id)
        state.save()  # 배치마다 저장 → 중간 실패해도 중복 전송 최소화

    # 워터마크 전진. 못 보낸 DART 항목(전송 실패·건수 제한)이 있으면 그 바로 앞까지만 전진해
    # 다음 실행에서 다시 수집되게 함.
    if not args.dry_run and webhook:
        unsent = [it.extra["rcept_no"] for it in all_new if it.source == "dart" and not state.is_seen(it.id)]
        if unsent:
            new_watermark = min(new_watermark, str(int(min(unsent)) - 1).zfill(len(min(unsent))))
        state.dart_watermark, state.dart_floor = new_watermark, new_floor
        state.save()
    return 1 if errors else 0


def main() -> None:
    p = argparse.ArgumentParser(description="공시·뉴스 키워드 Discord 알림 봇")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default="state/seen.json")
    p.add_argument("--dry-run", action="store_true", help="전송·상태 저장 없이 출력만")
    p.add_argument("--seed", action="store_true", help="현재 항목을 본 것으로만 기록")
    p.add_argument("--no-seed", action="store_true", help="상태 파일이 없어도 바로 전송")
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
