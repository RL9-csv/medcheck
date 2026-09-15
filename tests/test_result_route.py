"""확정한 약이 결과 화면에서 사라지지 않는다.

2단(허가목록)은 이름만 맞히고 성분 판정은 못 한다. 그 설계는 맞다.
그런데 _load_meds 가 1단(DUR)만 보고 없으면 continue 해서, 사람이 확인
화면에서 분명히 고른 2단 약이 결과에서 아무 말 없이 빠졌다. 실물 13장에서
auto 확정 43건 중 20건(47%)이 2단이었다. 봉투 한 장이 전부 2단이면
"0 개 약" 이 뜨고 브리핑이 통째로 비었다.

"판정을 못 한다" 와 "목록에서 지운다" 는 다르다. 목록에는 올리되 판정
대상이 아니라고 말해야 한다. 말하지 않으면 사용자는 "검토했는데 문제
없음" 으로 읽고, 그게 조용히 지우는 것보다 나쁘다.
"""
import re
import pytest
from fastapi.testclient import TestClient

import main
from core.match import _catalog, _permit, _dur_seqs

NOTICE = "이름만 확인했습니다"


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def seq2():
    """2단에만 있는 품목코드."""
    dur = _dur_seqs()
    return [s for s, _ in _permit() if s not in dur][:2]


@pytest.fixture(scope="module")
def seq1():
    """1단에 있는 품목코드."""
    return [r[0] for r in _catalog()[:2]]


def counts(html):
    m = re.search(r'font-semibold">(\d+)</span>\s*<span[^>]*>개 약 · 봉투 (\d+)개', html)
    assert m, "결과 화면에서 약/봉투 개수를 못 읽었다"
    return int(m.group(1)), int(m.group(2))


def test_2단만_고른_봉투가_사라지지_않는다(client, seq2):
    r = client.post("/result", data={"pick1": seq2[0]})
    assert r.status_code == 200
    meds, sources = counts(r.text)
    assert meds == 1, "확정한 2단 약이 결과에서 빠졌다"
    assert sources == 1, "약이 하나뿐인 봉투가 통째로 빠졌다"


def test_2단_약은_판정_대상이_아님을_말한다(client, seq2):
    r = client.post("/result", data={"pick1": seq2[0]})
    assert NOTICE in r.text, "성분 판정을 못 했다는 사실을 화면이 말하지 않는다"


def test_1단만이면_안내를_띄우지_않는다(client, seq1):
    r = client.post("/result", data={"pick1": seq1[0]})
    assert r.status_code == 200
    assert counts(r.text)[0] == 1
    assert NOTICE not in r.text, "판정이 된 약에까지 판정 불가 안내가 붙었다"


def test_1단과_2단을_같이_고르면_둘_다_남는다(client, seq1, seq2):
    r = client.post("/result", data={"pick1_0": seq1[0], "pick1_1": seq2[0]})
    assert r.status_code == 200
    assert counts(r.text)[0] == 2
    assert NOTICE in r.text


def test_봉투_여러_장이_전부_2단이어도_각각_남는다(client, seq2):
    r = client.post("/result", data={"pick1": seq2[0], "pick2": seq2[1],
                                     "label1": "아침", "label2": "저녁"})
    assert r.status_code == 200
    assert counts(r.text) == (2, 2)


def test_확정_약이_없으면_빈_결과가_정상_출력된다(client):
    r = client.post("/result", data={})
    assert r.status_code == 200
    assert counts(r.text) == (0, 0)
