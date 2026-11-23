"""시가총액 기준 종목 목록 (네이버 증권 시총 순위)."""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def load(min_cap_jo: float, session: requests.Session) -> Optional[Dict[str, Tuple[str, int]]]:
    """시총 min_cap_jo(조원) 이상인 코스피·코스닥 보통주 {종목코드: (이름, 시총 억원)}.

    조회에 실패하면 None (호출 측에서 필터를 끄고 진행 → 큰 회사 공시를 놓치지 않도록).
    """
    threshold = int(min_cap_jo * 10000)  # API 단위: 억원
    result: Dict[str, Tuple[str, int]] = {}
    try:
        for market in ("KOSPI", "KOSDAQ"):
            for page in range(1, 21):
                r = session.get(
                    URL.format(market=market),
                    params={"page": page, "pageSize": 100},
                    headers=HEADERS,
                    timeout=15,
                )
                r.raise_for_status()
                stocks = r.json().get("stocks") or []
                if not stocks:
                    break
                last_cap = None
                for s in stocks:
                    last_cap = int(str(s.get("marketValue", "0")).replace(",", "") or 0)
                    if last_cap >= threshold and s.get("stockEndType") == "stock":
                        result[s["itemCode"]] = (s.get("stockName", ""), last_cap)
                if last_cap is not None and last_cap < threshold:
                    break  # 시총 내림차순이라 이후 페이지는 모두 기준 미만
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("시총 목록 조회 실패 → 이번 실행은 시총 필터 없이 진행: %s", e)
        return None
    if not result:
        log.warning("시총 목록이 비어 있음 → 이번 실행은 시총 필터 없이 진행")
        return None
    log.info("시총 %.1f조 이상 종목 %d개", min_cap_jo, len(result))
    return result


def fmt(cap_eok: int) -> str:
    """억원 → '3.8조' / '9,500억'."""
    return f"{cap_eok / 10000:.1f}조" if cap_eok >= 10000 else f"{cap_eok:,}억"
