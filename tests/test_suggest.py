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


# --- 실물 약봉투에 인쇄된 이름 -----------------------------------------------
#
# 처음 구현은 2단 폴백을 개수로 걸었다.
#     out = search(q, limit)
#     if len(out) >= limit: return out
# 1단은 score_cutoff=50 이라 쓰레기로도 limit 이 거의 항상 찬다. 그래서 2단
# 코드가 실행되지 않았고, 아래 이름 전부가 후보 6개 안에 없었다. 사용자는
# 봉투에 인쇄된 이름을 그대로 쳤는데 자기 약은 없고 그럴듯한 오답만 떴다.
#
#     액티피드시럽 -> 큐피시럽 / 위피드정 -> 피드로정 / 에리우스정 -> 에이리스정
#
# 당시 자체 검증은 통과했다. 하필 1단 후보가 1건뿐이라 우연히 폴백을 타던
# 질의("바난")를 내가 골라서 확인했기 때문이다. 검증 입력을 스스로 고르면
# 안 된다는 사례라 실물 이름을 그대로 박아둔다.
PRINTED = [
    "액티피드시럽", "위피드정", "애니크라정", "킨도라제정", "비오플250산",
    "에리우스정", "레보프라이드", "비졸본정", "알게나정", "타스펜8시간이알서방정",
]


@pytest.mark.parametrize("printed", PRINTED)
def test_봉투에_인쇄된_이름이_후보에_들어온다(client, printed):
    hits = names(client.get("/suggest", params={"q": printed}))
    head = printed[:4]
    assert any(n.startswith(head) for n in hits), \
        "%s 를 쳤는데 후보에 없다. 나온 것: %s" % (printed, hits)


def test_아무것도_안_고르고_제출하면_막다른_길로_안_간다(client):
    # 자동완성은 목록에서 클릭해야 품목이 정해진다. 그런데 타이핑하는
    # 사람은 이름을 다 치고 엔터를 누른다. 칸이 하나뿐인 폼에서 엔터는
    # 그대로 제출이라 아무것도 안 고른 채 넘어간다.
    # 그냥 두면 "이렇게 읽었습니다" 화면이 약 0개로 뜬다. 읽은 게 없는데
    # 읽었다고 말하는 것이라 사용자는 뭘 해야 할지 모른다.
    r = client.post("/confirm", data={"label1": "내과"})
    assert r.status_code == 200
    assert "골라주세요" in r.text, "빈 제출인데 안내가 없다"
    assert "사진에서 약 찾기" in r.text, "사진 경로를 안내하지 않는다"
    assert "이렇게 읽었습니다" not in r.text, "읽은 게 없는데 읽었다고 말한다"


def test_목록에서_안_골라도_친_글자는_확인_화면까지_간다(client):
    # 자바스크립트가 제출 시 env{i} 로 옮긴다. 그 경로가 죽으면
    # 사용자가 적은 약이 조용히 사라진다.
    r = client.post("/confirm", data={"env1": "에어탈"})
    assert r.status_code == 200
    assert "에어탈" in r.text
