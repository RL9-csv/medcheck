"""직접 입력 자동완성.

사용자가 친 글자가 아니라 여기서 고른 품목코드가 넘어간다. 그래야
오탈자가 원천적으로 안 생기고, 봉투 인쇄("100mg")와 허가 등재명
("100밀리그램")의 표기 차이가 문제가 되지 않는다. 직접 입력에서
제일 흔한 실패가 그 둘이었다.
"""
import pytest
from fastapi.testclient import TestClient

import main
from core.match import _catalog, _permit, _dur_seqs


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def names(r):
    return [x["name"] for x in r.json()]


def test_두_글자면_후보가_나온다(client):
    r = client.get("/suggest", params={"q": "에어탈"})
    assert r.status_code == 200
    assert any("에어탈" in n for n in names(r))


def test_한_글자는_묻지_않는다(client):
    # 카탈로그 전체를 훑는 비용이 타자 한 번마다 나가면 안 된다.
    assert client.get("/suggest", params={"q": "에"}).json() == []
    assert client.get("/suggest", params={"q": ""}).json() == []


def test_2단_전용_품목도_자동완성에_나온다(client):
    # search() 는 1단만 본다. 2단 전용이 27,113건이라 이게 빠지면
    # 봉투에 인쇄된 이름을 쳤는데 목록에 없는 일이 생긴다.
    r = client.get("/suggest", params={"q": "바난"})
    assert any("바난" in n for n in names(r)), "2단 품목이 자동완성에서 빠졌다"


def test_판정_가능_여부를_같이_내려준다(client):
    # 고르기 전에 "이 약은 성분 정보가 없다" 를 말할 수 있어야 한다.
    hits = client.get("/suggest", params={"q": "바난"}).json()
    assert hits and hits[0]["dur"] is False
    hits = client.get("/suggest", params={"q": "에어탈"}).json()
    assert hits and hits[0]["dur"] is True


def test_limit_은_상한이_있다(client):
    r = client.get("/suggest", params={"q": "정", "limit": 999})
    assert len(r.json()) <= 10


def test_고른_품목코드가_확인_화면까지_간다(client):
    dur = _dur_seqs()
    s1 = _catalog()[0][0]
    s2 = next(s for s, _ in _permit() if s not in dur)
    r = client.post("/confirm", data={"seq1": [s1, s2], "label1": "내과"})
    assert r.status_code == 200
    assert _catalog()[0][1] in r.text


def test_고르지_않고_친_글자도_버리지_않는다(client):
    # 목록에서 안 고른 입력을 조용히 버리면 사용자는 자기가 적은 약이
    # 어디 갔는지 모른다. 기존 경로로 후보를 찾아 보여줘야 한다.
    r = client.post("/confirm", data={"env1": "에어탈정"})
    assert r.status_code == 200
    assert "에어탈" in r.text


def test_같은_품목을_두_번_고르면_한_번만_들어간다(client):
    s = _catalog()[0][0]
    r = client.post("/confirm", data={"seq1": [s, s]})
    assert r.status_code == 200
    assert r.text.count('value="%s"' % s) <= 2   # 라디오 하나 + 히든 정도
