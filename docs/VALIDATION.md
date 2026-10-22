# 실행 검증 기록

## 0.3 이후: MCP 에이전트 연동

2026-10-22, Claude Code 클라우드 컨테이너. `tests/test_mcp.py` 5개 통과: 실제 `python -m workbench --mcp` 프로세스와 stdio JSON-RPC로 프로토콜 버전 협상, 도구 22개 목록·읽기 전용/파괴 표시, ping, 미지원 메서드·도구·잘못된 인자·JSON 파싱 오류를 확인했습니다. MCP 세션 소유 서버를 띄워 NumPy CG 작업을 제출·완료·색인하고, `submitted_via=mcp:pytest`, 0600 세션 파일과 종료 시 삭제, `verified: true`를 보낸 측정값이 검토 전 상태로 저장되는지 검사합니다. 토큰을 쓰는 데스크톱 세션(BackendSupervisor)에 세션 파일로 붙는 경로, 외부 주소 거부, 한 데이터 폴더의 두 번째 서버 거부도 포함합니다.

별도 가상환경의 공식 MCP Python SDK 2.3.0 클라이언트(`stdio_client` + `ClientSession`)로 초기화(2025-06-18), 도구 목록, 서버 시작, 작업 제출·대기, structured content, 인자 오류의 `isError` 결과를 확인했습니다. 이번 컨테이너에는 BayBE 0.15.0을 PyPI torch 2.14.1로 설치해 건너뛰던 BayBE 테스트 2개를 실행했고 통과했습니다. CPU 전용 휠 색인은 이 환경의 프록시에서 차단되었습니다. 혼합계 캠페인 후보(PEO·PS·PMMA × 4길이, 원-핫 블록 벡터)에서 BayBE FPS 초기 5개 뒤 BoTorch 추천이 정상 동작했습니다. 이 마지막 확인은 합성 목표값을 쓴 상태 검사입니다. 이 구성에서 전체 소스 테스트는 **75개 통과, 건너뜀 없음**입니다.

## 0.3 이후: Martini 3 용매화 단백질–고분자 MD

2026-10-22, Claude Code 클라우드 컨테이너(Ubuntu 24.04, Python 3.13.16, GROMACS 2023.3 `apt` 패키지, martinize2/polyply는 `requirements.lock.txt`). Martini 3.0.0 힘장은 고정 커밋 `784591eb`에서 받아 SHA-256 4개를 확인했습니다.

- **실제 실행:** RDKit으로 만든 6잔기 알라닌 소프트웨어 테스트 구조(명시적 `CCCCCC` 가정) + PEO 12-mer 2사슬, 6 nm 박스. W 1,732개, Na⁺/Cl⁻ 각 19개(추정 0.152 M), 계 전하 0. 최소화는 Fmax < 100에 수렴했고 NVT·NPT·생산 단계 모두 `grompp` 경고 0개로 통과했습니다(NVT·NPT에는 위치 구속과 COM 제거에 관한 GROMACS NOTE 1개). 생산 20 ps 평균 밀도 1005 kg/m³, 온도 303 K, 전체 17초. 101 프레임에서 RMSD·회전반경·접촉 수·최소 거리를 계산했습니다.
- **자동 테스트:** 위 경로의 축소판(5.5 nm 박스), API → 작업 프로세스 → 결과 → 연구 DB 색인(`contact_count`, `protein_rmsd` 피처, Martini 3.0.0 호환 조건), 전하 합산과 중화 이온 개수, 고분자 전하 확인 요구, 박스 여유 부족·예상 입자 수 초과·AMBER simulate 거부, 수정된 로컬 힘장 파일 거부, 혼합계 캠페인 후보의 원-핫 블록 벡터와 단백질 입력 고정을 검사합니다. 전체 소스 테스트 **68개 통과, 2개 건너뜀**(BayBE 미설치)입니다. GROMACS·martinize2·polyply가 없으면 실제 실행 테스트 2개는 건너뜁니다.
- **화면:** headless Chromium 1360px에서 혼합계 화면 MD 실행 → 작업 완료 → 결과 지표와 최종 프레임(단백질 파랑·고분자 주황) 표시, 워크플로 단계 표기, 혼합계 설정으로 PEO·PS·PMMA × 2길이 캠페인 후보 6개 생성·저장. 390px 가로 넘침 0px, console 오류 0개.
- **REA:** `scripts/audit_extensions.py --fetch-pinned --run-rea --only martini-forcefields`로 Martini 저장소 원본 5개를 고정 커밋에서 받고 REA 6.7.0 `import-reference-source`를 실행했습니다. 기존 8개 저장소의 고정 원본도 다시 받아 바이트 단위로 확인했으며, 9개 저장소·31개 파일이 모두 `verified_inventory`입니다.

이 검증은 소프트웨어 경로가 끝까지 실행되고 단위·전하·개수가 일관됨을 확인한 것입니다. 20 ps 파일럿의 접촉 수·RMSD는 실제 단백질–고분자 결합이나 평형 도달의 근거가 아닙니다. 데스크톱 번들은 다시 빌드하지 않았습니다.

## 0.3 이후: TGA·GPC 곡선 분석

2026-10-22, Claude Code 클라우드 컨테이너(Linux x86_64, Python 3.13.16, `requirements.lock.txt`). 소스 자동 테스트 **62개 통과, 2개 건너뜀**. 건너뛴 2개는 BayBE 테스트로, BayBE·PyTorch가 `requirements.desktop.lock.txt`에만 있어 이 환경에 설치하지 않았기 때문입니다. 0.3 기록의 59개와 비교하면 이번 변경의 테스트 5개가 추가되었습니다.

- **GPC:** 선형 보정식 `log10 M = 8 − 0.3t`와 기울어진 기준선 위의 가우스 피크(로그정규 질량 분포)에서 Mn·Mw·Đ가 해석해와 상대오차 10⁻⁵ 이내로 일치. 보정 범위 밖 외삽, 용출 순서와 맞지 않는 표준, 차수 대비 부족한 표준, 표준 물질·검출기 누락, 적분 구간과 겹치는 기준선, 음수 면적 2% 초과를 거부. 잘린 분포 경고. 초안 저장 시 `analysis` 보존(API).
- **TGA:** 1 K 간격 시그모이드 감소 곡선에서 5% 감소 온도가 해석해와 0.02 K 이내로 일치. 수분 단계 전 기준 온도를 고르면 다른 값이 나오는 것을 확인했고, 감소율 미도달·분위기·가열 속도·질량 단위 누락·범위 밖 기준 온도·음수 질량을 거부.
- **단위:** Mn kg/mol 변환, Td °C→K, Đ < 1 거부.
- **화면:** headless Chromium(1360px, 390px)에서 GPC 미리보기(값 없음) → 보정 입력 → Mn·Mw·Đ 초안 3개 → 분산도 초안을 양식에 넣고 `analysis` 포함 확인, TGA Td 초안. 가로 넘침 0px, JavaScript console 오류 0개. QtWebEngine 데스크톱 창과 배포 번들은 다시 빌드·검증하지 않았습니다.

검증 자료는 해석해를 아는 인공 곡선입니다. 실제 기기 자료에 대한 정확도 검증이 아닙니다.

## 0.3 연구 기능 검증

2026-10-22, Linux x86_64 / Python 3.13.12. 최종 소스 자동 테스트 **59개 통과**, 실패·건너뜀 없음(39.24초). `evidence/source-tests-v0.3.xml`에 결과를 보관했습니다. 설치된 네이티브 LAMMPS 라이브러리를 직접 사용하는 테스트에도 `.venv/lib` 로더 경로를 명시했습니다.

```sh
env LD_LIBRARY_PATH="$PWD/.venv/lib" .venv/bin/python -m pytest -q --junitxml=evidence/source-tests-v0.3.xml
```

기존 계산 경로에 더해 DuckDB 원자적 저장/백업, 재료 참조, 단위 변환, CSV 검토, DSC/인장 초안, PDF, 토폴로지별 피처, 정의되지 않은 이면각, 실제 MD 여부, 학습 그룹 누출 방지, 단백질 문맥, BayBE 초기/관측 후 추천(이미 측정한 후보를 다시 추천하지 않음), 유한 자동 큐와 재시작, 실제 martinize2/polyply 구성요소 생성을 검증했습니다. 테스트 자료는 별도 임시 폴더의 합성 자료이며 실제 예측 정확도의 근거가 아닙니다. upstream API 폐기 예정 및 초기 FPS가 목적값을 사용하지 않는다는 경고는 실패와 구분합니다.

QtWebEngine/Xvfb의 실제 화면에서 **14개 UI 검사 통과**, JavaScript console 오류 0개입니다. 1360px/760px 화면과 검증 범위를 `evidence/ui-v0.3/report.json` 및 같은 폴더의 PNG 6개에 기록했습니다. 이는 격리된 소스 앱 화면 검증입니다.

REA 확장 감사는 **8개 저장소·26개 고정 원본 파일**에서 정상 실행되었고, 원본 해시가 유지되었습니다. `evidence/extensions/audit.json`에 API/CLI 위치·커밋·명령·종료 코드를 보관합니다. REA의 source inventory는 전체 코드의 역컴파일 또는 과학적 정확성 검증을 뜻하지 않습니다.

## 데스크톱 배포본 0.3.0

배포 실행 파일 SHA-256 `84986949ef0cec06…`(전체 값은 각 근거 파일)에 대해 개발용 가상환경·PYTHONPATH·외부 엔진 환경 변수를 제외하고 `PATH=/usr/bin:/bin`과 임시 연구 데이터 폴더에서 검증했습니다. 근거는 `evidence/desktop-v0.3/`에 있으며 배포본의 `_internal/evidence/desktop-v0.3/`에도 같은 파일이 들어 있습니다.

```sh
python3 scripts/verify_desktop.py --bundle dist/PolymerLab \
  --with-alphafold-db --report evidence/desktop-v0.3/runtime-validation.json
python3 scripts/verify_research_desktop.py --bundle dist/PolymerLab \
  --report evidence/desktop-v0.3/research-validation.json
```

- **기존 계산 경로 10개 통과:** NumPy, LAMMPS, RDKit, RadonPy MD, RDKit→PACKMOL, polyply ITP, pysimm, AF3 입력 내보내기, AlphaFold DB P69905, AlphaFold 구조→PACKMOL. 결과 ZIP CRC·SHA-256, 토큰 없는 요청 거부, 버전 0.3.0, 재시작 후 작업 10개 보존을 확인했습니다.
- **연구 기능 8개 통과:** 번들 BayBE의 유한 자동 캠페인(예산 6, 초기 FPS → 관측 5개 이후 BoTorch 추천), 작업 6개의 자동 피처·단위·PyMBAR 평형 후보 구간, 재료 10종 GP 학습·예측(미보정 불확실성, 정확도 미검증 표시), 인장 곡선 초안(2000 MPa, 검토 필요), PDF 텍스트, 실제 martinize2/polyply 구성요소(MD 미실행 표시), Parquet 스냅샷 해시, 재시작 후 측정값·모델·캠페인 보존. 측정값은 모두 소프트웨어 검증용 인공 자료입니다.
- **경로 이동:** 공백이 있는 폴더로 앱과 데이터를 옮긴 뒤 RadonPy·pysimm 계산과 martinize2/polyply 브릿지가 완료되었고 정상 종료를 기록했습니다(`relocation-validation.json`).
- **네이티브 라이브러리:** Qt WebEngine·LAMMPS·PACKMOL·RDKit·MDAnalysis·DuckDB·PyTorch CPU 등 14개 파일을 번들 경로만으로 `ldd` 확인, 누락 0개, CUDA 라이브러리 미포함(`native-library-validation.json`).
- **배포 파일:** `PolymerLab-0.3.0-linux-x86_64.tar.gz`와 `polymer-lab_0.3.0_amd64.deb`의 필수 파일 45개, 소스·정적 파일·실행기·근거 파일 35개 해시와 `SHA256SUMS`가 일치했습니다. 각각 약 759 MiB, 풀었을 때 약 2.3 GB입니다.

이 검증 중 0.3 소스의 결함 하나를 발견해 수정했습니다. BayBE `Campaign`은 기본적으로 이미 측정한 후보도 다시 추천할 수 있어서, 관측값 5개 이후 BoTorch 단계에서 실행할 수 없는 후보를 돌려주고 캠페인이 일시 정지했습니다. 측정한 후보를 추천 대상에서 제외하도록 고쳤고 같은 상황을 회귀 테스트로 추가했습니다.

Qt 창의 실제 화면 상호작용, 새 AlphaFold/ColabFold 추론, GROMACS·HOOMD-blue 실행, 용매화 혼합계 MD, 클라우드 업로드, 실제 실험 자료에 대한 예측 정확도는 이 배포 검증에 포함되지 않습니다. 화면 검증은 위의 소스 앱 QtWebEngine 기록을 참고하세요.

## 0.2 기존 기반 검증 기록

2026-10-22, Linux x86_64 / Python 3.13.12에서 검증했습니다. 모든 계산은 이번 프로젝트의 전용 환경에서 실행했습니다.

## 자동 테스트

```sh
PATH="$PWD/.venv/bin:$PATH" LD_LIBRARY_PATH="$PWD/.venv/lib:${LD_LIBRARY_PATH:-}" .venv/bin/pytest -q
```

**30 passed**, 19.28초. 테스트 클라이언트의 httpx 호환성에 관한 upstream deprecation 경고 1개가 있으며 실패는 없습니다.

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
- 독립 데스크톱 서비스의 임의 포트, 세션 토큰, 서비스 종료·재시작 후 계산 결과 보존.
- 데스크톱 서비스 종료 시 실행 중 계산 취소와 manifest 생성.
- 독립 `--server` 프로세스에 SIGTERM을 보내면 정상 종료하고 임시 준비 파일을 제거.
- 배포 리소스와 쓰기 가능한 데이터 경로 분리, 기존 완료 작업의 원본을 보존한 복사.
- 손상된 메타데이터·심볼릭 링크·중복 작업을 건너뛰는 데이터 이전.
- 공백을 포함한 배포 경로의 명령 생성, 별도 엔진 환경의 라이브러리 경로 복원.

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

## 데스크톱 배포본 0.2.0

개발용 가상환경·PYTHONPATH·외부 엔진 환경 변수를 제외하고, 임시 연구 데이터 폴더와 `PATH=/usr/bin:/bin`에서 실제 배포 실행 파일을 검증했습니다.

```sh
python3 scripts/verify_desktop.py --bundle dist/PolymerLab \
  --with-alphafold-db --report evidence/desktop-runtime-validation.json
```

**10개 경로 모두 통과**했습니다: NumPy, LAMMPS, RDKit 구조 생성, RadonPy MD, RDKit→PACKMOL, polyply ITP, pysimm 최소화, AF3 입력 내보내기, AlphaFold DB P69905 조회, AlphaFold 구조→PACKMOL. 새 AlphaFold 추론은 수행하지 않았습니다. 네트워크 없이 확인하려면 `--with-alphafold-db`를 생략해 기본 8개 경로를 실행합니다.

- 모든 결과 ZIP의 CRC·산출물 SHA-256·파일 크기 일치.
- 세션 토큰 없는 요청 거부, 정적 파일, 버전 0.2.0, SIGTERM 정상 종료와 임시 파일 정리.
- 서버 재시작 후 작업 10개의 상태와 결과 보존.
- 공백이 있는 폴더로 앱과 데이터를 옮긴 뒤 RadonPy·pysimm 계산 완료.
- Qt WebEngine·LAMMPS·PACKMOL 등 네이티브 파일 10개의 공유 라이브러리 누락 없음.
- 빌드에 기록한 계산·앱 소스 9개 파일의 SHA-256 일치.

실제 근거는 `evidence/desktop-runtime-validation.json`, `evidence/desktop-relocation-validation.json`, `evidence/desktop-native-library-validation.json`입니다. 설치·기존 기록 복사와 앱 시작 기록은 `evidence/desktop-installation.json`에 있습니다. 배포본은 현재 Kali/glibc 2.43 환경에서 검증했으며, 다른 운영체제·이전 Linux의 호환성을 확인하지 않았습니다.

## 화면과 제한

HTML·CSS·JavaScript·로컬 3Dmol 파일의 HTTP 200, JavaScript 문법, 입력 필드와 API 모델의 일치 및 DOM 참조를 확인했습니다. 데스크톱 앱의 Qt 렌더러와 로컬 계산 서버 시작도 확인했습니다. 연결된 검수 도구가 없어 화면 클릭·레이아웃·WebGL의 시각 검수는 수행하지 못했습니다.

짧은 계산의 성공은 모델 보정·평형화·실험적 타당성 또는 벌크 물성 예측의 검증이 아닙니다. polyply의 검토한 CLI에는 좌표 생성 시드를 지정하는 옵션이 없어 그 경로는 난수 제어 한계를 별도 기록합니다. 다른 플랫폼·GPU·병렬 실행에서 bitwise 재현성을 보장하지 않습니다.
