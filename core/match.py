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
from dataclasses import replace
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
def _permit():
    """2단. 식약처 의약품 제품 허가목록. 인식만 하고 판정은 못 한다.

    DUR 카탈로그는 상호작용 정보가 있는 품목만 담는다. 그런데 실물 약봉투
    4장 24개 약 중 절반이 거기 없었다. 정장제, 유산균, 일반의약품, 특정
    제조사 제품이 그렇다.

    없으면 두 가지가 일어난다. 화면에 안 떠서 사용자가 "내 약을 못 읽었나"
    하거나, 더 나쁘게는 비슷한 다른 약이 뜬다. 실물에서 이렇게 났다.

        에리우스정(DUR 에 없음)  ->  에이리스정  80점
        메드닌정(DUR 에 없음)    ->  메드론정(스테로이드)  75점

    두 번째가 위험하다. 안 먹는 약이 DUR 판정에 들어간다. 허가목록을 얹으면
    진짜 이름이 top1 이 되고 엉뚱한 약이 밀린다. 임계를 안 건드리고 고쳐진다.

    취하된 품목도 넣는다. 처음에는 뺐는데 실물에서 걸렸다.

        킨도라제정(스트렙토키나제스트렙토도르나제)   취하

    봉투에 인쇄돼 있으면 사용자는 그 약을 실제로 갖고 있다. 약국 재고이거나
    오래된 봉투다. "취하라서 안 보여준다" 는 우리 편의지 사용자 사정이 아니다.
    허가 상태는 판정에 안 쓰고 인식에만 쓰므로 넣어도 위험이 없다.
    """
    conn = connect()
    try:
        rows = [(r[0], (r[1] or "").strip())
                for r in conn.execute(
                    "SELECT ITEM_SEQ, ITEM_NAME FROM permit")]
    except Exception:
        rows = []          # permit 테이블이 없어도 1단만으로 돈다
    finally:
        conn.close()
    return rows


@functools.lru_cache(maxsize=1)
def _permit_names():
    return [n for _, n in _permit()]


@functools.lru_cache(maxsize=1)
def _dur_seqs():
    return {seq for seq, *_ in _catalog()}


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
    hits = _rank(q, names, limit * 4)

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


# 같은 말인데 표기가 갈리는 것들. 카탈로그 안에서 둘 다 실재한다.
# 봉투 인쇄와 허가 등재명이 다르면 한 글자 차이로 다른 제형이 1위가 된다.
#
#   뮤테란캅셀  ->  뮤테란과립200밀리그램   67.5   틀린 제형
#   뮤테란캡슐  ->  뮤테란캡슐200밀리그램   90.0   정답
#
# 카탈로그 등재 건수로 주류를 정했다. 1단+2단 65,995품목 기준으로
# 캡슐 9,358 대 캅셀 278, 밀리그램 25,995 대 밀리그람 1,227 이다.
# 소수파를 주류로 옮긴다. 양쪽 다 정규화하므로
# 어느 쪽으로 인쇄돼 있든 만난다.
_SPELL = [("캅셀", "캡슐"), ("캅슐", "캡슐"),
          ("밀리그람", "밀리그램"), ("마이크로그람", "마이크로그램")]


def _spell(s: str) -> str:
    for a, b in _SPELL:
        s = s.replace(a, b)
    return s


def _head(name: str) -> str:
    """첫 괄호 앞까지. 채점용 짧은 이름이다.

    _bare() 는 끝의 괄호만 떼는데 성분명 안에 괄호가 또 있으면 못 뗀다.
        애니크라정375밀리그램(아목시실린수화물-묽은클라불란산칼륨(2:1))
        _bare  -> ...묽은클라불란산칼륨      여전히 길다
        _head  -> 애니크라정375밀리그램       이게 필요한 것
    """
    i = name.find("(")
    return (name[:i] if i > 0 else name).strip()


@functools.lru_cache(maxsize=2)
def _normed(names: tuple):
    """카탈로그 이름을 미리 정규화해 둔다.

    _rank 가 질의마다 46,554개를 두 번씩 재가공하고 있었다. 약 4개면
    37만 번이다. 카탈로그는 프로세스 수명 동안 바뀌지 않으므로 한 번만
    만든다. 채점 결과는 그대로다.
    """
    return [_spell(n) for n in names], [_spell(_head(n)) for n in names]


def _rank(q: str, names, limit: int, cutoff: float = 50):
    """괄호를 뗀 이름으로도 재서 높은 쪽을 쓴다.

    카탈로그 이름이 "제품명(성분명)" 이라 성분명이 길수록 손해를 본다.
    WRatio 가 길이 비율로 깎기 때문이다. 실물에서 이렇게 났다.

        OCR 줄   애니크라정375밀리그램아목
        정답     애니크라정375밀리그램(아목시실린수화물-묽은클라불란산칼륨(2:1))  37자  83.6
        오답     유니크라정375밀리그램                                  12자  84.6

    내용은 정답이 훨씬 가까운데 길이 때문에 진다. 괄호를 떼면 92.3 대 84.6 이다.
    그래서 둘 다 재고 높은 쪽을 점수로 쓴다. 괄호 안 성분명이 질의에 실제로
    들어 있으면 원래 점수가 이기므로 손해가 없다.
    """
    qn = _spell(q)
    nfull, nhead = _normed(tuple(names))
    full = process.extract(qn, nfull,
                           scorer=fuzz.WRatio, limit=limit, score_cutoff=cutoff)
    bare = process.extract(qn, nhead,
                           scorer=fuzz.WRatio, limit=limit, score_cutoff=cutoff)
    best: dict[int, float] = {}
    for _, s, i in list(full) + list(bare):
        if s > best.get(i, 0):
            best[i] = s
    return [(names[i], s, i) for i, s in
            sorted(best.items(), key=lambda kv: -kv[1])[:limit]]


def _search_permit(q: str, floor: float = 0.0):
    """2단에서 최선 후보 하나. 1단에 이미 있는 품목은 건너뛴다.

    floor 는 "이 점수 이하는 어차피 버린다" 는 호출자의 약속이다.
    rapidfuzz 에 그대로 넘기면 내부에서 가지치기를 한다. 46,554개를
    두 패스 도는 비용이 실측으로 cutoff 50 에서 400ms, 90 에서 158ms,
    95 에서 54ms 다. 버려질 후보를 안 고르는 것뿐이라 결과는 같다.
    """
    names = _permit_names()
    if not names or len(q) < 2:
        return None
    if floor >= 1.0:
        # 점수는 1.0 을 못 넘는다. 호출자가 버릴 것이 확정이라 돌 필요가 없다.
        return None
    # 반올림(round(s/100, 3))에 걸려 경계값이 잘리지 않도록 반 점 낮춘다.
    cutoff = min(100.0, max(50.0, floor * 100 - 0.5))
    hits = _rank(q, names, 5, cutoff)
    rows, dur = _permit(), _dur_seqs()
    for _, score, idx in hits:
        seq, name = rows[idx]
        if seq in dur:
            continue
        if not _long_enough(q, name):
            continue
        return Medication(item_seq=seq, product_name=name, otc="",
                          ingredients=[], confidence=round(score / 100, 3),
                          dur_covered=False)
    return None


def _resolve(query: str):
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
        # 1단(DUR)에 없다. 2단(허가목록)에서 찾는다. 이름만 맞히고 판정은 못 한다.
        p = _search_permit(q)
        if p:
            return (p, [p], "auto") if p.confidence >= AUTO else (None, [p], "suggest")
        return None, [], "none"
    top = cands[0]

    # 1단 후보가 있어도 2단이 더 잘 맞으면 그쪽이다. 에리우스정(2단)이
    # 에이리스정(1단)에 80점으로 붙던 것을 이게 막는다.
    p = _search_permit(q, floor=top.confidence + MARGIN)
    if p and p.confidence > top.confidence + MARGIN:
        return (p, [p] + cands[:2], "auto") if p.confidence >= AUTO             else (None, [p] + cands[:2], "suggest")

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


def _clone(res):
    """캐시가 돌려주는 결과는 복사본이어야 한다.

    resolve 는 확정 시 top.confirmed = True 로 Medication 을 변형한다.
    캐시된 객체를 그대로 넘기면 호출자끼리 같은 객체를 공유하게 되고,
    한 봉투에서 확인한 것이 다른 봉투에 번진다. 후보 리스트도 같이
    복사한다 — 리스트는 새로 만들어도 안의 객체는 공유되기 때문이다.

    top 은 보통 cands[0] 과 같은 객체다. 그 관계를 복사본에서도 지킨다.
    Ingredient 는 어디서도 변형하지 않으므로 공유해도 된다.
    """
    top, cands, status = res
    new = [replace(c, ingredients=list(c.ingredients)) for c in cands]
    if top is None:
        return None, new, status
    for old, cp in zip(cands, new):
        if old is top:
            return cp, new, status
    return replace(top, ingredients=list(top.ingredients)), new, status


# 항목 1건이 실측 4.6KB 다. 2048 이면 최대 9MB 로, 2GB 박스에서 무시할
# 수 있다. 크기를 이만큼 잡는 이유는 한 장 안의 중복이 아니다. 사진 13장
# 실측 중복률은 2% 뿐이다 — 같은 약이 두 번 인쇄돼도 OCR 이 다르게 읽는다
# (한미아스피린장용정100n9 / 한미아스피린장용정1001림). 이득은 요청 사이에
# 있다. 여러 사람이 흔한 약을 올리면 두 번째부터 0ms 다.
@functools.lru_cache(maxsize=2048)
def _resolve_cached(q: str):
    return _resolve(q)


def resolve(query: str):
    """줄 -> (확정 약, 후보, 상태).

    같은 줄이 반복해서 들어온다. 약국 영수증은 같은 약이 표와 설명란에
    두 번씩 인쇄되고, 봉투 여러 장에 같은 약이 걸친다. 한 줄에 200ms 대가
    붙으므로 중복만 걷어내도 그만큼 준다.
    """
    return _clone(_resolve_cached((query or "").strip()))


resolve.cache_clear = _resolve_cached.cache_clear
resolve.cache_info = _resolve_cached.cache_info


def search_all(query: str, limit: int = 6):
    """자동완성용. 1단(DUR)과 2단(허가목록)을 합쳐 점수순으로 돌려준다.

    search() 는 1단만 본다. 그래서 바난정 같은 2단 전용 품목(27,113건)이
    자동완성에 아예 안 뜬다. 사용자는 봉투에 인쇄된 이름을 치는데 목록에
    없으면 직접 입력을 포기한다.

    2단은 "1단이 모자랄 때만" 이 아니라 항상 돈다. 개수로 걸면 안 된다 —
    1단은 score_cutoff=50 이라 쓰레기로도 limit 이 거의 항상 차고, 그러면
    2단 코드가 실행되지 않는다. 실물 인쇄명으로 이렇게 났다.

        액티피드시럽   정답 없음, 1위 큐피시럽
        위피드정      정답 없음, 1위 피드로정
        에리우스정    정답 없음, 1위 에이리스정

    대신 2단에 1단 꼴찌 점수를 문턱으로 넘긴다. 그보다 낮은 점수는 합쳐도
    상위 limit 에 못 드니 결과가 안 바뀌고, rapidfuzz 가 내부에서 가지친다.
    1단이 강하면 거의 공짜고 1단이 약할 때만 제값을 낸다 — 2단이 필요한
    경우가 정확히 그때다.

    병합은 점수순이다. 1단을 무조건 앞세우지 않는다. resolve() 가 이미
    2단이 MARGIN 만큼 높으면 그쪽을 택한다(에리우스정 대 에이리스정).
    자동완성만 반대로 동작하면 안 된다. 동점이면 정렬이 안정적이라
    판정 가능한 1단이 앞에 남는다.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []

    out = search(q, limit)
    floor = out[-1].confidence if len(out) >= limit else 0.0

    rows, dur = _permit(), _dur_seqs()
    seen = {m.item_seq for m in out}
    extra = []
    for _, score, idx in _rank(q, _permit_names(), limit * 3,
                               min(100.0, max(50.0, floor * 100 - 0.5))):
        seq, name = rows[idx]
        if seq in dur or seq in seen:
            continue
        if not _long_enough(q, name):
            continue
        seen.add(seq)
        extra.append(Medication(item_seq=seq, product_name=name, otc="",
                                ingredients=[], confidence=round(score / 100, 3),
                                dur_covered=False))
    if not extra:
        return out
    return sorted(out + extra, key=lambda m: -m.confidence)[:limit]
