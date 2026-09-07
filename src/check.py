"""약 이름 목록 -> 성분 -> KABS 누적 부담 + 효능군 중복.

설계 원칙
  - KABS 미수록은 0점이 아니라 '미평가'다. 0점(항콜린 작용 없음)과 구분한다.
  - 합계는 하한값이다. 미평가 존재를 명시한다.
  - 개별 위험도(HR/OR)를 합계에 연결해 표시하지 않는다.
    인용 연구는 수년 누적 노출 기반이라 현재 시점 합산과 다른 개념이다.
"""
import sqlite3, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(__file__).resolve().parent.parent / "data" / "dur.db"
TABS = ["efcy_dplct", "odsn_atent", "agrde_taboo", "pwnm_taboo",
        "cpcty_atent", "pd_atent", "sb_partitn"]
ING = re.compile(r"\[([A-Z]\d+)\]([^/\[\]]+)")
KOR_SALT = re.compile(
    r"(염산염|염산|말레산염|말레인산|주석산염|타르타르산염|황산염|인산염|구연산염|시트르산염|"
    r"푸마르산염|숙신산염|메실산염|메탄술폰산염|베실산염|초산염|아세트산염|브롬화물|브롬화수소산염|"
    r"요오드화물|나트륨|칼륨|칼슘|마그네슘|수화물|무수물|일수화물|이수화물|총알칼로이드|엑스|"
    r"메틸황산염|팔미트산염|스테아르산염|옥살산염)")

def norm_kor(s):
    return KOR_SALT.sub("", re.sub(r"^[dl]-", "", (s or "").strip())).strip()

def find_items(conn, q, limit=5):
    union = " UNION ".join(
        f"SELECT ITEM_SEQ, ITEM_NAME, MAIN_INGR, ETC_OTC_NAME FROM {t} WHERE ITEM_NAME LIKE ?"
        for t in TABS)
    return conn.execute(f"SELECT * FROM ({union}) LIMIT ?",
                        [f"%{q}%"] * len(TABS) + [limit]).fetchall()

def kabs_of(conn, main_ingr):
    """(성분명, 상태, 점수). 상태: scored | unassessed"""
    out = []
    for code, raw in ING.findall(main_ingr or ""):
        name = raw.strip()
        r = conn.execute("SELECT kabs FROM kabs_map WHERE ingr_code=? LIMIT 1", (code,)).fetchone()
        if r is None:
            r = conn.execute("SELECT kabs FROM kabs_map WHERE ingr_kor_norm=? LIMIT 1",
                             (norm_kor(name),)).fetchone()
        out.append((name, "unassessed", None) if r is None else (name, "scored", r[0]))
    return out

def check(names):
    conn = sqlite3.connect(DB)
    total, contrib, unassessed, groups, picked = 0, [], [], {}, []

    for n in names:
        cands = find_items(conn, n)
        if not cands:
            picked.append((n, None)); continue
        seq, iname, mi, otc = cands[0]
        ings = kabs_of(conn, mi)
        s = sum(k for _, st, k in ings if st == "scored")
        total += s
        picked.append((n, (iname, otc, s, ings)))
        for ing, st, k in ings:
            if st == "unassessed":
                unassessed.append((ing, iname))
            elif k:
                contrib.append((k, ing, iname))
        for (g,) in conn.execute(
                "SELECT DISTINCT EFFECT_NAME FROM efcy_dplct WHERE ITEM_SEQ=?", (seq,)):
            if g: groups.setdefault(g, []).append(iname)

    print("=== 확인된 약 ===")
    for n, r in picked:
        if r is None:
            print(f"  {n:<12} → 등재 목록에 없음 (검사 제외)")
        else:
            iname, otc, s, ings = r
            un = sum(1 for _, st, _ in ings if st == "unassessed")
            tag = f"   미수록 성분 {un}개" if un else ""
            print(f"  {n:<12} → {iname[:30]:<32} [{otc}]  KABS {s}점{tag}")

    print()
    print(f"=== 항콜린 부담 합계: {total}점 ===")
    print("  KABS(한국형 항콜린 부담 척도) 기준. 3점 이상은 복용약 검토를 권장하는 구간입니다.")
    print("  개인의 질병 위험을 예측하는 값이 아니라, 검토 필요 여부의 참고 지표입니다.")

    if contrib:
        print()
        print("  점수 기여 성분")
        for k, ing, iname in sorted(contrib, reverse=True):
            print(f"    {k}점  {ing:<22} ({iname[:20]})")

    if unassessed:
        uniq = sorted(set(unassessed))
        print()
        print(f"  KABS 미수록 성분 {len(uniq)}종")
        print("    KABS는 항콜린 작용이 보고된 약물을 중심으로 구성된 494개 목록입니다.")
        print("    미수록 성분은 대부분 항콜린 작용이 알려지지 않은 약이지만,")
        print("    평가되지 않았을 가능성도 있어 합계는 하한값으로 보아야 합니다.")
        for ing, iname in uniq[:5]:
            print(f"      - {ing:<20} ({iname[:20]})")
        if len(uniq) > 5:
            print(f"      외 {len(uniq)-5}종")

    dup = {g: v for g, v in groups.items() if len(v) > 1}
    print()
    print(f"=== 효능군 중복: {len(dup)}건 ===")
    for g, items in dup.items():
        print(f"  [{g}]")
        for i in items: print(f"    - {i}")
    if not dup: print("  없음")

    conn.close()
    return {"total": total, "unassessed": len(set(unassessed)), "duplicates": dup}

if __name__ == "__main__":
    check(["에어탈", "아펜탈", "페니라민"])
