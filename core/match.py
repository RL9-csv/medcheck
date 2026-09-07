"""약 이름 -> 품목 확정.

OCR 오류를 흡수하는 것이 이 모듈의 존재 이유다.
SQL LIKE 만으로는 오타가 들어오면 후보가 0이 된다. 따라서 품목명 전체를
메모리에 올려두고 퍼지 검색한다. (19,478건, rapidfuzz는 C++ 구현이라 빠르다)

자동 확정 조건 (전부 만족해야 함)
  1) top1 점수 >= AUTO
  2) 후보가 하나이거나, top1 - top2 차이가 MARGIN 이상
     -> "페니라민"처럼 정제·주사가 같은 점수로 나오면 자동 확정하지 않는다.
        브랜드명이 맞았다고 품목코드까지 확신하면 안 된다.
  3) 그 외에는 후보를 제시하고 사람이 고른다.

가장 위험한 오류는 미탐이 아니라 "틀린 품목을 자신 있게 확정하는 것"이다.
* 임계값은 잠정치. 실제 약봉투로 threshold sweep 후 확정한다.
"""
import re, functools
from rapidfuzz import process, fuzz
from .db import connect, TABS, norm_kor
from .model import Medication, Ingredient

ING = re.compile(r"\[([A-Z]\d+)\]([^/\[\]]+)")
AUTO, SUGGEST, MARGIN = 0.85, 0.60, 0.05

# 짧은 쿼리는 퍼지 매칭을 신뢰하지 않는다.
# WRatio 는 짧은 문자열에 부분일치 보너스를 주면서 길이 비율 페널티가 없다.
# 그래서 OCR이 잘라놓은 "식후", "내과", "1일" 같은 조각이 각각
# 후라시닐정, 내린다시럽, 일바돈크림에 0.60 으로 붙는다. 전부 오탐이고,
# 사용자가 "골라주세요"에서 아무거나 고르면 먹지도 않은 약이 확정되어
# DUR 엔진에 들어간다. 화면이 지저분해지는 것보다 이쪽이 훨씬 위험하다.
#
# 길이에 따라 허용치를 나눈다. 한 글자 오차가 차지하는 비중이 다르기 때문이다.
#   3글자 이하  글자 하나가 33%다. 오차를 허용할 여지가 없으므로 문자 그대로
#              포함될 것을 요구한다. "아펜탕" 같은 3글자 오타는 놓치지만,
#              같은 점수(80)를 받는 "30분"을 살리는 대가는 더 크다.
#   4글자      한 글자 오차까지 허용한다. 3/4 = 75.
#              실측에서 오타는 75~100, 잡음은 67 이하로 갈렸다.
#   5글자 이상  신호가 충분하므로 기존 퍼지 매칭 그대로 둔다.
EXACT_BELOW = 4          # 이 미만은 문자 그대로 포함
FUZZY_BELOW = 5          # 이 미만은 한 글자 오차까지
ONE_CHAR_OFF = 75        # (4-1)/4


@functools.lru_cache(maxsize=1)
def _catalog():
    """품목 사전. (item_seq, item_name, main_ingr, otc) 튜플 목록."""
    conn = connect()
    union = " UNION ".join(
        f"SELECT ITEM_SEQ, ITEM_NAME, MAIN_INGR, ETC_OTC_NAME FROM {t}" for t in TABS)
    rows = [(r[0], r[1], r[2], r[3] or "") for r in conn.execute(f"SELECT * FROM ({union})")]
    conn.close()
    return rows


@functools.lru_cache(maxsize=1)
def _names():
    return [r[1] for r in _catalog()]


@functools.lru_cache(maxsize=4096)
def _ingredients_cached(main_ingr):
    conn = connect()
    out, seen = [], set()
    for code, raw in ING.findall(main_ingr or ""):
        if code in seen:
            continue
        seen.add(code)
        name = raw.strip()
        nk = norm_kor(name)
        row = conn.execute("SELECT kabs FROM kabs_map WHERE ingr_code=? LIMIT 1", (code,)).fetchone()
        if row is None:
            row = conn.execute("SELECT kabs FROM kabs_map WHERE ingr_kor_norm=? LIMIT 1",
                               (nk,)).fetchone()
        out.append(Ingredient(code, name, nk, row["kabs"] if row else None))
    conn.close()
    return tuple(out)


def _long_enough(q: str, name: str) -> bool:
    """짧은 쿼리가 이 품목명에 붙어도 되는지."""
    if len(q) >= FUZZY_BELOW:
        return True
    if len(q) < EXACT_BELOW:
        return q in name
    return fuzz.partial_ratio(q, name) >= ONE_CHAR_OFF


def search(query: str, limit: int = 5):
    q = (query or "").strip()
    if len(q) < 2:
        return []
    cat, names = _catalog(), _names()
    hits = process.extract(q, names, scorer=fuzz.WRatio, limit=limit * 4, score_cutoff=50)

    out, seen_seq = [], set()
    for _, score, idx in hits:
        seq, iname, mi, otc = cat[idx]
        if seq in seen_seq:
            continue
        if not _long_enough(q, iname):
            continue
        seen_seq.add(seq)
        out.append(Medication(item_seq=seq, product_name=iname, otc=otc,
                              ingredients=list(_ingredients_cached(mi)),
                              confidence=round(score / 100, 3)))
        if len(out) >= limit:
            break
    return out


def resolve(query: str):
    """(확정된 약 | None, 후보목록, 상태)  상태: auto | suggest | none"""
    cands = search(query)
    if not cands:
        return None, [], "none"
    top = cands[0]
    if top.confidence < SUGGEST:
        return None, [], "none"

    unambiguous = len(cands) == 1 or (top.confidence - cands[1].confidence) >= MARGIN
    if top.confidence >= AUTO and unambiguous:
        top.confirmed = True
        return top, cands, "auto"
    return None, cands, "suggest"
