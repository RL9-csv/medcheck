"""약봉투가 망가질수록 파이프라인이 어떻게 무너지는지 측정한다.

실제 약봉투는 깨끗하지 않다. 주머니에서 구겨지고, 집 안 조명은 어둡고,
사진은 비뚤어진다. 깨끗한 합성 이미지 한 장으로 "됩니다"라고 말하면
심사에서 그대로 무너진다.

그래서 정답을 아는 이미지를 만들고, 그것을 단계적으로 망가뜨린다.
정답을 아는 이유는 우리가 그려 넣었기 때문이다. 라벨링 비용이 0이다.

측정하는 것은 넷이다.
  recall        정답 약을 자동 확정한 비율
  false_accept  자동 확정했는데 틀린 비율   <- 가장 위험한 수치
  suggest       사람에게 물어본 비율        <- 안전한 실패
  noise         약이 아닌 줄이 후보를 달고 올라온 수

false_accept 는 0 이어야 한다. 못 읽는 것은 사람이 고치면 되지만,
틀린 약을 확신하고 넣으면 그 아래 판정이 전부 오염된다.

사용:
    PADDLE_MODEL_DIR=C:/paddleocr python tools/bench_ocr.py --n 3
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import lines as L  # noqa: E402
from core import match, ocr  # noqa: E402

FONT = "C:/Windows/Fonts/malgun.ttf"
FONT_BD = "C:/Windows/Fonts/malgunbd.ttf"

# 봉투 양식은 약국마다 다르다. 한 양식에 맞춰 튜닝되지 않도록 셋을 돌린다.
LAYOUTS = ("basic", "boxed", "twocol")


def pick_drugs(rng, k):
    """실제 품목 사전에서 뽑는다. 이름 길이·괄호·숫자가 골고루 섞이도록."""
    names = [r[1] for r in match._catalog()]
    return rng.sample(names, k)


def make_envelope(drugs, layout, rng, size=(900, 1200)):
    """정답을 아는 약봉투 한 장."""
    img = Image.new("RGB", size, (252, 252, 250))
    d = ImageDraw.Draw(img)
    f_big = ImageFont.truetype(FONT_BD, 44)
    f = ImageFont.truetype(FONT, 34)
    f_sm = ImageFont.truetype(FONT, 28)

    y = 60
    d.text((60, y), "행복내과의원", font=f_big, fill=(20, 20, 20)); y += 80
    d.text((60, y), "조제일자 2026-09-07", font=f_sm, fill=(60, 60, 60)); y += 70
    if layout == "boxed":
        d.rectangle([40, y - 20, size[0] - 40, y + 90 * len(drugs) + 10], outline=(120, 120, 120), width=3)

    for name in drugs:
        dose = rng.choice(["1일 3회 식후 30분", "1일 2회 식후", "1일 1회 취침 전", "1일 3회 식전"])
        if layout == "twocol":
            d.text((70, y), name, font=f, fill=(0, 0, 0))
            d.text((size[0] - 360, y + 6), dose, font=f_sm, fill=(70, 70, 70))
        else:
            d.text((70, y), name, font=f, fill=(0, 0, 0)); y += 46
            d.text((90, y), dose, font=f_sm, fill=(70, 70, 70))
        y += 90

    y += 40
    d.text((60, y), f"약사 김００   총 {len(drugs)}종", font=f_sm, fill=(60, 60, 60))
    return img


# --- 열화 ---------------------------------------------------------------
# level 0 은 원본이다. 곡선의 시작점이 되어야 비교가 된다.

def deg_none(img, lv):
    return img


def deg_blur(img, lv):
    return img.filter(ImageFilter.GaussianBlur(radius=lv * 1.2))


def deg_dark(img, lv):
    a = np.asarray(img).astype(np.float32)
    a = a * (1 - 0.22 * lv) + 8 * lv          # 어두워지고 대비가 죽는다
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def deg_rotate(img, lv):
    return img.rotate(lv * 4, expand=True, fillcolor=(252, 252, 250), resample=Image.BICUBIC)


def deg_wrinkle(img, lv):
    """구김. 세로로 물결치게 밀고 접힌 선에 그늘을 넣는다."""
    a = np.asarray(img).astype(np.float32)
    h, w = a.shape[:2]
    amp = lv * 6
    out = np.empty_like(a)
    for y in range(h):
        shift = int(amp * math.sin(2 * math.pi * y / (h / 3.0)))
        out[y] = np.roll(a[y], shift, axis=0)
    for i in range(1, lv + 1):                 # 접힌 자국
        x = int(w * i / (lv + 1))
        out[:, max(0, x - 6):x + 6] *= 0.82
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def deg_jpeg(img, lv):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(5, 45 - 12 * lv))
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


DEGS = {"none": deg_none, "blur": deg_blur, "dark": deg_dark,
        "rotate": deg_rotate, "wrinkle": deg_wrinkle, "jpeg": deg_jpeg}


def to_png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def score(read_lines, truth, use_filter):
    """읽은 줄 -> (recall, false_accept, suggest, noise_suggest)."""
    queries, _ = L.filter_lines(read_lines) if use_filter else (list(read_lines), 0)
    hit, wrong, sug, noise_sug = set(), 0, 0, 0
    for q in queries:
        med, cands, st = match.resolve(q)
        if st == "auto":
            if med.product_name in truth:
                hit.add(med.product_name)
            else:
                wrong += 1
        elif st == "suggest":
            sug += 1
            # 정답 후보가 하나도 없는 제안 = 화면을 덮는 잡음
            if not any(c.product_name in truth for c in cands):
                noise_sug += 1
    return len(hit) / len(truth), wrong, sug, noise_sug


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="양식별 봉투 장수")
    ap.add_argument("--drugs", type=int, default=4, help="봉투당 약 개수")
    ap.add_argument("--levels", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "reports" / "ocr_bench.csv"))
    a = ap.parse_args()

    rng = random.Random(a.seed)
    engine = ocr.get_engine()
    ocr.warmup(engine)

    sheets = []
    for layout in LAYOUTS:
        for _ in range(a.n):
            drugs = pick_drugs(rng, a.drugs)
            sheets.append((layout, drugs, make_envelope(drugs, layout, rng)))

    rows = []
    for name, fn in DEGS.items():
        for lv in range(0, a.levels + 1):
            if name == "none" and lv > 0:
                continue
            acc = {k: [] for k in ("recall_f", "recall_r", "wrong_f", "wrong_r",
                                   "sug_f", "sug_r", "noise_f", "noise_r", "nlines")}
            for layout, drugs, img in sheets:
                read = [l.text for l in engine.read(to_png(fn(img, lv)))]
                truth = set(drugs)
                r, w, s, ns = score(read, truth, True)
                acc["recall_f"].append(r); acc["wrong_f"].append(w)
                acc["sug_f"].append(s); acc["noise_f"].append(ns)
                r, w, s, ns = score(read, truth, False)
                acc["recall_r"].append(r); acc["wrong_r"].append(w)
                acc["sug_r"].append(s); acc["noise_r"].append(ns)
                acc["nlines"].append(len(read))
            row = {"deg": name, "level": lv, "sheets": len(sheets)}
            row.update({k: round(float(np.mean(v)), 3) for k, v in acc.items()})
            rows.append(row)
            print(f"{name:8s} lv{lv}  recall {row['recall_f']:.2f}(filter) "
                  f"{row['recall_r']:.2f}(raw)  false_accept {row['wrong_f']:.2f}/"
                  f"{row['wrong_r']:.2f}  noise_suggest {row['noise_f']:.2f}/{row['noise_r']:.2f}",
                  flush=True)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        wcsv = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wcsv.writeheader()
        wcsv.writerows(rows)
    print("wrote", out)


if __name__ == "__main__":
    main()
