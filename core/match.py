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

# 긴 질의는 앞 8글자만 쓴다. 함량 접미사가 정보가 아니라 잡음이었다.
#
# 실제 제품명 1,928건(카탈로그 밖 789건 포함)으로 잰 결과:
#   원본 그대로   auto 정답  983   오확정 102 (5.3%)
#   앞 8글자      auto 정답 1045   오확정  16 (0.8%)
# "80밀리그램", "100/1000" 같은 꼬리가 WRatio 부분일치 보너스를 부풀려서
# 카탈로그에 없는 약을 비슷한 다른 약에 0.85 위로 밀어올렸다. 자르면 사라진다.
# 도달 가능 비율은 4글자까지 잘라도 거의 안 떨어진다(0.587 -> 0.576).
#
# 같은 약의 다른 함량은 8글자가 같아 점수가 묶인다. 그건 MARGIN 이 잡아서
# suggest 로 떨어뜨린다. 사람이 함량을 고르는 것이 맞다.
PREFIX = 8


@functools.lru_cache(maxsize=1)
def _catalog():
    """품목 사전. (item_seq, item_name, main_ingr, otc) 튜플 목록."""
    conn = connect()
    union = " UNION ".join(
        f"SELECT ITEM_SEQ, ITEM_NAME, MAIN_INGR, ETC_OTC_NAME FROM {t}" for t in TABS)
    # 같은 품목이 테이블마다 공백·개행이 다르게 들어 있어 UNION 이 별개 행으로
    # 뱉는다. 158건. 검색 pool 이 오염되고 화면에 공백이 딸려 나간다.
    # 이름을 strip 하고 seq 로 하나만 남긴다.
    rows, seen = [], set()
    for r in conn.execute(f"SELECT * FROM ({union})"):
        if r[0] in seen:
            continue
        seen.add(r[0])
        rows.append((r[0], (r[1] or "").strip(), r[2], r[3] or ""))
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


_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
_FORM = re.compile(r"^(.+?(?:정|캡슐|시럽|액|주|과립|산|건조시럽|서방정|츄정|점안액))(.{4,})$")
SHORT_NAME = 4           # 이 이하 글자의 품목명은 질의가 통째로 담고 있어야 후보


def _bare(name: str) -> str:
    s, prev = name, None
    while prev != s:
        prev, s = s, _PAREN.sub("", s)
    return s.strip()


def _long_enough(q: str, name: str) -> bool:
    """짧은 쿼리가 이 품목명에 붙어도 되는지. 그리고 짧은 품목명이 이 쿼리에 붙어도 되는지.

    질의 쪽 가드만 있었다. 그런데 실물 5장에서 "나드정"이 4장에 다 떴다.
    3글자 품목명은 OCR 조각 아무거나 부분일치로 붙는다. 9/9 의 식후->후라시닐정과
    같은 구조가 카탈로그 쪽에서 난 것이다. 품목명이 짧으면 질의가 그 이름을
    통째로 담고 있을 때만 후보로 인정한다.
    """
    # 양방향으로 본다. "아펜탈"(질의) 이 "아펜탈정"(이름) 의 앞부분인 건 정상이고,
    # "1일3회"(질의) 와 "나드정"(이름) 은 어느 쪽도 상대를 안 담으니 잡음이다.
    bare = _bare(name)
    if len(bare) < SHORT_NAME:                       # 3글자 이하: 포함 요구
        if not (bare in q or q in bare):
            return False
    elif len(bare) == SHORT_NAME:                    # 4글자: 한 글자 오차까지
        if fuzz.partial_ratio(bare, q) < ONE_CHAR_OFF:
            return False
    if len(q) >= FUZZY_BELOW:
        return True
    if len(q) < EXACT_BELOW:
        return q in name
    return fuzz.partial_ratio(q, name) >= ONE_CHAR_OFF


def _split_form(q: str) -> str | None:
    """실물 봉투는 괄호 없이 "제품명+성분명"을 한 줄에 찍고 OCR 이 한 덩어리로 읽는다.
        세프포독심정세프포독심프로세틸   ->   세프포독심정
    카탈로그는 괄호 문자열이라 통째로 퍼지 비교하면 점수가 안 나온다.
    제형 토큰에서 한 번 잘라 앞부분을 돌려준다. 잘릴 게 없으면 None."""
    m = _FORM.match(q)
    return m.group(1) if m else None


def search(query: str, limit: int = 5, prefix: int | None = None):
    q = (query or "").strip()
    if prefix:
        q = q[:prefix]
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
    """(확정된 약 | None, 후보목록, 상태)  상태: auto | suggest | none

    전체 문자열과 앞 8글자로 각각 매칭해서 둘이 같은 품목을 가리킬 때만 auto 다.

    두 측정이 반대 방향이었다.
      깨끗한 제품명 1,928건   8글자 절단이 오확정을 102 -> 16 으로 줄임 (함량 꼬리가 잡음)
      OCR 텍스트 30장+실물 5장  절단이 recall 을 깎음 (앞 글자가 깨지면 뒤로 만회 못 함)
    그래서 둘 다 돌리고 합의할 때만 확정한다. 실물에서 전체는 잘도스캡슐을 auto 로,
    8글자는 엘도스캡슐을 suggest 로 냈다. 같은 성분 다른 상표. 어느 쪽이 진짜인지
    모르는데 하나만 믿고 확정하면 안 된다. 불일치는 사람에게 넘긴다.

    질의에 제형 토큰 뒤로 긴 꼬리가 붙어 있으면(제품명+성분명 한 줄) 잘라서 재시도한다.
    """
    q = (query or "").strip()
    cands = search(q)
    if not cands or cands[0].confidence < SUGGEST:
        # 통째로는 안 붙는다. 제형에서 잘라 앞부분으로 한 번 더.
        head = _split_form(q)
        if head:
            alt = search(head)
            if alt and alt[0].confidence >= SUGGEST:
                cands, q = alt, head
    if not cands or cands[0].confidence < SUGGEST:
        return None, [], "none"
    top = cands[0]

    unambiguous = len(cands) == 1 or (top.confidence - cands[1].confidence) >= MARGIN
    if not (top.confidence >= AUTO and unambiguous):
        return None, cands, "suggest"

    # 8글자 절단으로 한 번 더. 같은 품목이어야 auto.
    short = search(q, prefix=PREFIX)
    if short and short[0].item_seq == top.item_seq and short[0].confidence >= AUTO:
        top.confirmed = True
        return top, cands, "auto"
    # 불일치. 양쪽 후보를 합쳐서 사람에게 보여준다.
    seen = {m.item_seq for m in cands}
    merged = cands + [m for m in short if m.item_seq not in seen]
    return None, merged[:5], "suggest"
