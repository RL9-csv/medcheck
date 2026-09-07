"""병용금기 API를 무작위 페이지로 샘플링해 성분 마스터를 넓힌다.
순차 페이지는 같은 DUR 규칙이 반복돼 성분 다양성이 거의 없다.
"""
import sqlite3, random, sys, time, requests
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
URL = "http://apis.data.go.kr/1471000/DURPrdlstInfoService03/getUsjntTabooInfoList03"
TOTAL_PAGES = 1595            # 797,186 / 500

key = [l.split("=", 1)[1].strip() for l in (ROOT/".env").read_text(encoding="utf-8").splitlines()
       if l.startswith("DATA_GO_KR_KEY=")][0]

conn = sqlite3.connect(ROOT/"data"/"dur.db")
conn.execute("CREATE TABLE IF NOT EXISTS ingr_master (code TEXT PRIMARY KEY, kor TEXT, eng TEXT)")
seen = {c: (k, e) for c, k, e in conn.execute("SELECT code,kor,eng FROM ingr_master")}
print(f"기존 성분 {len(seen)}개에서 시작")

rng = random.Random(0)
pages = rng.sample(range(1, TOTAL_PAGES + 1), 300)
for i, page in enumerate(pages, 1):
    try:
        r = requests.get(URL, params={"serviceKey": key, "pageNo": page,
                                      "numOfRows": 500, "type": "json"}, timeout=30)
        if not r.text.strip().startswith("{"):
            print(f"  중단: {r.text.strip()[:100]}"); break
        for it in (r.json().get("body", {}).get("items") or []):
            for c, k, e in ((it.get("INGR_CODE"), it.get("INGR_KOR_NAME"), it.get("INGR_ENG_NAME")),
                            (it.get("MIXTURE_INGR_CODE"), it.get("MIXTURE_INGR_KOR_NAME"),
                             it.get("MIXTURE_INGR_ENG_NAME"))):
                if c and e:
                    seen.setdefault(c, (k, e))
    except Exception as ex:
        print(f"  page {page} 실패: {str(ex)[:60]}")
    if i % 50 == 0:
        print(f"  {i}/300 페이지  누적 성분 {len(seen):,}개")
    time.sleep(0.1)

conn.executemany("INSERT OR IGNORE INTO ingr_master VALUES (?,?,?)",
                 [(c, k, e) for c, (k, e) in seen.items()])
conn.commit()
print(f"\n최종 성분 마스터: {len(seen):,}개")
