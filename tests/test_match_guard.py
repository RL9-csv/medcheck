"""짧은 쿼리 오탐 방지.

OCR은 약봉투를 "식후", "1일", "내과" 같은 조각으로 잘라놓는다.
rapidfuzz WRatio 는 짧은 문자열에 부분일치 보너스를 주면서 길이 비율
페널티가 없어서, 이 조각들이 실제 약품에 0.60 으로 붙었다.

사용자가 "골라주세요"에서 아무거나 고르면 먹지도 않은 약이 확정되어
DUR 엔진에 들어간다. 화면이 잡음으로 뒤덮이는 것보다 이쪽이 더 위험하다.
"""
import pytest

from core.match import EXACT_BELOW, FUZZY_BELOW, resolve, search

# 실제 약봉투에 찍히는 문구. 어느 것도 약으로 잡히면 안 된다.
NOISE = [
    "식후", "식전", "취침전", "1일", "2회", "3회", "30분", "1일2회",
    "내과", "의원", "약사", "조제일자", "총3종", "행복내과의원",
    "약사김00총3종", "조제일자2026-09-07", "1일2회식후30분",
]

# 사람이 직접 입력하거나 OCR이 제대로 읽는 이름. 살아남아야 한다.
REAL = ["에어탈", "에어탈정", "타이레놀", "페니라민", "아펜탈",
        "로수젯", "아모잘탄", "아세클로페낙"]


@pytest.mark.parametrize("q", NOISE)
def test_잡음은_약으로_잡히지_않는다(q):
    med, cands, status = resolve(q)
    assert status == "none", f"{q!r} -> {status} / {cands[0].product_name if cands else ''}"
    assert med is None


@pytest.mark.parametrize("q", REAL)
def test_실제_약이름은_살아남는다(q):
    med, cands, status = resolve(q)
    assert status != "none", f"{q!r} 가 걸러졌다"
    assert cands, f"{q!r} 후보 없음"


def test_짧은_쿼리는_제품명에_포함될_때만_후보가_된다():
    """가드의 규칙 자체를 고정한다. 임의의 감점 계수가 아니라 포함 여부다."""
    for c in search("에어탈"):
        assert "에어탈" in c.product_name
    assert search("식후") == []


@pytest.mark.parametrize("typo,want", [
    ("에어탈젓", "에어탈"),      # 정 -> 젓
    ("타이래놀", "타이레놀"),     # 레 -> 래
    ("페니라믄", "페니라민"),     # 민 -> 믄
    ("아모잘탈", "아모잘탄"),     # 탄 -> 탈
])
def test_네글자_오타는_한_글자까지_흡수한다(typo, want):
    """가드가 OCR 오타 흡수 능력까지 죽이면 안 된다."""
    assert len(typo) == FUZZY_BELOW - 1
    cands = search(typo)
    assert cands, f"{typo!r} 가 통째로 걸러졌다"
    assert any(want in c.product_name for c in cands)


def test_세글자_이하는_오차를_허용하지_않는다():
    """의도된 손실이다. '아펜탕'(오타)과 '30분'(잡음)이 둘 다 partial 80 이라
    3글자에서는 둘을 구분할 방법이 없다. 먹지도 않은 약을 확정하는 것보다
    놓치고 직접 입력받는 편이 낫다."""
    assert len("아펜탕") < EXACT_BELOW
    assert search("아펜탕") == []
    assert search("30분") == []


# --- 2단 후보에도 같은 문턱을 적용한다 ---------------------------------------
#
# 1단은 confidence >= SUGGEST(0.60) 여야 후보로 나가는데, 2단 폴백에는
# 문턱이 없어서 0.50 만 넘으면 나갔다. 같은 화면에 기준이 두 개였다.
# 그래서 약이 아닌 줄이 후보로 떴다.
#
#   투여횟수3              0.514 로 후보 표시
#   위장장애가나타날수있어요   0.514 로 후보 표시
#
# 사용자가 먹지도 않는 약을 고르라는 화면을 보게 된다. 이 프로젝트가 가장
# 위험하다고 정한 실패로 가는 문이다.

NOT_DRUG = [
    "투여횟수3", "위장장애가나타날수있어요", "위장장애가 나타날 수 있어요",
    "복용후졸음이올수있어요", "충분한물과함께",
]


@pytest.mark.parametrize("junk", NOT_DRUG)
def test_약이_아닌_문장은_후보로_내보내지_않는다(junk):
    med, cands, status = resolve(junk)
    assert status == "none", "%s 가 %s 로 나갔다: %s" % (
        junk, status, [c.product_name for c in cands])


# 문턱을 올려도 2단 실제 품목은 그대로 살아야 한다. 양방향을 다 막는다.
TIER2_REAL = ["바난정", "위피드정", "킨도라제정", "액티피드시럽", "에리우스정"]


@pytest.mark.parametrize("name", TIER2_REAL)
def test_2단_실제_품목은_문턱을_올려도_살아있다(name):
    med, cands, status = resolve(name)
    assert status != "none", "%s 가 사라졌다" % name
    assert any(c.product_name.startswith(name[:4]) for c in cands)
