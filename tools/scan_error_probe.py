"""실제 스캔 문서에서 OCR 이 한글을 어떤 식으로 깨뜨리는지만 본다.

합성 이미지로 만든 지금 수치는 낙관 편향이 있다. 실물 스캔에서는 훨씬 나쁘다.
그런데 손에 있는 스캔 문서는 의약품 첨부문서(제약사 인쇄물)이고 약봉투가 아니다.
안내 문구의 어휘 분포가 달라서 lines.py 필터를 여기에 맞추면 안 된다.

그래서 여기서는 **어휘가 아니라 깨지는 방식**만 본다.
  - 제품명이 페이지 어딘가에 온전히 남는가
  - 안 남으면 몇 글자가 어떻게 바뀌는가 (자모 붕괴 / 유사자 치환 / 탈락)
  - 앞부분과 뒷부분 중 어디가 먼저 무너지는가

정답은 파일명에 있다. `디오탄정80밀리그램(발사르탄).pdf` 의 앞부분이 제품명이다.
라벨링 비용이 0 이고, 파일 본문은 열되 기록으로 남기지 않는다.

원문은 의뢰사 기밀이다. 추출한 텍스트와 렌더 이미지는 남기지 않고 수치와
제품명 수준의 대조 예시만 출력한다. 제품명 자체는 식약처 공개 정보다.
"""
from __future__ import annotations

import argparse
import os
import io
import random
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# fitz(PyMuPDF)를 paddle 보다 먼저 올린다. 순서를 바꾸면 네이티브 라이브러리가
# 충돌해 프로세스가 통째로 죽는다(exit 139). 원인은 안 팠고 순서로 피한다.
import fitz  # noqa: E402
from core import ocr  # noqa: E402

KOR = re.compile(r"[가-힣]")


def truth_from_name(p: Path) -> str:
    """파일명 -> 제품명. 괄호 안 성분명은 떼고 앞부분만."""
    s = unicodedata.normalize("NFC", p.stem)
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)      # 끝의 (성분명)
    return s.strip()


def render(pdf: Path, dpi: int = 120) -> bytes | None:
    try:
        doc = fitz.open(pdf)
    except Exception:
        return None
    if doc.page_count == 0:
        return None
    pix = doc[0].get_pixmap(dpi=dpi)
    data = pix.tobytes("png")
    del pix                       # 비트맵을 바로 놓아준다. 이 PC 는 가용 메모리가 1~2GB 뿐이다
    doc.close()
    return data


def best_line(lines, truth: str):
    """제품명과 가장 가까운 줄과 그 점수."""
    from rapidfuzz import fuzz
    best, score = "", 0.0
    for t in lines:
        s = fuzz.partial_ratio(truth, t) / 100.0
        if s > score:
            best, score = t, s
    return best, score


def head_tail_survival(truth: str, got: str) -> tuple[float, float]:
    """앞 절반과 뒤 절반 중 어느 쪽이 더 살아남았는가."""
    from rapidfuzz import fuzz
    h = len(truth) // 2
    return (fuzz.partial_ratio(truth[:h], got) / 100.0,
            fuzz.partial_ratio(truth[h:], got) / 100.0)


def main():
    ap = argparse.ArgumentParser()
    # 기본값을 두지 않는다. 이 스크립트가 읽는 PDF 는 기밀 문서이고
    # 이 저장소는 공개다. 경로를 박아두면 디렉터리 구조가 같이 공개된다.
    # 환경변수 SCAN_PDF_ROOT 또는 --root 로 실행할 때 지정한다.
    ap.add_argument("--root", default=os.environ.get("SCAN_PDF_ROOT"))
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--dpi", type=int, default=120)
    a = ap.parse_args()

    if not a.root:
        print("PDF 폴더를 지정하십시오: --root 또는 SCAN_PDF_ROOT")
        return 2
    pdfs = list(Path(a.root).rglob("*.pdf"))
    if not pdfs:
        print("PDF 없음:", a.root)
        return 1
    rng = random.Random(a.seed)
    sample = rng.sample(pdfs, min(a.n, len(pdfs)))
    print(f"전체 {len(pdfs):,}건 중 {len(sample)}건 표본 · dpi {a.dpi}", flush=True)

    engine = ocr.get_engine()
    ocr.warmup(engine)

    exact = near = miss = failed = 0
    heads, tails, rows = [], [], []
    for i, p in enumerate(sample, 1):
        truth = truth_from_name(p)
        img = render(p, a.dpi)
        if img is None:
            failed += 1
            continue
        try:
            lines = [l.text for l in engine.read(img)]
        except Exception as e:
            failed += 1
            print(f"  ! {type(e).__name__}", flush=True)
            continue
        got, score = best_line(lines, truth)
        h, t = head_tail_survival(truth, got)
        heads.append(h)
        tails.append(t)
        if score >= 0.99:
            exact += 1
        elif score >= 0.70:
            near += 1
            rows.append((truth, got, score))
        else:
            miss += 1
            rows.append((truth, got, score))
        del img
        if i % 10 == 0:
            import gc
            gc.collect()
            print(f"  {i}/{len(sample)}", flush=True)

    n = exact + near + miss
    out = [
        "",
        f"표본 {n}건 (렌더·인식 실패 {failed}건 제외)",
        f"  제품명 원문 그대로 검출   {exact:3d}건 ({100*exact/max(n,1):.0f}%)",
        f"  일부 깨졌지만 근접        {near:3d}건 ({100*near/max(n,1):.0f}%)",
        f"  검출 실패                {miss:3d}건 ({100*miss/max(n,1):.0f}%)",
        "",
        f"제품명 앞 절반 생존률 평균  {sum(heads)/max(len(heads),1):.2f}",
        f"제품명 뒤 절반 생존률 평균  {sum(tails)/max(len(tails),1):.2f}",
        "",
        "깨진 사례 (제품명은 공개 정보. 문서 본문은 기록하지 않음)",
    ]
    for truth, got, score in rows[:15]:
        out.append(f"  {score:.2f}  정답 {truth!r}")
        out.append(f"        인식 {got!r}")
    io.open(ROOT / "reports" / "scan_error_probe.txt", "w", encoding="utf-8").write("\n".join(out))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
