"""데이터 모델.

핵심 추상화는 Source(출처)다. UI에서는 '봉투'로 부르지만
처방전·약국 포장지·병원 발행 목록이 들어와도 구조를 바꾸지 않는다.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Ingredient:
    code: str                      # M코드 또는 D코드
    name: str                      # 원문 성분명 (염 표기 포함)
    name_norm: str                 # 염 표기 제거
    kabs: Optional[int] = None     # None = KABS 미수록(미평가). 0과 구분한다.

    @property
    def assessed(self) -> bool:
        return self.kabs is not None


@dataclass
class Medication:
    item_seq: str
    product_name: str              # 에어탈정(아세클로페낙)
    otc: str                       # 전문의약품 / 일반의약품
    ingredients: list[Ingredient] = field(default_factory=list)
    confidence: float = 1.0        # 사전 매칭 신뢰도
    confirmed: bool = False        # 사용자가 확인했는가

    @property
    def kabs_score(self) -> int:
        return sum(i.kabs for i in self.ingredients if i.assessed)


@dataclass
class Source:
    """약 하나의 출처. UI에서는 '봉투 1' 등으로 표시된다."""
    index: int                             # 1부터
    label: Optional[str] = None            # 사용자가 붙인 이름 ("내과")
    source_type: Optional[str] = None      # 약봉투 / 처방전 / 약국포장
    medications: list[Medication] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.label or f"봉투 {self.index}"

    @property
    def named(self) -> bool:
        """사용자가 실제 이름을 붙였는가. 문구 승격 판단에 쓴다."""
        return bool(self.label)


@dataclass
class Review:
    sources: list[Source] = field(default_factory=list)

    @property
    def all_meds(self) -> list[Medication]:
        return [m for s in self.sources for m in s.medications]

    @property
    def all_named(self) -> bool:
        """모든 출처에 이름이 붙었는가"""
        return bool(self.sources) and all(s.named for s in self.sources)
