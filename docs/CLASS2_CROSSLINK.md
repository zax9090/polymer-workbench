# PCFF 벌크 셀 · 에폭시 가교 · REA로 고른 도구

매트릭스 4단계(Forcite/COMPASS 계열 힘장, 에폭시·하이드로젤 가교)와 2·5·6단계의 보조 도구를 공개 프로그램으로 채웠습니다. 프로그램마다 내려받은 배포본이나 고정 커밋을 REA로 목록화하고, 실제로 쓰는 인터페이스를 읽은 뒤 연결했습니다. 상용 프로그램의 파일은 쓰지 않습니다.

## REA로 확인한 내용

| 프로그램 | REA 대상 | 확인한 사실 | Polymer Lab에서의 결정 |
|---|---|---|---|
| EMC 9.4.4 (GPL-3.0, 실행 파일) | SourceForge 배포본 `emc_linux_x86_64_v9.4.4_20260701.tgz` (SHA-256 `97f8deba…`), ELF 헤더·섹션(objdump) | SMILES 그룹으로 PCFF 타입·결합 증분 전하를 정하고 사슬을 키워 LAMMPS class2 데이터·계수 파일을 씁니다. 내장 `pcff.frc`는 5.1판(2020), COMPASS는 LAMMPS 배포용 공개 부분집합입니다. 배포본 안의 `pcff+`·`trix` 링크는 배포본 밖을 가리켜 풀지 않습니다. 실행 파일은 2027-07-01까지 유효하다고 배포 페이지에 적혀 있습니다. | PCFF 셀 생성기로 사용 |
| LAMMPS `msi2lmp` (GPL-2.0) | `tools/msi2lmp` 소스, `frc_files`, 시험 입력과 기준 출력 | 계수는 정방향·역방향 일치 → `*` 와일드카드 → `#equivalence` 순으로 찾고, 같은 타입 조합은 높은 판을 씁니다. `#auto_equivalence`와 결합 증분은 쓰지 않습니다. `frc_files`는 퍼블릭 도메인입니다. | 규칙을 `workbench/class2.py`로 옮겨 EMC가 쓴 계수를 독립적으로 다시 계산 |
| LAMMPS `fix bond/react` (GPL-2.0) | `src/REACTION/fix_bond_react.cpp`, `examples/PACKAGES/reaction/tiny_epoxy` | 템플릿·맵 형식(InitiatorIDs, EdgeIDs, Equivalences). 이 판은 `custom_charges`를 주지 않으면 **가장자리 원자를 포함한 템플릿 전체의 전하를 다시 씁니다**(`recharged = 1` 기본값). | 전하가 실제로 바뀌는 원자만 `recharge` 조각으로 지정 |
| pysimm 1.1 (MIT) | `forcefield/pcff.py`, `apps/polymatic.py`, `apps/random_walk.py` | 폴리스티렌 올리고머에서 방향족 탄소를 `cp`가 아닌 `c=2`, 수소를 `hc`가 아닌 `h`로 타입 지정했습니다. Polymatic 가교는 배포되지 않은 Perl 스크립트를 부릅니다. | PCFF 타입 지정·가교에는 쓰지 않음 |
| xtb 6.7.1 (LGPL-3.0, 실행 파일) | GitHub 배포본 `xtb-6.7.1-linux-x86_64.tar.xz` (`62a8d187…`), ELF 헤더 | GFN1-xTB 단일점 출력에 CM5 전하 표가 있습니다. 85원자 계산이 0.3초입니다. | GAFF2 벌크 셀의 선택 전하(`charge_method: xtb_cm5`) |
| pybaselines 1.2.1 (BSD-3) | PyPI 휠 | `Baseline.arpls` | DSC 용융 봉우리, GPC 자동 기준선 |
| PDBFixer 1.12.0 (MIT) | PyPI sdist(휠 없음) | `findMissingResidues`, `replaceNonstandardResidues`, `addMissingAtoms(seed)`, `removeHeterogens` | 단백질 가져오기의 선택 보정 |
| HiPlot 0.1.33 (MIT) | PyPI 휠 | 독립 HTML은 `hiplot.render(element, {dataProviderName: 'none', experiment})` 한 번 호출입니다. Python 서버는 flask·ipython이 필요합니다. 번들의 테마 CSS가 Google Fonts를 `@import`합니다. | 브라우저 번들만 넣고 그 `@import` 한 줄을 지움(원본·수정본 해시 기록) |

REA 6.7.0의 `inspect-artifact`는 tar 압축 파일을 열지 못합니다. 그래서 EMC·xtb·PDBFixer는 Python `tarfile`로 구성 파일 해시를 만들고, 실행 파일은 REA `inspect-with-objdump`로 확인했습니다. 심층 디스어셈블러(Ghidra·Hopper·IDA·Kuna)는 이 환경에 없습니다. 기록은 `evidence/artifacts/{emc,xtb,pdbfixer,pybaselines,hiplot}.json`과 `evidence/extensions/audit.json`(`lammps-msi2lmp`, `lammps-reaction`, `pysimm`)에 있습니다.

## PCFF 비정질 벌크 셀

`atomistic_mode: bulk`에서 `atomistic_forcefield: pcff`를 고르면 다음 순서로 실행합니다.

1. EMC가 반복단위를 `*` 연결점 그룹으로 바꾸고, 수소로 말단을 막은 사슬을 0.5 g/cm³로 키웁니다. PCFF 타입과 결합 증분 전하를 정하고 `polymer.data`·`polymer.params`(class2 전 항목)를 씁니다.
2. EMC가 쓴 계수 줄마다 LAMMPS 공개 `pcff.frc`(4.0판, `workbench/data/forcefields/lammps_pcff.frc`)에서 msi2lmp 규칙으로 값을 다시 계산합니다. 원자 전하도 결합 증분으로 다시 계산해 `pcff_parity.json`에 남깁니다.
3. RadonPy `EQ21step`과 같은 단계표로 LAMMPS를 실행합니다(600 K 풀림, 0.02–1.00 × 50,000 atm 압축과 감압, Nose–Hoover NPT, X–H SHAKE, 1 fs). 이어서 NPT 샘플링을 합니다.
3a. LAMMPS를 실행하기 전에 `class2.check_coefficients`가 데이터 파일 머리글의 타입 수와 `polymer.params`를 대조해 `coefficient_check.json`에 남깁니다. 모든 타입에 `pair_coeff i i`(ε ≥ 0, σ > 0)와 Class II 항(bond, angle, dihedral, improper와 bb·ba·mbt·ebt·at·aat·bb13·aa 교차항)이 정해진 개수의 유한한 값으로 있어야 하고, 결합·각 K2 > 0, 0 < r0, 0 < θ0 ≤ 180°여야 합니다. 하나라도 어긋나면 실행 전에 실패합니다. EMC가 `Missing force field parameters`로 멈추면(예: Si–O의 PDMS, 인산 에스터) 빠진 항목 이름을 오류에 그대로 옮기고 GAFF2 경로를 쓰라고 안내합니다. 힘장을 섞어 채우지 않습니다.
4. 샘플링 로그는 RadonPy `Analyze`가 그대로 처리해 밀도·Cp·압축률·체적 탄성률·열팽창을 계산하고 수렴 기준 충족 여부를 판정합니다. 그래서 GAFF2 경로와는 힘장만 다릅니다.

GAFF2 경로의 `charge_method: xtb_cm5`는 말단을 막은 사슬의 RDKit 구조에서 GFN1-xTB 단일점 CM5 전하를 계산해 Gasteiger 대신 씁니다. 기기별 경로는 작업 설정(`config.json`)이 아니라 `xtb_engine.json`에 기록합니다. 이 전하는 RadonPy 기준 프로토콜의 RESP 전하를 대신하는 근사입니다.

## 에폭시–아민 가교 (`atomistic_mode: crosslink`)

1. RDKit으로 말단 에폭시(글리시딜)와 1차 아민을 찾고, 결합을 직접 고쳐 생성물을 만듭니다. 고리 C–O를 끊고 C–N을 만들며, N–H의 H를 O–H로 옮깁니다. 1차 아민 생성물(A)과, A의 같은 N이 두 번째 수지를 연 생성물(B)을 만듭니다.
2. EMC가 수지·경화제와 A·B 한 분자씩을 함께 PCFF로 타입 지정합니다. A·B는 반응 후 타입과 계수를 데이터 파일에 넣으려고 만든 것이며, LAMMPS에서 실행 전에 지웁니다.
3. RDKit 그래프와 EMC 분자를 원소 표지 그래프 동형으로 대응시킵니다. 결합·타입·전하가 바뀌는 원자에서 3결합 이내 원자로 반응 전후 템플릿을 잘라 냅니다. 바깥과 결합한 원자는 가장자리 원자입니다. 다른 반응기가 템플릿 안에 들어오면 계산을 거부합니다. 생성 결과는 DGEBA–EDA에서 1차 반응 21원자(가장자리 2개), 2차 반응 27원자(가장자리 4개)입니다.
4. 21단계 일정으로 혼합물을 고밀도화하고, 경화 온도에서 `fix bond/react`로 두 반응을 동시에 진행합니다. 안정화는 반응 원자 100스텝 `nve/limit`이고, 반응 거리 기본값은 4 Å입니다. 목표 전환율에 이르면 `fix halt`로 멈춥니다. 막 생긴 결합을 최소화와 제한 운동으로 이완한 뒤 냉각 NPT, 샘플링 NPT를 실행합니다.
5. 전환율 시계열, 이상적 Flory–Stockmayer 겔화 전환율(Macosko–Miller, 아민 수소가 부족하면 없음), 밀도·체적 탄성률을 보고합니다.

`cure_cutoff_schedule: stepwise`는 반응 거리를 3.5 Å에서 0.5 Å씩 최종 `reaction_cutoff_angstrom`까지 늘립니다. `cure_steps`를 단계 수로 나눠 각 단계를 실행하고, 단계가 끝날 때 목표 전환율에 이르렀으면 남은 단계를 건너뜁니다(`jump SELF cured`). 가까운 쌍이 먼저 반응해 처음부터 먼 쌍을 끌어당기는 결합이 줄어듭니다. `secondary_amine_probability`(기본 1)는 2차 아민 반응에만 `prob`를 붙여, 조건을 만족한 단계마다 그 확률로 반응시킵니다. 두 값은 `cure_complete.json`의 `cutoff_schedule`, `secondary_amine_probability`에 남습니다.

반응은 반응 원자가 거리 안에 들어오면 일어납니다(2차 아민은 지정한 확률). 2차 아민 확률은 가정한 반응성 차이이며 속도 상수가 아닙니다. 그래서 결과는 네트워크 구조이며 경화 속도론이 아닙니다. 에테르화·단독중합·확산 지배 감속은 다루지 않습니다.

## 측정 곡선 · 단백질 · 다변수 비교

- **DSC 용융**: `analysis: melting`. 곡선 전체에 arPLS 기준선(λ 기본 10⁶)을 맞추고, 적분 구간의 흡열 면적을 가열 속도로 나눠 ΔHm(J/g)을 구합니다. 흡열 방향과 W/g·mW/mg·mW(시료 질량 필요) 단위는 반드시 지정해야 합니다. 100% 결정 기준 엔탈피를 주면 결정화도를 함께 계산합니다. 새 물성은 `melting_temperature`(K), `melting_enthalpy`(J/g), `crystallinity`(fraction)입니다.
- **GPC**: `baseline_method: arpls`로 기준선 창 대신 자동 기준선을 씁니다.
- **PDBFixer**: `protein_repair: pdbfixer`. 빠진 무거운 원자, SEQRES의 내부 결손 잔기, 변형 잔기를 보정합니다. 말단 결손 잔기는 만들지 않고, 리간드·물은 제거하며, 수소는 붙이지 않습니다(PDB2PQR 몫). 제출한 원본은 `structure_submitted.pdb`, 변경 내역은 `repair.json`에 남깁니다.
- **HiPlot**: 연구 데이터 화면의 **다변수 비교**는 색인한 계산의 피처·조건(힘장, 전하, 단계 길이)과 같은 재료의 측정값을 평행 좌표·산점도·분포·표로 보여 줍니다.

## 검증

| 항목 | 결과 |
|---|---|
| `class2.py` ↔ msi2lmp | LAMMPS의 class2b(pcff) 시험 6개(ethane, benzene, naphthalene, cnt-hexagonal, hap_crystal, h2-h2o)의 기준 출력 계수를 모두 재현 |
| EMC ↔ LAMMPS `pcff.frc` | PE 31/31, PS 171/171, DGEBA–EDA 경화 셀 333/333 계수 일치. PS의 30개(c–c–c1–cp 비틀림 계열)와 경화 셀의 70개는 EMC의 `cff91_auto` 대체값으로, msi2lmp는 배정하지 않는 항목 |
| PCFF 전하 | 결합 증분 재계산과 EMC 전하 차이 0, 셀 순전하 0 |
| 경화 전하 보존 | `custom_charges` 없이 26회 반응 후 순전하 −2.324 e(가장자리 N 전하가 되돌려짐). 적용 후 0 |
| PS PCFF 셀 | 6사슬 × 20단위, 1,932원자, 단계 길이 ×0.02, 34분: 밀도 0.968 g/cm³, 체적 탄성률 2.09 GPa(실험 밀도 약 1.04–1.05 g/cm³). 단축 계산이라 RadonPy 수렴 기준 미달 |
| PE PCFF 셀 | 6사슬 × 50단위, 1,812원자, ×0.02, 31분: 밀도 0.776 g/cm³, 체적 탄성률 1.95 GPa(비정질 PE 실험 밀도 약 0.85 g/cm³). 계수 31/31 일치, 대체값 0. 수렴 기준 미달 |
| DGEBA–EDA 경화 | 16 + 8분자, 880원자, ×0.002: 27,600스텝에서 에폭시 전환율 81%(겔화 이론값 57.7%). 냉각 후 밀도 0.98 g/cm³(단축 고밀도화) |
| 계단식 반응 거리 | DGEBA–EDA 6 + 3분자, ×0.001, 경화 8,000스텝, 3.5→5.0 Å 4단계 × 2,000스텝, 2차 아민 확률 0.35: 6,288스텝에서 목표 0.6을 넘겨(전환율 0.667, 1차 6·2차 2회) 남은 단계를 건너뛰고 냉각으로 이동 |
| 계수 점검 | 합성 params에서 0인 K2, 빠진 교차항, 유한하지 않은 값을 각각 찾아냄. EMC 셀 PE·PS·PVDF·PTFE·폴리티오펜·DGEBA–EDA는 통과. PDMS·인산 에스터는 EMC가 계수 부족으로 멈추고 빠진 항목을 보고 |
| DSC 용융 | 기울어진 기준선 위 가우스 봉우리 합성 곡선에서 ΔHm 72.191 J/g(해석값과 같음), Tm 오차 0.1 K 이내 |
| xtb CM5 | LAMMPS 데이터의 원자 전하가 xtb 출력 CM5 값과 일치(잔여 전하 균등 보정 < 10⁻⁶) |

수치는 모두 탐색용 단축 계산의 결과입니다. 물성 예측의 정확도를 뜻하지 않습니다. 단계 길이 배율 1(RadonPy 기본 길이)은 데스크톱에서 수 시간~수 일이 걸립니다.

## 재실행

```sh
.venv/bin/python scripts/audit_artifacts.py --only emc --only xtb --only pdbfixer --only pybaselines --only hiplot
.venv/bin/python scripts/audit_extensions.py --fetch-pinned --run-rea --only lammps-msi2lmp --only lammps-reaction --only pysimm
.venv/bin/python -m pytest -q tests/test_class2.py tests/test_crosslink.py tests/test_rea_tools.py
```

EMC와 xtb는 첫 사용 때 `workbench/engines.py`의 고정 주소에서 받아 SHA-256을 확인하고 데이터 폴더의 `engines/`에 풉니다. 검토한 기존 설치본을 쓰려면 `EMC_ROOT`, `XTB_EXEC`를 지정하세요.
