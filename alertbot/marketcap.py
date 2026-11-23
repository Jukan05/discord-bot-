"""시가총액 기준 종목 목록 (네이버 증권 시총 순위)."""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from .common import Item

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


# 네이버 종목명과 기사에서 흔히 쓰는 이름이 다른 경우
ALIASES = {
    "NAVER": ["네이버"],
    "현대차": ["현대자동차"],
    "SK하이닉스": ["하이닉스"],
    "LG에너지솔루션": ["LG엔솔"],
    "삼성바이오로직스": ["삼성바이오"],
}
# 회사명 바로 뒤에 붙어도 같은 회사로 보는 글자 (조사 등). "한화"+"갤러리아"처럼
# 다른 글자가 이어지면 다른 회사로 보고 매칭하지 않음. ("로"는 "한화로보틱스" 같은 오탐 때문에 제외)
_PARTICLES = "은는이가을를의와과도에만측보처까부서랑께향向"
_HANGUL_ALNUM = "A-Za-z0-9&가-힣"
_SHORT_HANGUL = re.compile(r"^[가-힣]{1,2}$")

LISTING_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do"
_ROW_RE = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td[^>]*>.*?</td>\s*<td[^>]*>(\d{6})</td>", re.S)


def load_listed(session: requests.Session) -> Optional[Dict[str, str]]:
    """KRX KIND 상장법인 전체 목록 {회사명: 종목코드}. 실패하면 None."""
    try:
        r = session.get(
            LISTING_URL,
            params={"method": "download", "searchType": "13"},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        rows = _ROW_RE.findall(r.content.decode("euc-kr", errors="ignore"))
    except requests.RequestException as e:
        log.warning("상장사 목록 조회 실패 → 뉴스는 대형주 언급 여부로만 판단: %s", e)
        return None
    if len(rows) < 1000:
        log.warning("상장사 목록이 비정상(%d건) → 뉴스는 대형주 언급 여부로만 판단", len(rows))
        return None
    return {name.strip(): code for name, code in rows}


def restrict(
    caps: Optional[Dict[str, Tuple[str, int]]],
    sector: List[str],
    listed: Optional[Dict[str, str]],
) -> Optional[Dict[str, Tuple[str, int]]]:
    """통과 대상을 sector_corps 회사로 좁힘 (시총 기준과 AND).

    시총을 못 가져왔거나 시총 기준이 꺼져 있으면 상장사 목록으로 업종 제한만 적용.
    둘 다 실패하면 None(필터 없음).
    """
    wanted = set(sector)
    if listed:
        unknown = sorted(wanted - set(listed))
        if unknown:
            log.warning("sector_corps 중 상장사 목록에 없는 이름 (오타?): %s", ", ".join(unknown))
    if caps is not None:
        out = {c: v for c, v in caps.items() if v[0] in wanted}
    elif listed:
        out = {code: (name, 0) for name, code in listed.items() if name in wanted}
    else:
        log.warning("시총·상장사 목록 모두 조회 실패 → 이번 실행은 업종 제한 없이 진행")
        return None
    log.info("업종 제한: %d개 회사 대상 (%s)", len(out), ", ".join(v[0] for v in out.values()))
    return out


def build_matcher(names: Iterable[str]) -> "re.Pattern[str]":
    alts = sorted({n for n in names if n}, key=len, reverse=True)
    body = "|".join(re.escape(n) for n in alts)
    return re.compile(
        rf"(?<![{_HANGUL_ALNUM}])({body})(?=$|[^{_HANGUL_ALNUM}]|[{_PARTICLES}])",
        re.IGNORECASE,
    )


def filter_news(
    items: List[Item],
    caps: Dict[str, Tuple[str, int]],
    watch: Iterable[str],
    listed: Optional[Dict[str, str]] = None,
) -> List[Item]:
    """제목에서 처음 언급된 상장사가 시총 기준 이상(또는 관심 회사)인 기사만 남김.

    "휴림로봇, 삼성전기와 공급계약"처럼 소형주가 주어이고 대형주는 거래 상대인 기사는 제외.
    listed(전체 상장사 목록)가 없으면 대형주 이름이 하나라도 있으면 통과.
    """
    # 표기(소문자) → (대표 이름, 시총 또는 None, 통과 여부)
    names: Dict[str, Tuple[str, Optional[int], bool]] = {}
    for name, code in (listed or {}).items():
        if code in caps or _SHORT_HANGUL.match(name):
            continue  # 대형주는 아래에서, "대상"·"동방" 같은 일반어 소형주 이름은 오탐이 많아 제외
        names[name.lower()] = (name, None, False)
    code_to_name = {c: n for n, c in (listed or {}).items()}
    for w in watch:
        w_name = caps[w][0] if w in caps else code_to_name.get(w, w)
        names[w_name.lower()] = (w_name, None, True)
    for _code, (name, cap) in caps.items():
        for n in [name] + ALIASES.get(name, []):
            names[n.lower()] = (name, cap, True)
    matcher = build_matcher(names.keys())

    kept: List[Item] = []
    for it in items:
        subject = None
        for text in (it.title, it.text):
            m = matcher.search(text or "")
            if m:
                subject = names[m.group(1).lower()]
                break
        if subject and subject[2]:
            it.extra["companies"] = {subject[0]: subject[1]}
            kept.append(it)
    log.info("뉴스 시총 필터: %d건 중 %d건 통과", len(items), len(kept))
    return kept


def fmt(cap_eok: int) -> str:
    """억원 → '3.8조' / '9,500억'."""
    return f"{cap_eok / 10000:.1f}조" if cap_eok >= 10000 else f"{cap_eok:,}억"
