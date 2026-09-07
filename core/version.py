"""지식 자산 버전. 응답에 함께 실어 어떤 데이터로 나온 결과인지 추적한다."""
import sqlite3, hashlib
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "dur.db"


def knowledge_version() -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    counts = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    sig = hashlib.md5(str(sorted(counts.items())).encode()).hexdigest()[:8]
    return {
        "catalog_version": sig,
        "db_bytes": DB.stat().st_size,
        "tables": counts,
        "scales": {
            "kabs": "Jun 2019 / Suh 2020 BMC Geriatr Additional File 1 (CC BY 4.0)",
            "dur": "식약처 의약품안전사용서비스 오픈API",
        },
    }
