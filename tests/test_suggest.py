"""직접 입력 자동완성.

사용자가 친 글자가 아니라 여기서 고른 품목코드가 넘어간다. 그래야
오탈자가 원천적으로 안 생기고, 봉투 인쇄("100mg")와 허가 등재명
("100밀리그램")의 표기 차이가 문제가 되지 않는다. 직접 입력에서
제일 흔한 실패가 그 둘이었다.
"""
import re
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
    assert "사진으로 찾기" in r.text, "사진 경로를 안내하지 않는다"
    assert "이렇게 읽었습니다" not in r.text, "읽은 게 없는데 읽었다고 말한다"


def test_목록에서_안_골라도_친_글자는_확인_화면까지_간다(client):
    # 자바스크립트가 제출 시 env{i} 로 옮긴다. 그 경로가 죽으면
    # 사용자가 적은 약이 조용히 사라진다.
    r = client.post("/confirm", data={"env1": "에어탈"})
    assert r.status_code == 200
    assert "에어탈" in r.text


def test_후보마다_성분을_같이_내려준다(client):
    # 판정이 보는 것은 제품명이 아니라 성분이다. "타이레놀" 을 치면 여섯
    # 개가 같은 점수로 나오는데 그중 넷은 성분이 아세트아미노펜 하나로
    # 같다. 어느 것을 골라도 판정이 같다. 반대로 코푸 계열은 고르는 것마다
    # 성분 구성이 다르다. 화면이 그 차이를 보여줘야 사용자가 근거를 갖고
    # 고른다.
    hits = client.get("/suggest", params={"q": "타이레놀"}).json()
    assert hits, "후보가 없다"
    assert all("ing" in h for h in hits), "성분 항목이 빠졌다"
    acet = [h for h in hits if h["ing"] == ["아세트아미노펜"]]
    assert len(acet) >= 2, "같은 성분인 제품이 같게 안 나온다"


def test_성분이_다른_제품은_다르게_나온다(client):
    hits = client.get("/suggest", params={"q": "코푸"}).json()
    sets = {tuple(h["ing"]) for h in hits if h["ing"]}
    assert len(sets) >= 2, "성분 구성이 다른데 같게 나온다"


def test_2단_품목은_성분이_비어있다(client):
    hits = client.get("/suggest", params={"q": "바난"}).json()
    t2 = [h for h in hits if not h["dur"]]
    assert t2, "2단 품목이 없다"
    assert all(h["ing"] == [] for h in t2), "성분 정보가 없어야 한다"


def test_먹는약은_함량_제형이_달라도_한_줄로_묶는다(client):
    # 뮤테란을 치면 과립200 / 캡슐200 / 캡슐100 / 주사 네 줄이 나왔는데
    # 성분이 전부 아세틸시스테인 하나다. 판정이 같으므로 사용자에게 물을
    # 이유가 없는 선택이었다. 다만 주사는 따로 남긴다 — 봉투로 받아 먹는
    # 약을 찾는 사람에게 주사제를 같은 줄로 보여주면 안 된다.
    hits = client.get("/suggest", params={"q": "뮤테란"}).json()
    acet = [h for h in hits if h["ing"] == ["아세틸시스테인"]]
    oral = [h for h in acet if "주" not in h["name"]]
    assert len(oral) == 1, "먹는 약이 여러 줄로 나왔다: %s" % [h["name"] for h in oral]
    assert oral[0]["also"] >= 2, "묶인 개수를 안 알려준다"
    assert any("주" in h["name"] for h in acet), "주사제가 먹는 약에 합쳐졌다"


def test_묶은_줄은_함량을_찍지_않는다(client):
    # 레피졸정 5 / 10 / 15 / 30밀리그램을 한 줄로 묶어놓고 대표를
    # "레피졸정15밀리그램" 으로 찍으면, 5밀리그램을 드시는 분의 브리핑에
    # 15밀리그램이 인쇄된다. 판정은 성분만 보므로 결과는 같지만 출력물은
    # 틀린다. 브리핑은 진료 때 보여주는 것이라 없는 숫자를 지어내면 안 된다.
    hits = client.get("/suggest", params={"q": "레피졸"}).json()
    g = [h for h in hits if h["also"] > 0]
    assert g, "레피졸이 묶이지 않았다"
    for h in g:
        assert not re.search(r"\d", h["name"]),             "묶은 줄에 함량이 남았다: %s" % h["name"]


def test_성분이_다르면_안_묶는다(client):
    # 아세트아미노펜 하나짜리와 아세트아미노펜+파마브롬은 다른 약이다.
    hits = client.get("/suggest", params={"q": "타이레놀"}).json()
    sets = {tuple(h["ing"]) for h in hits if h["ing"]}
    assert len(sets) >= 2, "성분 구성이 다른데 묶였다"


def test_대표_이름은_검색어에_가까운_것을_쓴다(client):
    # 가장 짧은 이름을 고르면 "뮤테란주사" 가 대표가 된다. 주사제는
    # 사용자가 봉투로 받아온 약이 아니다.
    hits = client.get("/suggest", params={"q": "뮤테란캅셀"}).json()
    assert hits and "캡슐" in hits[0]["name"], "대표가 %s 다" % hits[0]["name"]


def test_묶어서_고르면_함량이_안_찍힌다(client):
    # 자동완성에서 함량만 다른 약을 한 줄로 묶어 보여줬는데, 고른 뒤에
    # 카탈로그 원래 이름을 찍으면 "레피졸정" 을 고른 사람 화면에
    # "레피졸정15밀리그램" 이 나온다. 우리 브리핑은 그 이름을 의사에게
    # 보여주라고 말한다. 사용자가 정하지 않은 용량이다.
    import re
    hits = client.get("/suggest", params={"q": "레피졸"}).json()
    g = next((h for h in hits if h["also"]), None)
    assert g, "묶인 줄이 없다"
    assert not re.search(r"\d", g["name"]), "묶은 줄 이름에 숫자가 있다: %s" % g["name"]

    r = client.post("/confirm", data={"seq1": "%s|%s" % (g["seq"], g["name"])})
    shown = re.findall(r'<span class="text-sm">([^<]+)</span>', r.text)
    assert shown and shown[0] == g["name"], "고른 이름과 다르게 찍혔다: %s" % shown[:1]


def test_사진에서_확정된_약은_함량을_유지한다(client):
    # OCR 이 봉투에 인쇄된 함량을 실제로 읽었으므로 아는 정보다.
    # 묶어서 고른 것만 떼고 이건 그대로 보여준다.
    import re
    hits = client.get("/suggest", params={"q": "레피졸"}).json()
    g = next(h for h in hits if h["also"])
    r = client.post("/confirm", data={"seq1": g["seq"]})      # 표시이름 없이
    shown = re.findall(r'<span class="text-sm">([^<]+)</span>', r.text)
    assert shown and re.search(r"\d", shown[0]), "함량이 사라졌다: %s" % shown[:1]


def test_묶어서_고른_이름이_결과_화면까지_간다(client):
    # 확인 화면에서 "레피졸정" 이라고 보여준 것을 사용자가 확인했는데
    # 브리핑에 "레피졸정15밀리그램" 이 찍히면, 확인한 것과 다른 이름을
    # 의사에게 보여주라고 말하게 된다. confirm.html 이 코드만 넘겨서
    # 마지막 단계에서 함량이 되살아나던 것을 막는다.
    import re
    hits = client.get("/suggest", params={"q": "레피졸"}).json()
    g = next(h for h in hits if h["also"])

    h1 = client.post("/confirm", data={"seq1": "%s|%s" % (g["seq"], g["name"])}).text
    v = re.search(r'name="pick1" value="([^"]+)"', h1).group(1)
    assert "|" in v, "확인 화면이 표시이름을 안 넘긴다: %s" % v

    h2 = client.post("/result", data={"pick1": v}).text
    shown = re.findall(r">([^<>]*레피졸[^<>]*)<", h2)
    assert shown, "결과에 약이 없다"
    assert not any(re.search(r"\d", x) for x in shown), "함량이 되살아났다: %s" % shown[:2]


def test_사진에서_확정된_약은_코드만_넘긴다(client):
    import re
    hits = client.get("/suggest", params={"q": "레피졸"}).json()
    g = next(h for h in hits if h["also"])
    h = client.post("/confirm", data={"seq1": g["seq"]}).text
    v = re.search(r'name="pick1" value="([^"]+)"', h).group(1)
    assert "|" not in v, "카탈로그 원본인데 표시이름이 붙었다: %s" % v
