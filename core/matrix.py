"""봉투 × 성분 매트릭스와 확인사항 생성.

문구 승격 규칙
  출처에 사용자가 이름을 붙이지 않았으면 '서로 다른 약봉투'라고만 말한다.
  실제 의료기관명을 확인하지 않은 상태에서 '내과와 정형외과'라고 쓰면 안 된다.
"""
from collections import defaultdict
from .model import Review, Source


def build_matrix(review: Review):
    """성분을 행, 출처를 열로 하는 매트릭스.
    반환: [(성분명, kabs, {source_index: 제품명}, 중복여부)]
    """
    rows = defaultdict(dict)
    kabs = {}
    for s in review.sources:
        for m in s.medications:
            for ing in m.ingredients:
                rows[ing.name_norm][s.index] = m.product_name
                kabs[ing.name_norm] = ing.kabs
    out = []
    for name, cells in rows.items():
        out.append((name, kabs.get(name), cells, len(cells) > 1))
    # 중복 우선, 그다음 KABS 점수 높은 순
    out.sort(key=lambda r: (not r[3], -(r[1] or 0), r[0]))
    return out


def _where(review: Review, indices: list[int]) -> str:
    """출처를 어떻게 부를지 결정한다. 이름이 없으면 승격하지 않는다."""
    srcs = [s for s in review.sources if s.index in indices]
    if not all(s.named for s in srcs):
        return "서로 다른 약봉투에서"
    names = [s.label for s in srcs]
    if len(names) == 1:
        joined = names[0]
    else:
        # 받침 유무로 '과/와' 선택
        joined = names[0]
        for nm in names[1:]:
            prev = joined[-1]
            has_batchim = (ord(prev) - 0xAC00) % 28 != 0 if 0xAC00 <= ord(prev) <= 0xD7A3 else False
            joined += ("과 " if has_batchim else "와 ") + nm
    return joined + "에서"


def find_cross_source_duplicates(review: Review):
    """서로 다른 출처에 같은 성분이 있는 경우"""
    out = []
    for name, kabs, cells, dup in build_matrix(review):
        if dup:
            out.append({
                "ingredient": name,
                "kabs": kabs,
                "products": cells,
                "phrase": f"{_where(review, list(cells))} 같은 성분이 확인되었습니다",
            })
    return out


def kabs_summary(review: Review):
    """미평가는 성분 코드 기준으로 묶는다.
    같은 성분이 여러 제품에 들어 있어도 성분은 1종이다.
    """
    total, contrib = 0, []
    unassessed = {}                      # code -> (성분명, [제품명...])
    for s in review.sources:
        for m in s.medications:
            for ing in m.ingredients:
                if not ing.assessed:
                    e = unassessed.setdefault(ing.code, (ing.name, []))
                    if m.product_name not in e[1]:
                        e[1].append(m.product_name)
                elif ing.kabs:
                    total += ing.kabs
                    contrib.append((ing.kabs, ing.name, m.product_name))
    contrib.sort(reverse=True)
    return {
        "total": total,
        "needs_review": total >= 3,
        "contributors": contrib,
        "unassessed": [(name, prods) for name, prods in unassessed.values()],
    }
