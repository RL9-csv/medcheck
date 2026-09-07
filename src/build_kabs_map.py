"""KABS(영문) ↔ DUR 성분(한글/영문) 매핑 테이블을 만든다."""
import sqlite3, re, sys
from pathlib import Path
from rapidfuzz import process, fuzz

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(__file__).resolve().parent.parent / "data" / "dur.db"

SALT = (r"\b(hydrochloride|hcl|maleate|tartrate|sulfate|sulphate|citrate|besylate|mesylate|"
        r"fumarate|succinate|phosphate|acetate|sodium|potassium|calcium|bromide|chloride|"
        r"oxalate|napadisylate|embonate|pamoate|hydrobromide|dihydrate|monohydrate|"
        r"anhydrous|iodide|butylbromide|extract|alkaloids?)\b")

def norm(s):
    s = re.sub(r"[^a-z\s;]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", re.sub(SALT, " ", s)).strip()

conn = sqlite3.connect(DB)
TABS = ["efcy_dplct", "odsn_atent", "agrde_taboo", "pwnm_taboo", "cpcty_atent", "pd_atent"]
q = " UNION ".join(f"SELECT INGR_CODE, INGR_NAME, INGR_ENG_NAME FROM {t} "
                   f"WHERE INGR_ENG_NAME IS NOT NULL" for t in TABS)

dmap = {}
for code, kor, eng in conn.execute(q):
    dmap.setdefault(norm(eng), (code, kor, eng))
for code, kor, eng in conn.execute("SELECT code, kor, eng FROM ingr_master"):
    dmap.setdefault(norm(eng), (code, kor, eng))
keys = list(dmap)
print(f"성분 마스터 {len(dmap):,}개 (정규화 키 기준)")

rows, miss = [], []
kabs = conn.execute("SELECT ingredient, kabs, category FROM kabs WHERE kabs >= 0").fetchall()
for ing, score, cat in kabs:
    hit = None
    for part in re.split(r"\s*;\s*", ing):
        n = norm(part)
        if n in dmap:
            hit = ("exact", dmap[n]); break
    if hit is None:
        for part in re.split(r"\s*;\s*", ing):
            m = process.extractOne(norm(part), keys, scorer=fuzz.token_sort_ratio, score_cutoff=88)
            if m:
                hit = ("fuzzy", dmap[m[0]]); break
    if hit:
        kind, (code, kor, eng) = hit
        rows.append((ing, score, cat, code, kor, eng, kind))
    else:
        miss.append((score, ing))

conn.execute("DROP TABLE IF EXISTS kabs_map")
conn.execute("""CREATE TABLE kabs_map
                (kabs_ingredient TEXT, kabs INTEGER, category TEXT,
                 ingr_code TEXT, ingr_kor TEXT, ingr_eng TEXT, match TEXT)""")
conn.executemany("INSERT INTO kabs_map VALUES (?,?,?,?,?,?,?)", rows)
conn.commit()

cov = len(rows) / len(kabs)
print(f"매칭 {len(rows)}/{len(kabs)} = {cov:.1%}  (정확 {sum(r[6]=='exact' for r in rows)}, "
      f"유사 {sum(r[6]=='fuzzy' for r in rows)})")
print(f"3점 매칭 {sum(1 for r in rows if r[1]==3)}/{sum(1 for _,s,_ in kabs if s==3)}")
print("\n미매칭 (DUR 데이터에 해당 품목 없음):")
for s, i in sorted(miss, reverse=True)[:12]:
    print(f"  {s}점  {i}")
