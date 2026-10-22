# 빠른 예측 · 상용성

계산을 실행하기 전에 후보를 좁히는 화면입니다. Materials Studio Synthia(반복단위 QSPR)·Blends(χ)와 HSPiP(Hansen 용해 구)가 맡던 1차 선별을 공개 데이터와 공개 코드로 대신합니다. 결과는 **참조 데이터 기반 예측**이며 연구 DB의 측정값으로 저장되지 않습니다. HSP 구 피팅 결과만 측정값 양식에 초안으로 넘길 수 있고, 저장 후 사람이 검토해야 학습에 쓰입니다.

## 계산 전 물성 예측 (RadonPy PI1070)

| 항목 | 내용 |
|---|---|
| 학습 데이터 | RadonPy `data/PI1070.csv` (`RadonPy/RadonPy@8834b907`, SHA-256 `4ee41d52…`, BSD-3-Clause). 비정질 고분자 1,077종의 전원자 MD 결과, 300 K · 1 atm. Hayashi et al., npj Comput. Mater. 8, 222 (2022) |
| 예측 물성 | 밀도(g/cm³), Cp(J/(kg K)), 체적 탄성률(GPa), 등온 압축률(1/GPa), 체적 열팽창 계수(1/K), 굴절률, 열전도도(W/(m K)) |
| 표현 | 반복단위를 머리–꼬리로 3개 이은 올리고머(양 끝 H)의 Morgan count 지문(반지름 2, 2048) |
| 모델 | 가우스 과정. MinMax(count Tanimoto) 커널. 진폭·잡음은 정확한 로그 주변우도로 격자 선택 |
| 출력 | 예측값 ± 1σ(새 MD 결과에 대한 예측 분포), 95% 구간, 최근접 PI1070 고분자 3개와 그 값, 적용 범위 |

물성 키는 벌크 MD 경로의 피처 이름(`bulk_density`, `heat_capacity_cp` …)과 같습니다. 그래서 이 작업공간에서 같은 반복단위로 완료한 RadonPy 벌크 계산이 있으면 그 값과 예측값의 차이, z 점수를 함께 보여 줍니다. 예측 → 벌크 MD로 확인하는 다중 충실도 흐름의 첫 단계입니다. **벌크 MD 캠페인 후보로 보내기**는 예측한 반복단위로 `atomistic_mode: bulk` 후보(사슬 4개 × 20 반복단위, 단계 길이 ×0.01 탐색용)를 캠페인 양식에 채웁니다.

### 교차검증 (무작위 5겹, seed 0)

| 물성 | MAE | R² | 1σ / 2σ 포함률 | MAE: 유사도 ≥ 0.55 / 0.35–0.55 / < 0.35 |
|---|---|---|---|---|
| 밀도 | 0.0235 g/cm³ | 0.925 | 92% / 97% | 0.015 / 0.043 / 0.076 |
| Cp | 99 J/(kg K) | 0.943 | 88% / 98% | 73 / 142 / 334 |
| 체적 탄성률 | 0.30 GPa | 0.723 | 81% / 97% | 0.25 / 0.43 / 0.69 |
| 등온 압축률 | 0.043 1/GPa | 0.586 | 78% / 96% | 0.037 / 0.058 / 0.077 |
| 체적 열팽창 | 6.0e-5 1/K | 0.632 | 72% / 92% | 5.4e-5 / 6.8e-5 / 1.1e-4 |
| 굴절률 | 0.0113 | 0.961 | 90% / 98% | 0.0094 / 0.015 / 0.024 |
| 열전도도 | 0.019 W/(m K) | 0.724 | 87% / 96% | 0.016 / 0.022 / 0.052 |

화면의 **교차검증 결과 보기**와 MCP `screen_polymer`의 `include_validation`이 같은 계산을 다시 수행합니다. 적용 범위는 학습 고분자와의 최대 MinMax 유사도로 나눕니다. 위 표처럼 0.35 미만에서는 오차가 3–5배 커지므로 화면에 "학습 범위 밖"으로 표시합니다. 같은 데이터셋 안의 무작위 분할이므로, 전혀 다른 화학군에 대한 오차는 이보다 클 수 있습니다.

같은 지문으로 비교한 ExtraTrees(기술자 + 지문)는 밀도 MAE 0.0215, Cp 92.9로 GP와 비슷했습니다. 불확실성과 최근접 근거를 함께 주는 GP를 택했습니다.

### 해석 한계

- PI1070 값은 실험값이 아니라 GAFF2 계열 고전 MD 결과입니다(사슬 10개, 각 약 1,000 원자).
- 고전 MD의 Cp는 모든 진동 모드를 고전적으로 세므로 실험 Cp보다 큰 경향이 있습니다.
- Polymer Lab의 탐색용 벌크 경로는 Gasteiger 전하와 축소한 단계 길이를 쓸 수 있어 PI1070과 정확히 같지 않습니다.
- Tg, 결정화도, 분자량·가공 이력의 영향은 이 참조 데이터에 없습니다.

PolyMetriX 정제 Tg 데이터(MIT, Zenodo)와 OpenPoly(CC BY 4.0, Hugging Face)는 이 개발 환경에서 내려받을 수 없어 넣지 않았습니다. 실험 Tg 예측은 지금처럼 검토한 측정값으로 **물성 학습 · 예측**에서 학습합니다.

## Hansen 용해도 파라미터 (HSP)

| 계산 | 식과 출처 |
|---|---|
| 거리 | Ra² = 4(δD₁−δD₂)² + (δP₁−δP₂)² + (δH₁−δH₂)², RED = Ra / R0 |
| 용매 χ | χ = α V<sub>용매</sub> Ra² / (4RT), α = 0.6 (Lindvig, Michelsen, Kontogeorgis, Fluid Phase Equilib. 203, 247 (2002)). 긴 사슬의 임계값 약 0.5와 비교 |
| 두 용매 혼합 | 쌍마다 부피분율 φ를 닫힌 식으로 구해 Ra를 최소화. R0가 있으면 두 성분이 모두 구 밖인데 혼합물이 구 안인 조합(비용매 쌍)을 따로 보여 줍니다 |
| 구 피팅 | Hansen 고전 DATAFIT(용매별 바람직도의 기하평균)를 최대화하고, 같은 값이면 가장 작은 R0를 고릅니다. 시험 용매 범위의 격자 탐색 후 정육면체 꼭짓점 언덕 오르기 |
| 블렌드 | 정규 용액 χ = V<sub>ref</sub> Ra² / (4RT) (α = 1), 임계값 χc = ½(N<sub>A</sub><sup>−½</sup> + N<sub>B</sub><sup>−½</sup>)², N = 중합도 × 반복단위 몰부피 / V<sub>ref</sub> |

용매 데이터는 HSPiPy 1.1.8 `hspipy/db.csv`(MIT, `Gnpd/HSPiPy@b2cd91aa`, SHA-256 `42cb4179…`, PyPI 휠과 바이트 동일)에서 몰부피가 있는 248종입니다. Hansen 문헌 표의 값과 일치하는지 일부 확인했습니다(톨루엔 18.0/1.4/2.0, V 106.8; 물 15.5/16.0/42.3). 단위는 MPa½이며 (cal/cm³)½ 입력은 2.0455배로 환산합니다.

**구 피팅 검증:** HSPiPy 예제 37개 용매(점수 1 = 용해)에서 HSPiPy의 고전 피팅은 DATAFIT 0.97361(16.58/13.95/11.47, R0 11.58)입니다. Polymer Lab은 격자 탐색으로 0.97426(16.75/14.49/11.21, R0 12.02)을 찾았습니다. 완전 분리되는 HSPiT 예제 27개에서는 두 구현 모두 DATAFIT 1.0이며 Polymer Lab이 더 작은 R0(9.21, HSPiPy 9.49)를 고릅니다. 시험 용매가 10개 미만이거나, 불용 판정이 없거나(R0는 하한), 중심이 시험 범위 경계에 있으면 경고합니다.

피팅 결과는 `hsp_delta_d`·`hsp_delta_p`·`hsp_delta_h`·`hsp_radius`(MPa½) 측정값 초안이 됩니다. **측정값 초안으로 보내기**는 δD를 측정값 양식에 넣고, 나머지 값은 안내 메시지로 보여 줍니다.

**블렌드 한계:** 정규 용액 χ는 항상 0 이상입니다. 수소결합·이온 상호작용으로 섞이는 블렌드를 비상용으로 잘못 판정할 수 있고, LCST/UCST 같은 χ의 온도 의존성·결정화·분자량 분포는 다루지 않습니다. 반복단위 몰부피를 입력하지 않고 SMILES만 주면 RDKit 반복단위 질량 / PI1070 밀도 예측값으로 계산하며, 그 근거를 결과에 적습니다.

## 데이터 재현

번들 데이터(`workbench/data/`)는 REA로 감사한 고정 원본에서 다시 만들 수 있습니다.

```sh
.venv/bin/python scripts/audit_extensions.py --fetch-pinned --run-rea --only radonpy-pi1070 --only hspipy
.venv/bin/python scripts/build_reference_data.py --check --audit-wheel
```

`reference_data.json`에 원본 저장소·커밋·SHA-256·라이선스·행 수·제외 행(몰부피가 없는 Diethyl ketone)을 기록합니다. `--audit-wheel`은 PyPI HSPiPy 1.1.8 휠을 REA `inspect-artifact`로 목록화하고 그 안의 `db.csv`가 고정 저장소 파일과 같은지 `evidence/artifacts/hspipy.json`에 기록합니다. 원본 라이선스 전문은 `workbench/data/licenses/`에 있습니다.

## API · MCP

| HTTP | MCP 도구 | 동작 |
|---|---|---|
| `GET /api/research/screening` | — | 모델 카드, 참조 데이터 출처 |
| `GET /api/research/screening/validation` | `screen_polymer` (`include_validation`) | 5겹 교차검증 |
| `POST /api/research/screening/predict` | `screen_polymer` | 반복단위 최대 20개 예측 + 내 벌크 계산 비교 |
| `GET/POST /api/research/screening/solvents` | `rank_solvents` | 용매 검색, RED·χ 순위, 두 용매 혼합 |
| `POST /api/research/screening/hsp-fit` | `fit_hsp_sphere` | 용해 시험 → Hansen 구, 측정값 초안 |
| `POST /api/research/screening/blend` | `blend_compatibility` | 고분자 블렌드 χ와 χc |

모든 MCP 도구는 읽기 전용입니다. 저장은 `add_measurement`로만 하며 항상 검토 전 상태입니다.
