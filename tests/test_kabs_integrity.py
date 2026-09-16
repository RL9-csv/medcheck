"""KABS 파싱 무결성. 원 논문 보고치와 대조한다."""
import sqlite3, sys
from pathlib import Path
import pytest

DB = Path(__file__).resolve().parent.parent / "data" / "dur.db"

# Jun K et al., Geriatr Gerontol Int. 2019;19(7):628-634 본문 보고치
PAPER = {3: 56, 2: 23, 1: 59, 0: 356}
PAPER_TOTAL = 494


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    yield c; c.close()


def test_total_close_to_paper(conn):
    """병기명(A; B) 처리 차이로 ±3까지 허용. 그 이상이면 파싱 누락이다."""
    n = conn.execute("SELECT COUNT(*) FROM kabs").fetchone()[0]
    assert abs(n - PAPER_TOTAL) <= 3, f"총 {n}종, 논문 {PAPER_TOTAL}종 — 차이 과다"


def test_score_distribution(conn):
    got = dict(conn.execute("SELECT kabs, COUNT(*) FROM kabs GROUP BY kabs"))
    for score, expected in PAPER.items():
        assert abs(got.get(score, 0) - expected) <= 3, \
            f"{score}점: 파싱 {got.get(score,0)} vs 논문 {expected}"


def test_no_duplicate_ingredients(conn):
    dups = conn.execute(
        "SELECT ingredient, COUNT(*) c FROM kabs GROUP BY LOWER(ingredient) HAVING c>1"
    ).fetchall()
    assert not dups, f"중복 성분: {[d['ingredient'] for d in dups]}"


def test_scores_in_range(conn):
    bad = conn.execute("SELECT COUNT(*) FROM kabs WHERE kabs NOT IN (0,1,2,3)").fetchone()[0]
    assert bad == 0, f"0~3 범위 밖 점수 {bad}건"


@pytest.mark.parametrize("name,score", [
    ("Chlorpheniramine", 3), ("Diphenhydramine", 3), ("Amitriptyline", 3),
    ("Olanzapine", 3), ("Clozapine", 3), ("Hydroxyzine", 3),
    ("Cimetidine", 2), ("Quetiapine", 2), ("Tramadol", 2), ("Paroxetine", 2),
    ("Diazepam", 1), ("Ranitidine", 1), ("Digoxin", 1),
    ("Acetaminophen", 0), ("Aspirin", 0), ("Donepezil", 0),
])
def test_known_scores(conn, name, score):
    """대표 성분의 점수가 논문 표와 일치하는지."""
    r = conn.execute("SELECT kabs FROM kabs WHERE LOWER(ingredient) LIKE ? LIMIT 1",
                     (f"%{name.lower()}%",)).fetchone()
    assert r is not None, f"{name} 이 KABS 테이블에 없음"
    assert r["kabs"] == score, f"{name}: 파싱 {r['kabs']}점, 기대 {score}점"


def test_unassessed_is_not_zero(conn):
    """0점(평가됨)과 미수록(모름)이 구분되는지 — kabs_map에 0점이 실제로 있어야 한다."""
    z = conn.execute("SELECT COUNT(*) FROM kabs_map WHERE kabs=0").fetchone()[0]
    assert z > 0, "kabs_map에 0점 성분이 없음 — 0점과 미평가가 뭉개진 상태"


def test_화면_문구의_목록_크기가_데이터와_같다():
    """근거 문구는 심사자가 원문과 대조할 수 있는 몇 안 되는 지점이다.
    여기가 어긋나면 나머지 수치의 신뢰도까지 같이 떨어진다.

    원자료의 분류별 N 표기를 더하면 494지만 실제 나열된 성분은 493개다
    (Antidepressants N=20/나열 19, Antipsychotics N=25/나열 24,
    Mineral and vitamins N=15/나열 16). 우리가 적재한 493개는 나열된
    성분과 이름 단위까지 일치한다. 화면에는 대조 가능한 493을 쓴다.
    """
    import re
    from core import cite
    from core.db import connect

    n = connect().execute("select count(*) from kabs").fetchone()[0]
    said = re.search(r"(\d+)개 목록", cite.UNASSESSED)
    assert said, "문구에서 목록 크기를 못 읽었다"
    assert int(said.group(1)) == n, \
        "화면 문구 %s개, 데이터 %d개" % (said.group(1), n)
