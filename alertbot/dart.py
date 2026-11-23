"""OpenDART 공시 목록 조회 + 키워드 매칭.

오늘 접수된 전체 공시 목록(list.json)을 모든 페이지 훑은 뒤 키워드로 거르고,
중복은 state의 seen ID로 막습니다. 접수번호는 금감원(00xxxx)·거래소(8xxxxx)·
공정위(9xxxxx) 계열이 섞여 있어 번호 순서에 기대지 않습니다.
평일 하루 공시는 보통 1,000~3,000건(10~30페이지)이라 15분 간격이면
하루 API 한도(20,000건) 안에 들어옵니다.
"""
from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from datetime import timedelta
from typing import List

import requests

from . import marketcap
from .common import Item, is_excluded, match_keywords, now_kst, strip_html

log = logging.getLogger(__name__)

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOC_URL = "https://opendart.fss.or.kr/api/document.xml"
VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

MAX_PAGES = 100
BODY_CHARS = 6000


class DartError(RuntimeError):
    pass


def _get_page(session: requests.Session, api_key: str, bgn: str, end: str, page: int) -> dict:
    for attempt in range(3):
        try:
            r = session.get(
                LIST_URL,
                params={
                    "crtfc_key": api_key,
                    "bgn_de": bgn,
                    "end_de": end,
                    "page_no": page,
                    "page_count": 100,
                    "sort": "date",
                    "sort_mth": "desc",
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    status = data.get("status")
    if status == "013":  # 조회된 데이터 없음
        return {"list": [], "total_page": 0}
    if status != "000":
        raise DartError(f"DART {status}: {data.get('message')}")
    return data


def fetch(
    cfg: dict,
    keywords: List[str],
    excludes: List[str],
    api_key: str,
    session: requests.Session,
) -> List[Item]:
    now = now_kst()
    # 자정 직후에는 전날 밤 공시까지 보도록 어제부터 조회
    days = int(cfg.get("lookback_days", 1)) if now.hour < 2 else 0
    bgn = (now.date() - timedelta(days=days)).strftime("%Y%m%d")
    end = now.date().strftime("%Y%m%d")
    markets = set(cfg.get("markets") or [])
    watch = {str(c).strip() for c in (cfg.get("watch_corps") or [])}

    rows: List[dict] = []
    page = 1
    while page <= MAX_PAGES:
        data = _get_page(session, api_key, bgn, end, page)
        rows.extend(data.get("list", []))
        if page >= int(data.get("total_page", 0)):
            break
        page += 1

    min_cap = float(cfg.get("min_market_cap") or 0)
    caps = marketcap.load(min_cap, session) if min_cap and rows else None

    items: List[Item] = []
    small = 0
    for r in rows:
        if markets and r.get("corp_cls") not in markets:
            continue
        hay = f"{r['corp_name']} {r['report_nm']}"
        if is_excluded(hay, excludes):
            continue
        code = r.get("stock_code") or ""
        watched = r["corp_name"] in watch or code in watch
        hits = match_keywords(hay, keywords)
        if watched:
            hits = [r["corp_name"]] + [h for h in hits if h != r["corp_name"]]
        if not hits:
            continue
        if caps is not None and not watched and code not in caps:
            small += 1
            continue
        items.append(
            Item(
                id="dart:" + r["rcept_no"],
                source="dart",
                title=f"{r['corp_name']} · {r['report_nm'].strip()}",
                url=VIEW_URL.format(r["rcept_no"]),
                keywords=hits,
                publisher=r.get("flr_nm", ""),
                extra={
                    "rcept_no": r["rcept_no"],
                    "rcept_dt": r.get("rcept_dt", ""),
                    "corp_cls": r.get("corp_cls", ""),
                    "rm": r.get("rm", ""),
                    "market_cap": caps[code][1] if caps and code in caps else None,
                },
            )
        )
    log.info(
        "DART: %s~%s %d페이지 %d건 조회, %d건 매칭 (시총 미달 제외 %d건)",
        bgn, end, page, len(rows), len(items), small,
    )
    return items


def fetch_body(item: Item, api_key: str, session: requests.Session) -> str:
    """공시 원문(zip 안의 XML)을 받아 텍스트로 변환. 실패하면 빈 문자열."""
    try:
        r = session.get(
            DOC_URL,
            params={"crtfc_key": api_key, "rcept_no": item.extra["rcept_no"]},
            timeout=30,
        )
        r.raise_for_status()
        if not r.content.startswith(b"PK"):
            log.warning("DART 원문 없음 %s: %s", item.id, r.text[:200])
            return ""
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = sorted(z.namelist(), key=lambda n: z.getinfo(n).file_size, reverse=True)
            raw = z.read(names[0])
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                xml = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            xml = raw.decode("utf-8", errors="ignore")
        xml = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", xml)
        return strip_html(xml)[:BODY_CHARS]
    except Exception as e:  # 원문은 부가 정보라 실패해도 알림은 보냄
        log.warning("DART 원문 다운로드 실패 %s: %s", item.id, e)
        return ""
