# Polymer Lab · 개인 고분자 연구 워크벤치

고분자 구조 생성·MD·궤적 분석과 단백질 구조 가져오기/예측 작업을 한 로컬 화면에서 관리합니다. 계산은 별도 프로세스에서 실행하고, 설정·로그·원본 파일·라이브러리 버전·해시를 작업별로 보존합니다. 한국어 화면과 로컬 3Dmol.js 뷰어를 포함합니다.

## 바로 실행

현재 이 컴퓨터에는 프로젝트 전용 `.venv`와 기본 계산 도구가 설치되어 있습니다.

```sh
cd /home/kali/Projects/polymer-workbench
./start.sh
```

브라우저에서 **http://127.0.0.1:8765**를 엽니다. 서버를 종료하려면 터미널에서 `Ctrl+C`를 누릅니다. 이미 실행 중이면 새 서버를 만들지 않고 주소를 열면 됩니다. 포트를 바꾸려면 `POLYMER_PORT=8766 ./start.sh`를 사용합니다.

다른 Linux 환경에 설치할 때는 Python 3.13 기준으로 검증한 `./setup.sh`를 먼저 실행합니다. 다운로드가 필요하며 시스템 Python이나 사용자 Codex 설정을 바꾸지 않습니다. Node 24는 REA 재분석에 필요하고 일상적인 계산 서버에는 필요하지 않습니다.

## 현재 실행 가능한 흐름

| 경로 | 실제 수행하는 작업 | 결과의 의미 |
|---|---|---|
| 조대화 고분자 | 사슬 생성 → LAMMPS Langevin MD → MDAnalysis 교차 확인 → 궤적/그래프 | 환산 단위의 미보정 비드-스프링 모델 |
| 전원자 구조 | 반복단위 SMILES → RDKit 연결·3D 배치·MMFF 최적화 | 준비된 한 사슬 구조, MD 수행 아님 |
| RadonPy MD | RDKit → GAFF2_mod/Gasteiger → LAMMPS 최소화·짧은 NVT | 고립된 한 사슬 계산, 벌크 물성 아님 |
| AlphaFold DB | UniProt ID → 공식 DB 예측 파일 → 원자 구조·pLDDT 분석 | 이미 존재하는 예측 가져오기, 새 추론 아님 |
| 내 PDB 분석 | PDB → 구조 확인·기하 분석 | B-factor는 사용자가 지정할 때만 pLDDT로 해석 |
| PACKMOL | RDKit 사슬 + 선택한 이전 구조 → 분자 배치 | MD용 초기 배치. 공통 힘장/토폴로지 설정이 추가로 필요 |
| polyply | 라이브러리 잔기·길이 → 분자 ITP | 기본 설정은 Martini 3 PEO 토폴로지 준비 |
| pysimm | RDKit → GAFF2/Gasteiger → LAMMPS 에너지 최소화 | 고립 사슬 최소화, 평형 MD 아님 |

조대화 기본 모델은 **코사인 soft 반발 + harmonic 결합**, 비주기 희박 사슬입니다. 비드가 서로 통과할 수 있어 얽힘 동역학을 재현하는 모델로 해석하면 안 됩니다. NumPy 대체 엔진은 동일한 포텐셜의 overdamped Langevin 적분을 수행하지만 LAMMPS 관성 동역학과 시간 척도가 다릅니다. `repeat_smiles`를 입력해도 조대화 힘장이 그 화학종에 맞춰 자동 보정되지는 않습니다.

전원자 기본 예제는 반복단위 `[*]CC[*]`를 머리-꼬리 방향으로 연결하고 수소로 말단을 막습니다. 입체규칙성·분자량 분포·가교·평형화·실험 보정은 자동 추론하지 않습니다. 짧은 검증 실행에서 Tg·밀도·탄성률을 계산했다고 표시하지 않습니다.

## 단백질과 고분자를 이어서 실행

1. 연구 경로에서 **단백질 · AlphaFold / ColabFold**를 선택합니다.
2. **AlphaFold DB에서 기존 예측 가져오기**, 접근 번호 `P69905`로 실행합니다.
3. 구조와 신뢰도를 확인한 뒤 **PACKMOL로 이어가기**를 누릅니다.
4. 고분자 반복단위·사슬 길이·사슬 수를 입력하고 실행합니다.
5. 가져온 구조 1개와 고분자 사슬들이 배치된 `structure.pdb`를 받습니다. 다음 MD 단계에는 양쪽 분자에 일관된 힘장·전하·토폴로지·용매/이온 조건이 필요합니다.

이 연결은 이전 작업 ID와 입력 PDB의 해시를 기록합니다. 조대화 비드를 원자 단위 PDB와 자동 혼합하는 연결은 거부합니다. 두 모델의 단위와 의미가 다르기 때문입니다.

## AlphaFold 계열의 새 구조 예측

화면에는 세 가지 별도 기능이 있습니다.

- **기존 예측 조회:** AlphaFold DB 인터넷 접근만 필요합니다. API가 돌려준 현재 PDB 주소와 원본 응답을 보관합니다.
- **입력 내보내기:** FASTA와 AlphaFold 3 입력 JSON을 만듭니다. 선택한 작은 리간드는 AF3 JSON에만 포함합니다. 내보내기 상태는 `prepared`입니다.
- **로컬 새 예측:** 설치된 `colabfold_batch`를 실행합니다. 이번 환경에는 ColabFold/JAX 모델 실행 환경·가중치·GPU가 준비되지 않아 새 추론은 검증하지 않았습니다. 필요한 파일과 명령을 보관하고 `blocked`로 표시합니다.

ColabFold를 별도 환경에 구성했다면 서버 실행 전에 연결합니다.

```sh
export COLABFOLD_EXEC=/absolute/path/to/colabfold_batch
./start.sh
```

기본 예측 설정은 단일 서열 또는 제공한 A3M입니다. 공개 MSA 서버 옵션을 직접 선택하면 서열이 해당 서버로 전송됩니다. 단일 서열 예측은 MSA가 있는 경우보다 품질이 낮을 수 있습니다. pLDDT는 국소 구조 신뢰도이며 실험적 정확성이나 결합 친화도가 아닙니다. AlphaFold는 합성 고분자의 일반 물성 예측기로 사용하지 않습니다.

공식 설치와 모델 이용 조건은 [ColabFold](https://github.com/sokrypton/ColabFold), [AlphaFold 3](https://github.com/google-deepmind/alphafold3)를 따릅니다. 모델 가중치는 이 프로젝트에 포함하지 않습니다.

## 선택 계산 엔진 연결

모든 경로가 하나의 작업 관리자·파일 저장·로그·상태 API를 사용합니다. 엔진들이 모든 실험에서 순서대로 실행되지는 않습니다.

| 환경 변수 | 역할 |
|---|---|
| `LAMMPS_EXEC` | `lmp` 실행 파일. 기본 `.venv/bin/lmp`를 PATH로 찾음 |
| `PACKMOL_EXEC` | 별도 PACKMOL 실행 파일 |
| `POLYPLY_EXEC` | 별도 polyply 실행 파일 |
| `GROMACS_EXEC` | `gmx` 실행 파일 |
| `RADONPY_PYTHON` | 별도 RadonPy Python 환경 |
| `PYSIMM_PYTHON` | 별도 pysimm Python 환경 |
| `HOOMD_PYTHON` | 별도 HOOMD-blue Python 환경 |
| `COLABFOLD_EXEC` | 별도 ColabFold 실행 파일 |
| `POLYMER_DATA_DIR` | 작업 데이터 저장 폴더 |

HOOMD-blue는 별도 CPU 실행 스크립트를 생성하며 설치된 환경을 연결하면 실행합니다. 기본 LAMMPS 경로와 다른 Gaussian 반발 모델임을 기록합니다. 이번 환경에서 HOOMD-blue는 실행하지 않았습니다.

polyply에서 **완전한 forcefield ITP**를 제공하면 좌표 생성을 이어가고, **MDP**와 설치된 GROMACS가 있으면 `grompp → mdrun`을 실행합니다. 이번 환경에서는 GROMACS 실행과 전체 힘장 검증을 수행하지 않았습니다. 분자 ITP 생성과 실제 GROMACS 계산은 서로 다른 단계로 표시됩니다. 외부 파일을 include하는 힘장 대신 필요한 정의가 포함된 입력이 필요합니다.

## REA 분석 근거

13개 공식 저장소의 커밋을 고정한 60개 핵심 소스·라이선스 파일을 검토했습니다.

- **실제 REA 실행:** 13개 소스 스냅샷 등록·인벤토리·해시 기록.
- **실제 REA 정적 분석:** 3Dmol.js의 선택한 JS/TS 파일에 대한 모듈·import 그래프.
- **직접 소스 검토:** Python/C++/Fortran API와 입출력 경계. 이를 REA 네이티브 디컴파일 결과라고 표현하지 않습니다.

화면의 **REA 분석 근거**에서 원본 출력·파일 해시·고정 커밋 링크를 확인할 수 있습니다. 자세한 범위는 [REA 감사 문서](docs/REA_AUDIT.md), 재실행은 [REA 도구 안내](tools/rea/README.md)를 참고하세요.

```sh
python3 tools/rea/bootstrap.py
python3 scripts/audit_repos.py --refresh-sources --run-rea
```

REA는 개발 분석에 사용합니다. 완료된 연구 작업은 각 계산 엔진의 공개 API/CLI로 실행됩니다. 사용자 Codex의 MCP 설정을 수정하지 않았습니다.

## 원본 보관·재실행

`data/jobs/<작업ID>/`에 설정, `runtime.json`, 로그, 구조, 궤적, `result.json`, `manifest.json`을 보관합니다. 전체 ZIP 다운로드에는 이 파일들이 포함됩니다. `manifest.json`은 산출물 SHA-256을 기록하며 `runtime.json`은 설치 버전과 계산 코드 해시를 기록합니다. 다운로드는 실행 종료 또는 중단 완료 뒤 가능합니다.

`completed`는 해당 경로의 작업이 끝났다는 뜻입니다. `prepared`는 구조·토폴로지·예측 입력의 준비, `blocked`는 필요한 엔진이나 자료 부족을 의미합니다. 어느 상태도 연구 결과의 과학적 타당성을 자동 보증하지 않습니다. 서버 재시작 중 끊긴 작업은 `interrupted`가 되고 설정으로 다시 실행할 수 있습니다. 현재 재실행은 처음부터 다시 계산하며 체크포인트에서 자동 이어달리기는 구현하지 않았습니다.

## CLI와 검증

```sh
export PATH="$PWD/.venv/bin:$PATH"
export LD_LIBRARY_PATH="$PWD/.venv/lib:${LD_LIBRARY_PATH:-}"
.venv/bin/python scripts/engine_worker.py --config examples/cg-polymer.json --output data/cli-example
.venv/bin/pytest -q
```

CLI 직접 실행은 계산 파일을 만들며 HTTP 작업 관리자의 대기열·중단·전체 해시 묶음은 사용하지 않습니다. HTTP API 문서는 서버의 `/docs`에서 확인할 수 있습니다.

테스트는 포텐셜의 힘-에너지 일치, 실제 LAMMPS 비교, MDAnalysis 교차 확인, RDKit 결합 연결, 단백질 신뢰도 해석, API 입력 검증·재실행·경로 제한·실제 프로세스 그룹 중단을 포함합니다. [검증 기록](docs/VALIDATION.md)을 참고하세요.

현재 서버는 개인 PC의 `127.0.0.1` 전용입니다. 다중 사용자 인증·원격 HPC 스케줄러·프로덕션 클러스터 서비스는 포함하지 않습니다.

## 소스 구성

```text
workbench/server.py        로컬 HTTP API
workbench/jobs.py          작업·프로세스·산출물 관리
workbench/models.py        입력 검증
workbench/science.py       조대화 계산·분석·RDKit 구조 생성
workbench/external.py      RadonPy/PACKMOL/단백질/선택 엔진 어댑터
workbench/static/          한국어 화면·3D 뷰어
scripts/engine_worker.py   계산 프로세스 진입점
scripts/audit_repos.py     REA 재분석·근거 검증
evidence/                 고정 소스·라이선스·분석 원본
examples/                 실행 설정 예시
```

제3자 코드·도구의 라이선스는 각 원본을 따릅니다. 3Dmol.js 배포본의 BSD 고지는 `workbench/static/vendor/3Dmol-LICENSE.txt`에 보관했습니다. 다른 연구 도구는 설치된 패키지로 사용하며 전체 코드를 합쳐 복제한 형태가 아닙니다.
