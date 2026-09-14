"""식약처 의약품 제품 허가정보를 받아 2단 카탈로그 테이블을 만든다.

DUR 카탈로그(19,441건)는 상호작용 정보가 있는 품목만 담는다. 그런데 실물
약봉투 4장 24개 약 중 절반이 거기 없었다. 정장제, 유산균, 일반의약품,
특정 제조사 제품이 그렇다.

없으면 두 가지가 일어난다.
  화면에 안 뜬다          사용자는 "내 약을 못 읽었나" 한다
  비슷한 다른 약이 뜬다    에리우스정(없음) -> 에이리스정 80점

두 번째가 위험하다. 안 먹는 약이 DUR 판정에 들어간다.

허가 목록 42,986건을 인식용으로 얹으면 둘 다 해결된다. 이름은 맞히되
"확인 대상 정보가 없습니다"로 표시한다. 없다는 걸 없다고 말하는 것이다.
"""
import os, pathlib, re, sqlite3, sys, time
import requests

# 두 곳에서 받는다. 공개 API 하나가 전체를 안 준다.
#   허가정보   42,986건   제품 허가 원장
#   낱알식별   25,426건   알약 식별용. 40%가 허가정보에 없는 품목이다
# 실물에서 비졸본정이 허가정보엔 없고 낱알식별에만 있었다.
SOURCES = [
    ("permit", "https://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07"),
    ("grain",  "https://apis.data.go.kr/1471000/MdcinGrnIdntfcInfoService03/getMdcinGrnIdntfcInfoList03"),
]
DB = pathlib.Path(__file__).resolve().parent.parent / "data" / "dur.db"
ROWS = 500         # API 최대치. 86페이지로 끝난다


def _key() -> str:
    for p in (pathlib.Path(".env"), pathlib.Path(__file__).parent.parent / ".env"):
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                if ln.startswith("MFDS_PERMIT_KEY="):
                    return ln.split("=", 1)[1].strip()
    k = os.environ.get("MFDS_PERMIT_KEY")
    if not k:
        sys.exit("MFDS_PERMIT_KEY 가 .env 나 환경변수에 없다")
    return k


def _get(url: str, key: str, page: int, tries: int = 4):
    """공공데이터포털은 간헐적으로 연결이 끊긴다. 173페이지에서 한 번 죽었다.
    URL 이 예외 메시지에 실려 키가 로그에 남으므로 예외를 그대로 올리지 않는다."""
    last = ""
    for i in range(tries):
        try:
            r = requests.get(url, params={"serviceKey": key, "type": "json",
                                          "numOfRows": ROWS, "pageNo": page}, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = type(e).__name__
            time.sleep(2 * (i + 1))
    raise SystemExit(f"{page}페이지에서 {tries}회 실패 ({last})")


def fetch_all(key: str, url: str):
    page, out = 1, []
    while True:
        b = _get(url, key, page).get("body") or {}
        items = b.get("items") or []
        if not items:
            break
        for it in items:
            seq, name = (it.get("ITEM_SEQ") or "").strip(), (it.get("ITEM_NAME") or "").strip()
            if seq and name:
                out.append((seq, name, (it.get("ENTP_NAME") or "").strip(),
                            (it.get("CANCEL_NAME") or "").strip()))
        total = int(b.get("totalCount") or 0)
        if page % 5 == 0 or len(out) >= total:
            print(f"  {len(out):6d} / {total}", flush=True)
        if len(out) >= total:
            break
        page += 1
        time.sleep(0.05)
    return out


def main():
    key = _key()
    rows, seen = [], set()
    for label, url in SOURCES:
        got = fetch_all(key, url)
        added = [r for r in got if r[0] not in seen]
        seen.update(r[0] for r in got)
        rows += added
        print(f"  {label}: {len(got)}건 중 신규 {len(added)}건")
    conn = sqlite3.connect(DB)
    conn.execute("DROP TABLE IF EXISTS permit")
    conn.execute("""CREATE TABLE permit (
        ITEM_SEQ TEXT PRIMARY KEY, ITEM_NAME TEXT, ENTP_NAME TEXT, CANCEL_NAME TEXT)""")
    conn.executemany("INSERT OR REPLACE INTO permit VALUES (?,?,?,?)", rows)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM permit").fetchone()[0]
    live = conn.execute("SELECT COUNT(*) FROM permit WHERE CANCEL_NAME != '취하'").fetchone()[0]
    # DUR 에 없는 것이 몇 건인가. 2단이 실제로 얼마나 덮는지.
    dur = {r[0] for r in conn.execute(
        "SELECT ITEM_SEQ FROM efcy_dplct UNION SELECT ITEM_SEQ FROM odsn_atent")}
    only = sum(1 for s, *_ in rows if s not in dur)
    conn.close()
    print(f"permit {n}건 (유효 {live}) · DUR 에 없는 것 {only}건")


if __name__ == "__main__":
    main()
