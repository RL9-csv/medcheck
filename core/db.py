"""SQLite 접근. 읽기 전용."""
import sqlite3, re
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "dur.db"
TABS = ["efcy_dplct", "odsn_atent", "agrde_taboo", "pwnm_taboo",
        "cpcty_atent", "pd_atent", "sb_partitn"]

KOR_SALT = re.compile(
    r"(염산염|염산|말레산염|말레인산|주석산염|타르타르산염|황산염|인산염|구연산염|시트르산염|"
    r"푸마르산염|숙신산염|메실산염|메탄술폰산염|베실산염|초산염|아세트산염|브롬화물|브롬화수소산염|"
    r"요오드화물|나트륨|칼륨|칼슘|마그네슘|수화물|무수물|일수화물|이수화물|총알칼로이드|엑스|"
    r"메틸황산염|팔미트산염|스테아르산염|옥살산염)")


def norm_kor(s: str) -> str:
    return KOR_SALT.sub("", re.sub(r"^[dl]-", "", (s or "").strip())).strip()


def connect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
