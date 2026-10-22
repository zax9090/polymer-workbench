# 단백질–고분자 준비 브릿지

Polymer Lab 0.3의 `mixture` 경로는 입력 구조, 실제 설치 도구, 명시한 고분자 블록을 확인하고 구성요소 생성 또는 검토 가능한 준비 레시피를 기록한다. 혼합계 MD를 완료했다고 표시하지 않는다. 모든 브릿지 결과에는 `simulation_executed=false`와 남아 있는 요구 조건이 저장된다.

## Martini 3

`bridge_track=martini3`, `bridge_mode=protein_cg`는 실제 martinize2와 polyply를 실행한다. 표준 아미노산만 들어 있는 단일 모델 PDB, 잔기 수와 일치하는 명시적 H/E/C 이차구조 문자열, 설치된 Martini 3 고분자 블록이 필요하다. UI/API의 기본 고분자 블록은 PEO다. 이차구조는 검토한 DSSP 등에서 가져와야 하며 프로그램이 임의 추정하지 않는다. 비표준 잔기, 미분리 리간드·물·이온, 해결하지 않은 대체 배치 등은 거부한다.

martinize2는 `-ff martini3001 -p backbone`으로 단백질 좌표와 topology/ITP를 만들고 polyply `gen_params -lib martini3`가 고분자 ITP를 만든다. 고분자 SMILES를 임의의 Martini 매핑으로 바꾸지 않는다. 탄성 네트워크와 IDR 처리는 자동 선택하지 않는다. 생성물은 **구성요소**다. 전체 고분자 좌표, 혼합 좌표, 통합 토폴로지, 용매·이온 수, 전하/1-4 상호작용 등은 아직 검토·조립해야 한다.

함께 저장되는 GROMACS .mdp 파일은 최소화 → restrained NVT → restrained NPT → 짧은 production의 시작 템플릿이다. `command_plan.json`은 argv 배열로 명령과 전제조건을 기록하며 실행 셸 파일이 아니다. `insane`의 용매화 명령도 검토된 mixed_solute.pdb와 완전한 매개변수를 전제로 한다. `-maxwarn` 같은 경고 무시 옵션을 붙이지 않는다. POSRES include와 일치한 원자 인덱스, 비결합 Martini 파일, 물·이온 모델, 분자 순서/개수, charge와 salt concentration, grompp 통과가 필요하다.

## AMBER 및 OpenFF

AMBER 트랙은 ff14SB/GAFF2/TIP3P 후보 조합의 antechamber → parmchk2 → tleap 입력과 단계별 sander 템플릿을 저장한다. 전하와 적용 범위가 검토된 capped polymer, 일관된 원자 순서, 겹치지 않는 단백질/고분자 좌표가 필요하다. tleap 예제는 고분자 **한 사슬**용이며 여러 사슬이면 사용자가 명시적으로 확장해야 한다. 이온 처리는 중화만 하고 목표 염 농도에 필요한 이온 쌍 수를 자동 추정하지 않는다. 실제 외부 AMBER 도구 실행은 하지 않는다.

OpenFF 트랙은 현재 실행 가능한 혼합계 생성기가 아니라 통합 명세를 저장한다. Sage 파일, charge model, 고분자 적용 범위, 단백질과의 호환 조립 및 지원 엔진 export가 필요하다. NAGL이 수천 원자를 일정 시간 안에 처리하거나 정확한 전하를 보장한다고 주장하지 않는다. ParmEd/InterMol이 임의의 힘장을 LAMMPS로 손실 없이 바꾼다는 가정도 하지 않는다.

## 계산 해석과 증거

용매에 있는 단백질계에 벌크 고분자의 21단계 고압 압축을 그대로 적용하지 않는다. CG 시간은 모델에 의존하고 긴 물리 시간 또는 평형 도달을 보장하지 않는다. 짧은 pilot 뒤에는 힘, 에너지, 밀도, 구조 안정성과 독립 복제 계산을 확인해야 한다.

`tests/test_bridges.py`는 불완전한 레시피가 blocked로 남는지, 버전 혼합/임의 매핑/부적절한 PDB를 거부하는지 검사한다. 설치 도구가 있으면 RDKit으로 만든 6잔기 알라닌 **소프트웨어 테스트 구조**와 명시적 CCCCC C 이차구조 가정(공백 없이 6글자)을 사용해 실제 martinize2+polyply 구성요소 파일 생성도 실행한다. 이는 생물학적 단백질 구조 또는 용액 MD 안정성 검증이 아니다.

`evidence/extensions/audit.json`에 새 도구 8개의 선택된 upstream 원본, 고정 커밋, 파일 해시, API/CLI 위치가 기록된다. `rea/*.command.json`은 실제 `rea import-reference-source` 호출과 종료 코드, 실행 전후 source hash를 보관한다. REA는 여기서 선택 소스의 정체성과 인터페이스를 조사했으며 전체 저장소 디컴파일·라이선스 법률 검토·힘장 타당성 검증을 수행한 것은 아니다.
