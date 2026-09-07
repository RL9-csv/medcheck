"""Review -> 확인사항(Finding).

모든 Finding은 근거와 출처를 들고 다닌다. 판정은 여기서 결정론적으로 끝나고,
LLM은 문장 표현만 담당한다.

동일성분 중복과 효능군 중복은 근거가 다르므로 분리한다.
  동일성분   허가 성분정보(MAIN_INGR) 기준. 우리가 직접 비교.
  효능군     식약처 DUR 효능군중복주의 데이터.
"""
from dataclasses import dataclass, field
from collections import defaultdict
from . import cite
from .db import connect
from .model import Review
from .matrix import find_cross_source_duplicates, kabs_summary

# 증상 항목은 KABS 검증 연구가 실제로 측정한 결과로 한정한다.
SYMPTOMS = [
    ("dizziness", "어지러움"),
    ("constipation", "변비"),
    ("urinary", "소변 보기 불편"),
    ("fall", "넘어지거나 휘청거림"),
    ("cognition", "기억력·혼란"),
]
SYMPTOM_LABEL = dict(SYMPTOMS)


@dataclass
class Finding:
    kind: str
    title: str
    evidence: list = field(default_factory=list)
    source_note: str = ""
    priority: int = 0
    subject: str = ""       # 질문에서 대상을 이름으로 부르기 위한 값


def same_ingredient_findings(review: Review):
    out = []
    for d in find_cross_source_duplicates(review):
        out.append(Finding(
            kind="same_ingredient",
            title=f"{d['phrase']} — {d['ingredient']}",
            evidence=[(f"봉투 {i}", p) for i, p in sorted(d["products"].items())],
            source_note=cite.SAME_INGREDIENT,
            priority=100,
            subject=d["ingredient"]))
    return out


def effect_group_findings(review: Review, covered=frozenset()):
    """DUR 효능군중복. 성분이 달라도 같은 효능군이면 잡힌다.

    covered: 이미 동일성분으로 보고한 제품명 집합. 그 안에서만 이뤄진
    효능군 중복은 같은 사실을 두 번 말하는 것이므로 보고하지 않는다.
    """
    conn = connect()
    groups = defaultdict(set)
    for s in review.sources:
        for m in s.medications:
            for (g,) in conn.execute(
                    "SELECT DISTINCT EFFECT_NAME FROM efcy_dplct WHERE ITEM_SEQ=?",
                    (m.item_seq,)):
                if g:
                    groups[g].add(m.product_name)
    conn.close()

    out = []
    for g, prods in groups.items():
        if len(prods) < 2:
            continue
        names = sorted(prods)
        if prods <= covered:
            continue
        out.append(Finding(
            kind="effect_group",
            title=f"같은 효능군의 약이 {len(names)}개 있습니다 — {g}",
            evidence=[("품목", n) for n in names],
            source_note=cite.EFFECT_GROUP,
            priority=95,
            subject=g))
    return out


def elderly_findings(review: Review):
    conn = connect()
    rows = []
    for m in review.all_meds:
        r = conn.execute(
            "SELECT PROHBT_CONTENT FROM odsn_atent WHERE ITEM_SEQ=? LIMIT 1",
            (m.item_seq,)).fetchone()
        if r:
            rows.append((m.product_name, (r["PROHBT_CONTENT"] or "").strip()))
    conn.close()
    if not rows:
        return []
    return [Finding(
        kind="elderly",
        title=f"식약처 노인주의 정보가 등록된 약이 {len(rows)}개 있습니다",
        evidence=rows,
        source_note=cite.ELDERLY,
        priority=80)]


def kabs_findings(review: Review, symptoms):
    k = kabs_summary(review)
    out = []
    if k["needs_review"]:
        out.append(Finding(
            kind="kabs",
            title=f"항콜린 부담 합계가 {k['total']}점입니다 (검토 권장 3점 이상)",
            evidence=[(f"{s}점", f"{ing} — {prod}") for s, ing, prod in k["contributors"]],
            source_note=cite.KABS_SCALE + " · " + cite.KABS_THRESHOLD,
            priority=110 if symptoms else 90))
    if k["unassessed"]:
        out.append(Finding(
            kind="unassessed",
            title=f"KABS 척도에 없는 성분이 {len(k['unassessed'])}종 있습니다",
            evidence=[(name, ", ".join(prods)) for name, prods in k["unassessed"][:8]],
            source_note=cite.UNASSESSED,
            priority=10))
    return out


def analyze(review: Review, symptoms=None) -> list[Finding]:
    symptoms = symptoms or []
    same = same_ingredient_findings(review)
    covered = {p for f in same for _, p in f.evidence}
    findings = (same
                + effect_group_findings(review, covered)
                + elderly_findings(review)
                + kabs_findings(review, symptoms))
    findings.sort(key=lambda f: -f.priority)
    return findings


def make_questions(findings, symptoms) -> list[str]:
    """근거가 있는 Finding에서만 질문을 만든다.
    '원인이다 / 위험하다 / 중단하라' 표현은 쓰지 않는다.
    """
    sym = ", ".join(SYMPTOM_LABEL.get(s, s) for s in symptoms)
    qs = []
    for f in findings:
        if f.kind == "same_ingredient":
            qs.append(f"서로 다른 곳에서 받은 약에 {f.subject} 성분이 함께 있습니다. "
                      f"두 가지를 모두 복용해야 하는지 확인 부탁드립니다.")
        elif f.kind == "effect_group":
            qs.append(f"{f.subject}에 해당하는 약이 함께 있습니다. "
                      f"둘 다 필요한지 확인 부탁드립니다.")
        elif f.kind == "kabs":
            if symptoms:
                qs.append(f"최근 {sym} 증상이 있습니다. 한국 고령자 연구에서 높은 항콜린 "
                          f"부담과 이런 증상 관련 의료이용 사이의 연관성이 보고된 바 있습니다. "
                          f"현재 복용약과 관련이 있는지 확인 부탁드립니다.")
            else:
                qs.append("항콜린 부담 척도상 검토 권장 구간에 해당합니다. "
                          "조정 가능한 약이 있는지 확인 부탁드립니다.")
        elif f.kind == "elderly":
            qs.append("식약처 노인주의 정보가 등록된 약이 포함되어 있습니다. "
                      "현재 상태에서 계속 복용해도 되는지 확인 부탁드립니다.")
    return qs[:3]
