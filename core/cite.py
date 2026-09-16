"""근거 출처. Finding 종류마다 다른 논문·데이터를 인용한다.
한 논문이 모든 것을 증명하는 것처럼 묶지 않는다.
"""

KABS_SCALE = (
    "KABS 한국형 항콜린 부담 척도 · "
    "개발: Jun K et al., Geriatr Gerontol Int. 2019;19(7):628-634 · "
    "데이터 출처: Suh Y et al., BMC Geriatrics. 2020;20:265, "
    "Additional File 1 (CC BY 4.0)")

KABS_THRESHOLD = (
    "KABS 3점 이상을 고부담으로 보는 기준 · "
    "Hwang S et al., BMC Pharmacol Toxicol. 2021;22:4 "
    "(한국 고령자 461,034명 청구자료 분석)")

KABS_SYMPTOM = (
    "항콜린 부담과 어지럼·변비·요저류·낙상·섬망 관련 의료이용 사이의 "
    "연관성 보고 · Jun K et al., Arch Gerontol Geriatr. 2019 "
    "(전국 코호트 118,750명). 인과관계를 의미하지 않습니다.")

SAME_INGREDIENT = (
    "식품의약품안전처 의약품 허가 성분 정보(주성분) 기준으로 "
    "동일 성분 여부를 비교했습니다.")

EFFECT_GROUP = (
    "식품의약품안전처 의약품안전사용서비스(DUR) 효능군중복주의 정보")

ELDERLY = (
    "식품의약품안전처 DUR 노인주의 (약사법 제23조의2 등에 따른 공고). "
    "원자료에 상세 사유가 제공되지 않는 항목이 있습니다.")

UNASSESSED = (
    "KABS는 항콜린 작용이 보고된 약물을 중심으로 구성된 493개 목록입니다. "
    "목록에 없는 성분은 평가되지 않은 것이며, 합계는 하한값입니다.")

# 493은 원 척도 첨부파일에 실제로 나열된 품목 수다. 논문 본문은 494라고 한다.
#
# 원본을 직접 받아 대조했다. 공개된 첨부파일과 data/kabs_raw.docx 가
# 바이트 단위로 동일하다(30,057B, md5 5ccb45040abc223ac383803206145cb6).
# 우리 사본이 곧 원본이고 변환에서 잃은 것은 없다.
#
# 원본 자체가 어긋난다. 분류 21개를 전수 확인했다.
#
#   분류별 N 표기 합계    494
#   쉼표 기준 나열 합계    493
#
#   Antidepressants        N=20  나열 19
#   Antipsychotics         N=25  나열 24
#   Mineral and vitamins   N=15  나열 16
#
# 나머지 18개 분류는 N 과 나열이 일치한다. Mineral and vitamins 가 1 많은
# 이유는 Tocopherol 과 Vitamin E 가 쉼표로 나란히 적혀 있기 때문이다.
# 같은 물질이고, 이 표는 다른 곳에서 동의어를 세미콜론으로 묶는다
# (Levomepromazine; Methotrimeprazine 등 9건). 저자가 한 항목으로 세어
# N=15 로 적고 나열만 풀어 쓴 것으로 보인다.
#
# 우리가 적재한 493개는 원본 나열과 분류 단위까지 전부 일치한다. 누락은
# 없다. Antidepressants 와 Antipsychotics 에서 1건씩 모자란 것은 원본에
# 이름이 아예 없는 것이라 복구 대상이 아니다.
#
# 화면에는 대조 가능한 수인 493을 쓴다.
