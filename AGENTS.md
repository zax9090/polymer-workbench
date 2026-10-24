# Polymer Lab 작업 안내 (AI 코딩 에이전트용)

이 파일은 Codex·ChatGPT·Claude 같은 에이전트가 이 저장소에서 일을 시작할 때 읽는 안내입니다. 사용자 설명서는 `docs/MANUAL.md` 한 곳에서 유지하고 앱의 `/manual`에서 같은 파일을 보여줍니다. 다른 `docs/` 파일은 개발·검증 근거입니다.

## 프로젝트 한 줄 요약

개인용 고분자·단백질 연구 워크벤치(Linux 데스크톱 + 로컬 웹 UI)입니다. 모든 계산은 "작업(job)"으로 실행되며 입력·로그·버전·해시가 함께 저장됩니다. 상용 프로그램(Materials Studio, Minitab 등)의 기능은 **공개 문헌의 식과 공개 데이터를 근거로 직접 새로 구현**합니다(계산 커널은 Rust 우선, 원칙 7). 상용 파일이나 바이너리는 쓰지 않습니다.

## 저장소 구조

| 위치 | 내용 |
|---|---|
| `workbench/server.py`, `jobs.py`, `models.py`, `preflight.py` | FastAPI 로컬 서버(127.0.0.1 전용), 작업 관리자, JobConfig 검증, 제출 전 화학·크기·엔진 검사(`POST /api/jobs/preflight`, MCP `validate_job`) |
| `workbench/science.py`, `external.py`, `engines.py` | 계산 경로(route) 실행. 외부 엔진(LAMMPS, RadonPy, GROMACS, EMC, xtb 등) 호출 |
| `workbench/pcff.py`, `class2.py`, `crosslink.py` | PCFF 벌크 셀(EMC), msi2lmp 규칙 대조, 실행 전 계수 점검(`coefficient_check.json`), 에폭시–아민 가교(fix bond/react, 계단식 반응 거리·2차 아민 확률) |
| `workbench/glass_transition.py` | `atomistic_mode: tg_scan`. 완료된 벌크·가교 셀을 단계 냉각 NPT로 다시 돌려 비부피 쌍곡선 Tg(프로파일 구간, 두 직선 비교)와 열팽창을 구함. 냉각 속도를 함께 보고하고 실험값으로 환산하지 않음. 검증 `docs/GLASS_TRANSITION.md` |
| `workbench/bridges.py` | Martini 3 단백질–고분자 혼합계(martinize2, polyply, GROMACS). pLDDT 구간 탄성망(`-eunit`), 실험적 단백질–고분자 ε 배율(`martini_lambda.itp`) |
| `workbench/correlations.py`, `native/src/topology.rs` | Synthia 방식(Bicerano) 물성 상관식의 기반. 반복단위를 고리로 닫은 연결성 지수(반복단위당 값)와 주쇄·곁사슬 구분. 상관식 계수는 원 출처를 확인한 뒤 넣음. 검증 `docs/PROPERTY_CORRELATIONS.md` |
| `workbench/amorphous.py` | 자체 비정질 셀 빌더(배위 편향 성장). `cell_builder: "native"` |
| `workbench/blends.py` | Blends 방식 χ(T) (Fan 1992), Rust 커널 사용, 실험적 |
| `workbench/structure_analysis.py` | RDF·MSD·자유 부피·Widom 분석(Rust 커널). 작업 API·MCP·UI 연결, 입력·계산량 검증과 h/2h 격자 비교 |
| `workbench/execution.py`, `native/src/execution.rs`, `compute_worker.py` | 실행 큐(MD·분석·통계 세 줄)와 별도 계산 프로세스. 새 계산은 이 경로로 연결하고 `LANES`에서 줄을 정함. HTTP 요청은 `request_wait`로 대기 시간 제한 |
| `workbench/workflows.py`, `workflow_api.py` | 단계별 DOE→계산(GAFF2 자체 벌크, PCFF 벌크, 에폭시–아민 가교, 그 셀의 Tg 스캔 후속 흐름)→자유 부피·NPT 물성·전환율·Tg→추천(단일 반응 또는 만족도)·확인. 경로별 연결 설정(정수 설정은 반올림 확인), 행마다 여러 반응값, 입력 일치·예산·중복·출처(분석·결과 파일 해시) 검증. 검증 `docs/WORKFLOWS.md` |
| `workbench/sheets.py`, `sheet_api.py`, `static/sheets.js` | 붙여넣어 실행하는 실행표(`# Polymer Lab 실행표 v1`). Blends χ 스크리닝과 작업 묶음, 줄 번호 한국어 오류, 작업은 `preflight.prepare_submission`(작업 API와 같은 검사). 코드로 실행되지 않음. 형식·검증 `docs/RUN_SHEETS.md`, MCP `run_sheet` |
| `workbench/autopilot.py` | 사람이 한 번 승인한 계획·계산 수 안에서 연구 흐름을 끝까지 실행(같은 설정 재시도, 실패 한도, 제외 사유 기록, 확인 계산, 보고서). 설정은 바꾸지 않고 값은 모두 검토 전. 실행 중에는 직접 단계 실행을 막음 |
| `workbench/doe.py`, `native/src/doe.rs`, `native/src/mixture.rs`, `native/src/statistics.rs` | 요인설계·면중심 중심합성설계, 혼합물 설계(심플렉스 격자·중심, 꼭짓점)와 Scheffé 모델(L-유사성분), QR 회귀·ANOVA·잔차/PRESS·t/F 추론·예측구간, 모델 비교·범위 내 조건 탐색, Derringer–Suich 다중 반응 만족도. 검증 `docs/DOE_MIXTURE.md` |
| `workbench/process.py`, `evaluation.py` | 공정 데이터 분석: 데이터 사전, I-MR·X̄-S 관리도와 신호 규칙, Cp·Cpk·Pp·Ppk, 균형 내포 분산 성분, 층별 불량률(Mantel–Haenszel). 모델 평가: 판정 기준별 놓침·오탐·비용, 가상계측 경계 오차, 무작위·로트·시간 분할 비교. 검증 기록 `docs/PROCESS.md` |
| `workbench/drive_backup.py`, `manual.py` | 소스·설명서·연구 결과 ZIP과 검증된 Drive 업로드, 단일 설명서 표시 |
| `workbench/reports.py`, `static/reports.js` | DoE 기록·연구 흐름·마스터 표의 불변 보고서(요약·SVG/PNG 그림·CSV·자체 xlsx·해시)와 파일별 MD5를 확인하는 Drive 폴더 업로드. 숫자와 검토 표시는 옮기기만 함. 검증 `docs/REPORTS.md` |
| `workbench/spectra.py`, `ingestion.py` | IR·UV-vis·탁도, DSC·TGA·GPC·인장 곡선 분석 |
| `workbench/research_store.py`, `research_api.py`, `learning.py`, `campaigns.py` | DuckDB 연구 데이터, 검토 단계, GP 모델, BayBE 캠페인 |
| `workbench/screening.py`, `compatibility.py` | PI1070 대리 모델 예측, Hansen 용해도·블렌드 |
| `workbench/mcp_server.py`, `mcp_http.py` | MCP 서버(stdio, Streamable HTTP). 도구 목록은 `docs/MCP.md` |
| `workbench/static/` | 웹 UI(`index.html`, `app.js`, `research.js`, `screening.js`) |
| `native/` | Rust 크레이트(PyO3, 모듈 `workbench_native`): contacts, trajectory, volume, widom, doe, statistics, topology |
| `scripts/` | 엔진 워커, 데스크톱 빌드, REA 감사, CI GROMACS 설치, Drive 백업 설정 |
| `tests/` | pytest(약 165개 함수). 실제 엔진을 쓰는 테스트는 엔진이 없으면 이유를 남기고 건너뜀 |
| `docs/` | 기능별 문서와 검증 기록. 진행 중 작업은 `docs/RUST_KERNELS.md` |
| `tools/rea/`, `evidence/` | REA로 감사한 외부 도구의 고정 커밋·해시와 감사 기록 |
| `.github/workflows/` | `tests.yml`(PR·main 전체 테스트), `drive-backup.yml`(main 커밋을 Google Drive에 zip 백업) |

## 실행과 테스트

```sh
bash setup.sh                 # .venv 생성, 고정 의존성 설치, cargo가 있으면 Rust 커널 빌드
./start.sh                    # http://127.0.0.1:8765
.venv/bin/python -m pytest -q                       # 전체 테스트(엔진이 있으면 수십 분)
.venv/bin/python -m pytest -q tests/test_blends.py  # 바꾼 부분만 먼저
cargo test --release --locked --no-default-features --manifest-path native/Cargo.toml
.venv/bin/python -m pip install ./native            # Rust 커널을 바꾼 뒤 다시 설치
```

- Python은 3.13에서 검증했습니다(3.12 이상 필요).
- 의존성은 `requirements.lock.txt`와 `native/Cargo.lock`으로 고정되어 있습니다. 버전을 올릴 때는 잠금 파일도 함께 갱신합니다.
- CI 러너(Ubuntu 24.04)에서 LAMMPS의 MPI가 RDMA 장치 때문에 죽지 않도록 `UCX_TLS=self,sm,tcp`를 기본으로 씁니다(`workbench/paths.py`).

## 지켜야 할 원칙

1. **검증 없이 결과를 주장하지 않습니다.** 새 계산은 해석해, 독립 구현(MDAnalysis, LAMMPS 등), 공개 데이터 중 하나와 대조하고, 그 수치를 `docs/`에 남깁니다. 맞지 않으면 숨기지 말고 "실험적"이라고 표시하고 한계를 적습니다(예: `docs/BLENDS.md`).
2. **상태의 뜻을 지킵니다.**
   - `completed`: 경로가 끝까지 실행됐다는 뜻입니다.
   - `prepared`: 입력만 준비됐다는 뜻입니다.
   - `blocked`: 엔진이나 입력이 없다는 뜻입니다.
   - 어느 것도 과학적 타당성을 보증하지 않습니다. 짧은 MD로 평형·Tg·탄성률을 주장하지 않습니다.
3. **측정값은 사람이 검토합니다.** 에이전트·MCP가 넣은 측정값은 항상 `verified=false`입니다. 학습에는 검토된 실험·문헌값만 씁니다. 이 규칙을 우회하지 않습니다.
4. **외부 도구와 데이터는 고정합니다.** 내려받는 엔진과 데이터는 버전·SHA-256을 고정하고, REA 감사 기록(`tools/rea/`, `evidence/`)과 맞춥니다. 저작권이 있는 데이터(예: NIST WebBook 스펙트럼)는 로컬 검증에만 쓰고 커밋하지 않습니다.
5. **비밀 값을 다루지 않습니다.**
   - CI 토큰은 GitHub Secrets에만 둡니다. 쓰는 값은 `RCLONE_DRIVE_TOKEN`, `DRIVE_CLIENT_ID`, `DRIVE_CLIENT_SECRET`입니다. 개인 앱의 Drive 연결은 저장소 밖의 rclone 보안 설정 파일을 사용하며 환경 변수 `POLYMER_DRIVE_REMOTE`에는 원격 이름만 넣습니다.
   - 이 값들을 로그·커밋·문서에 쓰지 않습니다. MCP HTTP 토큰(`<데이터 폴더>/.mcp_http_token`)도 마찬가지입니다.
6. **사용자 데이터를 건드리지 않습니다.** 테스트는 임시 폴더와 합성 데이터만 씁니다.
7. **상용 기능은 직접 새로 구현합니다.**
   - Materials Studio(Synthia, Blends, Amorphous Cell 등)·Minitab의 기능은 공개 논문·교재의 식과 공개 데이터를 근거로 이 저장소에서 직접 만듭니다. 계산 커널은 Rust(`native/`)를 우선합니다.
   - 오픈소스 도구가 없다는 이유로 기능을 빼지 않습니다. 쓸 만한 오픈소스는 버전을 고정해 쓰고(원칙 4), 없으면 문헌을 보고 구현합니다.
   - 상용 소프트웨어의 바이너리·파라미터 파일·매뉴얼 문장·출력 파일은 쓰거나 옮기지 않고, 디컴파일하지 않습니다.
   - 쓴 식의 출처(저자, 문헌, 판, 식·표 번호)를 코드 주석과 `docs/`에 적습니다. 계수를 기억에 의존해 넣지 않습니다. 상용 제품과 같은 결과를 보장한다고 말하지 않으며, 도구·식별자 이름에 상표를 쓰지 않습니다(예: `minitab_*` 대신 `doe_*`).

## 코드 관례

- 주변 코드와 같은 방식으로 씁니다.
  - 짧은 함수, 꼭 필요한 곳에만 주석을 답니다.
  - 주석과 식별자는 영어로 씁니다.
  - 사용자에게 보이는 UI 문구와 API 오류(422 메시지)는 한국어로 씁니다.
- 결과 JSON에는 단위, 방법, 출처, `warnings`(영어 문장)를 함께 넣습니다.
- 커밋 메시지는 영어로 씁니다. 제목 한 줄과 본문에 "무엇을, 왜, 어떻게 확인했는지"를 적습니다.
- 새 기능을 연결하는 순서는 다음과 같습니다.
  1. 계산 모듈
  2. 테스트
  3. API(`research_api.py` 또는 `server.py`)
  4. MCP 도구(`mcp_server.py`, `docs/MCP.md` 표)
  5. UI
  6. 문서
- PyInstaller 배포(`packaging/polymer-lab.spec`)에 새 모듈이 들어가는지 확인합니다.

## 지금 진행 중인 일 (2026-10-24 기준)

**먼저 `docs/HANDOFF.md`를 읽으세요.** 최근 작업 내역, 중간에 멈춘 실행표 작업, 남은 일과 사용자 할 일이 있습니다. 커널 쪽 세부는 `docs/RUST_KERNELS.md`에 있습니다.

1. **자유 부피(`native/src/volume.rs`)**
   - 거리장을 주기 경계 상자 트리로 가속하고, h와 2h의 절대 차이·실제 격자 간격을 보고하도록 연결했습니다.
   - 탐침 점유 부피가 실제 PS 셀에서 아직 수렴하지 않습니다(탐침 1.0 Å: 0.166 → 0.191). 두 격자의 차이를 오차 상한이나 수렴 증명으로 해석하지 않습니다.
2. **`structure_analysis.py` 연결 완료**
   - `POST /api/jobs/{id}/analysis`, MCP `analyze_job_structure`, 완료된 작업의 구조·궤적 분석 화면을 사용합니다. 분석은 원본 작업 파일을 바꾸지 않고 입력·커널·코드 해시를 응답에 담습니다.
   - `tests/test_structure_analysis.py`, `test_structure_analysis_api.py`, `test_native_bindings.py`에 합성 데이터·MDAnalysis 대조·HTTP/MCP·잘못된 입력 검증이 있습니다. 실제 PS/아르곤 셀의 기존 검증 기록과 새 합성 회귀 테스트를 구분합니다.
3. **Widom**
   - 짧게 평형화한 PS 셀의 기체 용해도가 실험보다 약 10배 큽니다.
   - 할 일은 전하를 가진 다점 CO₂ 모델, 여러 셀 평균, 충분히 평형화한 셀로 다시 보는 것입니다.
   - PCFF(9-6 LJ) 셀은 지원하지 않습니다.
4. **Blends χ**
   - 쌍 하나의 볼츠만 평균은 방향족 고리 포개짐과 극성 쌍의 비중을 과대평가합니다. 지방족 용매에서 부호가 틀립니다.
   - 보완 후보는 여러 이웃을 채운 클러스터 몬테카를로입니다.
5. **셀 빌더**: `workbench/amorphous.py`를 Rust로 옮기고 trans/gauche 비틀림 통계를 넣습니다.
6. **공정 데이터 분석(`workbench/process.py`)**
   - statsmodels 대조와 시뮬레이션은 끝났습니다(`docs/PROCESS.md`).
   - 모델 평가(`workbench/evaluation.py`)는 scikit-learn 대조와 로트 누수 합성 실험을 마쳤습니다.
   - NIST Furnace·UCI SECOM 실데이터 대조는 해당 서버에 접속할 수 있는 PC에서 `scripts/validate_process_furnace.py`, `scripts/validate_secom.py`를 실행해야 합니다. 데이터 파일은 커밋하지 않습니다.

## 연결된 환경

- **Google Drive 백업**: main에 커밋이 올라오면 자동으로 됩니다(`docs/DRIVE_BACKUP.md`). rclone 공용 클라이언트는 2026년 중 종료되므로, 본인 OAuth 클라이언트 설정을 권장합니다.
- **원격 MCP**: `python -m workbench --mcp-http`와 터널(cloudflared/Tailscale Funnel)로 claude.ai 커넥터에 연결해 사용 중입니다(`docs/MCP.md`). 주소에 비밀 토큰이 들어 있습니다.
