"""OpenDART 공시 목록 조회 + 키워드 매칭.

- 전체 시장 공시 목록(list.json)을 최신순으로 받아오며,
  접수번호(rcept_no, 날짜+일련번호로 단조 증가) 워터마크 아래로 내려가면 페이징을 멈춥니다.
- 늦게 목록에 올라오는 공시를 놓치지 않도록 워터마크를 지난 뒤 1페이지를 더 봅니다.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import timedelta
from typing import List, Tuple

import requests

from .common import Item, is_excluded, match_keywords, now_kst, strip_html

log = logging.getLogger(__name__)

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOC_URL = "https://opendart.fss.or.kr/api/document.xml"
VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

MAX_PAGES = 60
BODY_CHARS = 6000


class DartError(RuntimeError):
    pass


def _get_page(session: requests.Session, api_key: str, bgn: str, end: str, page: int) -> dict:
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
    watermark: str,
    floor: str,
    api_key: str,
    session: requests.Session,
    seed: bool = False,
) -> Tuple[List[Item], str, str]:
    """매칭 공시, 새 워터마크(조회한 최대 접수번호), 새 floor(조회한 최소 접수번호)를 반환.

    floor 미만은 이전 실행이 본 적 없는 오래된 공시이므로 후보에서 제외.
    floor~watermark 구간에서 seen에 없는 매칭 공시 = 늦게 올라온 공시 또는 미전송분.
    """
    today = now_kst().date()
    bgn = (today - timedelta(days=int(cfg.get("lookback_days", 1)))).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    markets = set(cfg.get("markets") or [])
    watch = {str(c).strip() for c in (cfg.get("watch_corps") or [])}

    rows: List[dict] = []
    new_watermark = watermark
    extra_pages = 1  # 워터마크를 지난 뒤 추가로 볼 페이지 수
    page = 1
    while page <= MAX_PAGES:
        data = _get_page(session, api_key, bgn, end, page)
        batch = data.get("list", [])
        if not batch:
            break
        rows.extend(batch)
        new_watermark = max([new_watermark] + [r["rcept_no"] for r in batch])
        if seed:
            break  # 첫 실행: 워터마크만 잡고 끝
        if watermark and min(r["rcept_no"] for r in batch) <= watermark:
            if extra_pages <= 0:
                break
            extra_pages -= 1
        if page >= int(data.get("total_page", 0)):
            break
        page += 1

    # 이번에 실제로 평가한 구간의 하한. floor 아래는 평가하지 않았으므로 floor 밑으로 내리지 않음
    new_floor = max(floor, min((r["rcept_no"] for r in rows), default=floor))
    items: List[Item] = []
    for r in rows:
        if floor and r["rcept_no"] < floor:
            continue
        if markets and r.get("corp_cls") not in markets:
            continue
        hay = f"{r['corp_name']} {r['report_nm']}"
        if is_excluded(hay, excludes):
            continue
        hits = match_keywords(hay, keywords)
        if r["corp_name"] in watch or (r.get("stock_code") or "") in watch:
            hits = [r["corp_name"]] + [h for h in hits if h != r["corp_name"]]
        if not hits:
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
                },
            )
        )
    log.info("DART: %d건 조회, %d건 매칭 (watermark %s → %s)", len(rows), len(items), watermark or "-", new_watermark)
    return items, new_watermark, new_floor


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
