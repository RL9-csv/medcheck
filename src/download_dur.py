"""DUR 소규모 오퍼레이션 7종을 전부 내려받아 SQLite에 저장한다.
병용금기(797K건)는 제외 — 실시간 itemSeq 조회로 처리한다.
"""
import sqlite3, time, sys, requests
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
BASE = "http://apis.data.go.kr/1471000/DURPrdlstInfoService03/"
DB   = ROOT / "data" / "dur.db"

OPS = [                                    # (테이블명, 오퍼레이션, 한글명)
    ("efcy_dplct",  "getEfcyDplctInfoList03",                "효능군중복"),
    ("odsn_atent",  "getOdsnAtentInfoList03",                "노인주의"),
    ("agrde_taboo", "getSpcifyAgrdeTabooInfoList03",         "특정연령대금기"),
    ("pwnm_taboo",  "getPwnmTabooInfoList03",                "임부금기"),
    ("cpcty_atent", "getCpctyAtentInfoList03",               "용량주의"),
    ("pd_atent",    "getMdctnPdAtentInfoList03",             "투여기간주의"),
    ("sb_partitn",  "getSeobangjeongPartitnAtentInfoList03", "서방정분할주의"),
]

def load_key():
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATA_GO_KR_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(".env 에 DATA_GO_KR_KEY 없음")

def fetch_all(key, op, rows=500, pause=0.3):
    """페이지를 끝까지 돌며 전체 레코드를 모은다."""
    out, page = [], 1
    while True:
        r = requests.get(BASE + op,
                         params={"serviceKey": key, "pageNo": page,
                                 "numOfRows": rows, "type": "json"},
                         timeout=30)
        if not r.text.strip().startswith("{"):
            raise RuntimeError(f"비JSON 응답: {r.text.strip()[:150]}")
        body = r.json().get("body", {})
        items = body.get("items") or []
        total = body.get("totalCount") or 0
        out.extend(items)
        if len(out) >= total or not items:
            break
        page += 1
        time.sleep(pause)
    assert len(out) == total, f"{op}: 수집 {len(out)} != 총계 {total}"
    return out

def save(conn, table, rows):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    coldef = ", ".join('"%s" TEXT' % c for c in cols)
    ph = ", ".join("?" * len(cols))
    conn.execute("DROP TABLE IF EXISTS " + table)
    conn.execute("CREATE TABLE %s (%s)" % (table, coldef))
    conn.executemany(
        "INSERT INTO %s VALUES (%s)" % (table, ph),
        [[None if r.get(c) is None else str(r.get(c)) for c in cols] for r in rows])
    conn.commit()
    return len(rows)

if __name__ == "__main__":
    key = load_key()
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    calls = 0
    for table, op, kor in OPS:
        t0 = time.time()
        rows = fetch_all(key, op)
        n = save(conn, table, rows)
        calls += -(-n // 500)
        print(f"  {kor:<14} {n:>6,}건  {len(rows[0]) if rows else 0:>2}컬럼  {time.time()-t0:>5.1f}초")
    conn.close()
    print(f"\n저장: {DB}")
    print(f"총 API 호출: 약 {calls}회")
