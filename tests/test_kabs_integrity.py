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
