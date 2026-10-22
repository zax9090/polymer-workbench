# 단백질–고분자 준비 브릿지

Polymer Lab 0.3의 `mixture` 경로는 입력 구조, 실제 설치 도구, 명시한 고분자 블록을 확인하고 구성요소 생성 또는 검토 가능한 준비 레시피를 기록한다. `prepare`·`protein_cg` 결과에는 `simulation_executed=false`와 남아 있는 요구 조건이 저장된다. Martini 3 `simulate` 모드만 아래에 설명한 용매화 혼합계 GROMACS 계산을 실제로 실행하고, 실행이 끝났을 때만 `simulation_executed=true`를 기록한다.

## Martini 3

`bridge_track=martini3`, `bridge_mode=protein_cg`는 실제 martinize2와 polyply를 실행한다. 표준 아미노산만 들어 있는 단일 모델 PDB, 잔기 수와 일치하는 명시적 H/E/C 이차구조 문자열, 설치된 Martini 3 고분자 블록이 필요하다. UI/API의 기본 고분자 블록은 PEO다. 이차구조는 검토한 DSSP 등에서 가져와야 하며 프로그램이 임의 추정하지 않는다. 비표준 잔기, 미분리 리간드·물·이온, 해결하지 않은 대체 배치 등은 거부한다.

martinize2는 `-ff martini3001 -p backbone`으로 단백질 좌표와 topology/ITP를 만들고 polyply `gen_params -lib martini3`가 고분자 ITP를 만든다. 고분자 SMILES를 임의의 Martini 매핑으로 바꾸지 않는다. 탄성 네트워크와 IDR 처리는 자동 선택하지 않는다. 생성물은 **구성요소**다. 전체 고분자 좌표, 혼합 좌표, 통합 토폴로지, 용매·이온 수, 전하/1-4 상호작용 등은 아직 검토·조립해야 한다.

함께 저장되는 GROMACS .mdp 파일은 최소화 → restrained NVT → restrained NPT → 짧은 production의 시작 템플릿이다. `command_plan.json`은 argv 배열로 명령과 전제조건을 기록하며 실행 셸 파일이 아니다. `insane`의 용매화 명령도 검토된 mixed_solute.pdb와 완전한 매개변수를 전제로 한다. `-maxwarn` 같은 경고 무시 옵션을 붙이지 않는다. POSRES include와 일치한 원자 인덱스, 비결합 Martini 파일, 물·이온 모델, 분자 순서/개수, charge와 salt concentration, grompp 통과가 필요하다.

## Martini 3 용매화 혼합계 MD

`bridge_track=martini3`, `bridge_mode=simulate`는 위 구성요소 생성 뒤 다음을 실제로 실행한다. 각 단계가 실패하면 그 자리에서 작업을 실패로 끝내며, 앞 단계 산출물과 로그를 보존한다.

1. **힘장 고정:** `marrink-lab/martini-forcefields` 커밋 `784591eb`의 `martini_v3.0.0.itp`, `solvents_v1`, `ions_v1`, LICENSE를 SHA-256으로 확인한다. 데이터 폴더의 `forcefields/martini3/<commit>/`에 한 번 받아 두고 작업 폴더에는 하드 링크(불가하면 복사)로 넣는다. `MARTINI3_FF_DIR`로 직접 준 파일도 해시가 다르면 거부한다. 이 저장소는 REA 확장 감사(`evidence/extensions/`)에 고정 원본·인터페이스 위치와 함께 기록했다.
2. **토폴로지 조립:** martinize2가 쓴 단백질 ITP와 polyply `polymer.itp`의 실제 전하를 합산한다. 고분자 한 사슬의 전하가 `polymer_net_charge`와 다르면 거부해 사용자가 전하를 확인하게 한다. 물 W 개수는 박스 부피 × 8.36 beads/nm³에서 용질 비드 수를 뺀 추정치, Na⁺/Cl⁻ 쌍은 물 부피 기준 목표 몰농도에서 계산하고 반대 이온을 더해 계 전체 전하를 0으로 만든다. 실제 개수·추정 농도·방법은 `solvation.json`에 남는다.
3. **좌표 생성:** 단백질을 박스 중앙에 두고(한 면당 1 nm 여유가 없으면 거부) `polyply gen_coords -c protein_centered.gro`가 고분자 사슬·물·이온을 배치한다. polyply 1.8 CLI는 시드 옵션이 없어 좌표 배치는 비트 단위로 재현되지 않는다.
4. **GROMACS 4단계:** `01_minimize`(steepest descent, Fmax < 100 kJ/mol/nm 수렴 확인) → `02_nvt`(위치 구속, `-r 01_minimize.gro`) → `03_npt`(위치 구속, C-rescale) → `04_production`. 각 단계는 `grompp`를 경고 무시 옵션 없이 통과해야 한다. NVT·NPT 스텝은 `equilibration_steps`, 생산 스텝은 `steps`(dt 10 fs)로 정한다. OpenMP 스레드는 `POLYMER_MD_THREADS`(기본 최대 4)이다. 탄성 네트워크는 `elastic_network=true`일 때만 `-elastic -ef 700 -el 0.5 -eu 0.9`로 추가한다.
5. **분석:** `gmx trjconv -pbc mol` 후 MDAnalysis로 단백질 BB RMSD, 단백질·고분자 회전반경, 지정 거리 안의 접촉 고분자 비드 수, 최소 거리를 계산하고 `gmx energy`로 NPT·생산 구간의 위치 에너지·온도·압력·밀도를 읽는다. 표본이 20개 이상이면 PyMBAR 정상성 후보 구간을 기록한다(`equilibrated`는 항상 false). 결과는 `mixture_analysis.json`, 최종 프레임의 단백질+고분자는 `structure.pdb`(물·이온 생략, Å)에 저장한다.

완료 상태는 `completed`, `simulation_executed=true`이다. GROMACS가 없으면 조립한 `system.top`/`system.gro`를 남기고 `blocked`, 힘장을 받지 못하면 그 이유와 함께 `blocked`로 끝난다. 색인된 피처(`contact_count`, `protein_rmsd`, `polymer_rg_mean` 등)는 탐색 캠페인 목표로 쓸 수 있다. 혼합계 캠페인은 단백질 입력·스텝·힘장 조건을 고정하고 polyply 블록(원-핫)·사슬 길이·사슬 수·온도·박스·염 농도를 탐색한다.

이 경로는 짧은 CG 파일럿이다. 접촉 수는 기준 거리와 비드 해상도에 따라 달라지며 흡착·결합 자유에너지가 아니다. 평형 판단에는 충분히 긴 생산 구간, 독립 반복 계산, 관측량별 수렴 확인이 필요하다. 고분자 Martini 매핑과 말단 처리는 polyply 라이브러리 블록 정의를 그대로 따른다.

## AMBER 및 OpenFF

AMBER 트랙은 ff14SB/GAFF2/TIP3P 후보 조합의 antechamber → parmchk2 → tleap 입력과 단계별 sander 템플릿을 저장한다. 전하와 적용 범위가 검토된 capped polymer, 일관된 원자 순서, 겹치지 않는 단백질/고분자 좌표가 필요하다. tleap 예제는 고분자 **한 사슬**용이며 여러 사슬이면 사용자가 명시적으로 확장해야 한다. 이온 처리는 중화만 하고 목표 염 농도에 필요한 이온 쌍 수를 자동 추정하지 않는다. 실제 외부 AMBER 도구 실행은 하지 않는다.

OpenFF 트랙은 현재 실행 가능한 혼합계 생성기가 아니라 통합 명세를 저장한다. Sage 파일, charge model, 고분자 적용 범위, 단백질과의 호환 조립 및 지원 엔진 export가 필요하다. NAGL이 수천 원자를 일정 시간 안에 처리하거나 정확한 전하를 보장한다고 주장하지 않는다. ParmEd/InterMol이 임의의 힘장을 LAMMPS로 손실 없이 바꾼다는 가정도 하지 않는다.

## 계산 해석과 증거

용매에 있는 단백질계에 벌크 고분자의 21단계 고압 압축을 그대로 적용하지 않는다. CG 시간은 모델에 의존하고 긴 물리 시간 또는 평형 도달을 보장하지 않는다. 짧은 pilot 뒤에는 힘, 에너지, 밀도, 구조 안정성과 독립 복제 계산을 확인해야 한다.

`tests/test_bridges.py`는 불완전한 레시피가 blocked로 남는지, 버전 혼합/임의 매핑/부적절한 PDB를 거부하는지 검사한다. 설치 도구가 있으면 RDKit으로 만든 6잔기 알라닌 **소프트웨어 테스트 구조**와 명시적 CCCCC C 이차구조 가정(공백 없이 6글자)을 사용해 실제 martinize2+polyply 구성요소 파일 생성도 실행한다. 이는 생물학적 단백질 구조 또는 용액 MD 안정성 검증이 아니다.

`evidence/extensions/audit.json`에 새 도구 8개의 선택된 upstream 원본, 고정 커밋, 파일 해시, API/CLI 위치가 기록된다. `rea/*.command.json`은 실제 `rea import-reference-source` 호출과 종료 코드, 실행 전후 source hash를 보관한다. REA는 여기서 선택 소스의 정체성과 인터페이스를 조사했으며 전체 저장소 디컴파일·라이선스 법률 검토·힘장 타당성 검증을 수행한 것은 아니다.
