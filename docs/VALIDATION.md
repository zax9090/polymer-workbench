# 실행 검증 기록

2026-10-22, Linux x86_64 / Python 3.13.12에서 검증했습니다. 모든 계산은 이번 프로젝트의 전용 환경에서 실행했습니다.

## 자동 테스트

```sh
PATH="$PWD/.venv/bin:$PATH" LD_LIBRARY_PATH="$PWD/.venv/lib:${LD_LIBRARY_PATH:-}" .venv/bin/pytest -q
```

**18 passed**, 9.83초. 테스트 클라이언트의 httpx 호환성에 관한 upstream deprecation 경고 1개가 있으며 실패는 없습니다.

- 조대화 힘을 포텐셜의 수치 미분과 비교.
- 실제 LAMMPS 에너지를 독립 NumPy 포텐셜 구현과 비교.
- 알려진 사슬의 회전반경, MDAnalysis 교차 확인.
- RDKit 결합 연결·말단 처리·좌표 유효성.
- NumPy 계산의 동일 시드 재현성과 실제 시간 변화.
- 실제 RadonPy/LAMMPS의 350 K 설정 전달, 동일 시드 두 실행의 좌표·실제 엔진 시드 일치.
- PDB의 일반 B-factor와 AlphaFold pLDDT 구분, NaN/Inf 거부.
- AF3 입력 준비를 예측 완료로 표시하지 않는지 확인.
- API 입력·원본 ZIP·체크섬·재실행·경로 접근 검사.
- 실제 종료 신호를 무시하는 자식 프로세스까지 중단하고 파일이 더 이상 바뀌지 않는지 확인.

## 실제 HTTP 워크플로우 검증

`evidence/runtime-validation.json`에 실제 작업 ID·상태·계산 엔진·요약을 보관했습니다. 해당 원본 결과는 이 컴퓨터의 `data/jobs/<ID>/`와 화면의 실험 기록에서 확인할 수 있습니다. 소스 배포 ZIP은 실행 데이터 전체 대신 검증 요약을 포함합니다.

| 경로 | 관측한 결과 |
|---|---|
| LAMMPS 조대화 MD | 실제 48비드, 1,000스텝 계산 완료 |
| RadonPy / LAMMPS | 실제 350 K 설정의 고립 사슬 NVT 완료 |
| AlphaFold DB P69905 | 기존 예측 PDB 다운로드·구조·신뢰도 분석 완료 |
| AlphaFold 구조 → PACKMOL | 이전 구조와 고분자 2사슬의 실제 배치 완료, MD 전 준비 상태 |
| polyply Martini 3 PEO | 실제 분자 ITP 생성, 전체 힘장/MD 전 준비 상태 |
| pysimm / LAMMPS | 실제 GAFF2/Gasteiger 최소화 완료 |
| AlphaFold 3 | FASTA·입력 JSON 내보내기, 새 추론 미실행 |
| ColabFold | 미설치 의존성을 감지하고 blocked, 새 추론 미실행 |
| HOOMD-blue | 미설치 의존성을 감지하고 blocked, MD 미실행 |

각 작업의 ZIP을 실제 내려받고 내부 산출물의 SHA-256을 `manifest.json`과 비교했습니다. GROMACS의 실제 실행은 이 환경에서 검증하지 않았습니다.

## REA

13개 저장소, 60개 핵심 소스·라이선스 파일이 고정 Git blob과 byte 단위로 일치함을 확인했습니다. 실제 REA 소스 인벤토리 13건, 정적 JS/TS 분석 1건, 기능·provider 조회 2건을 실행했습니다. 3Dmol 선택 소스 정적 분석은 127개 그래프 노드, 232개 연결, 파싱 실패 0입니다. 원본 전체에 대한 디컴파일 결과를 주장하지 않습니다.

## 화면과 제한

HTML·CSS·JavaScript·로컬 3Dmol 파일의 HTTP 200, JavaScript 문법, 입력 필드와 API 모델의 일치 및 DOM 참조를 확인했습니다. 연결된 브라우저가 없어 실제 렌더링·화면 클릭·WebGL 시각 검수는 수행하지 못했습니다.

짧은 계산의 성공은 모델 보정·평형화·실험적 타당성 또는 벌크 물성 예측의 검증이 아닙니다. polyply의 검토한 CLI에는 좌표 생성 시드를 지정하는 옵션이 없어 그 경로는 난수 제어 한계를 별도 기록합니다. 다른 플랫폼·GPU·병렬 실행에서 bitwise 재현성을 보장하지 않습니다.
