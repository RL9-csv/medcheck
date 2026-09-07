# medcheck

여러 병원에서 받은 약을 한 장으로 정리합니다.

## 설정
1. `.env` 의 DATA_GO_KR_KEY 에 공공데이터포털 Decoding 키 입력
2. `pip install -r requirements.txt`

## 데이터 출처와 라이선스

### 식품의약품안전처 의약품안전사용서비스(DUR) 품목정보
공공데이터포털 OpenAPI로 수집했습니다. `data/dur.db` 에 적재되어 있습니다.

| 테이블 | 내용 | 건수 |
|---|---|---|
| pwnm_taboo | 임부금기 | 16,078 |
| efcy_dplct | 효능군중복주의 | 7,064 |
| cpcty_atent | 용량주의 | 6,621 |
| agrde_taboo | 연령금기 | 2,682 |
| sb_partitn | 분할주의 | 2,077 |
| odsn_atent | 노인주의 | 1,983 |
| pd_atent | 투여기간주의 | 644 |

병용금기(797,186건)는 적재하지 않고 확정된 품목코드로 실시간 조회합니다.

### KABS (Korean Anticholinergic Burden Scale)
494개 성분의 항콜린 부담 점수(0-3)입니다. `data/kabs_raw.docx` 는 아래 논문의
Additional File 1 원본이며 **CC BY 4.0** 으로 배포된 것을 그대로 포함합니다.

- **척도 개발**: Jun K, Hwang S, Ah YM, Suh Y, Lee JY.
  Development of an anticholinergic burden scale specific for Korean older adults.
  *Geriatr Gerontol Int.* 2019;19(7):628-634.
- **목록 출처(재배포 근거)**: Suh Y, Ah YM, Han E, Jun K, Hwang S, Choi KH, Lee JY.
  Development of a comprehensive anticholinergic burden scale for older adults in Korea.
  *BMC Geriatrics.* 2020;20:265. Additional File 1.
  Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). 변경 없이 포함.
- **3점 기준**: Hwang S, et al. *BMC Pharmacol Toxicol.* 2021;22:4.
- **증상 연관성**: Jun K, et al. *Arch Gerontol Geriatr.* 2019.

KABS 는 항콜린 작용이 보고된 약물 중심의 목록입니다. **목록에 없는 성분은
"0점"이 아니라 "미평가"** 이며, 화면에서도 이 둘을 구분해 표시합니다.
따라서 합계 점수는 하한값입니다.

## 한계

- 항콜린 부담 점수는 개인의 질병 위험을 예측하는 값이 아니라 검토 필요 여부의
  참고 지표입니다. 인용한 코호트 연구는 다년간 DDD 표준화 누적 노출을 사용했으며,
  본 서비스의 시점 합계와 같은 값이 아닙니다.
- DUR 노인주의는 원자료에 상세 사유가 제공되지 않는 항목이 많습니다.
- KABS 성분과 DUR 품목의 매핑률은 약 75% 입니다.

## 면책

본 서비스는 교육·정보 제공 목적이며 진단이나 치료를 제공하지 않습니다.
개별 환자에 맞춤화하지 않으며 어떤 치료도 권고하지 않습니다.
임의로 약을 중단하거나 변경하지 마시고 의사·약사와 상담하세요.
