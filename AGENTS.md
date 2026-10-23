# Polymer Lab 작업 안내 (AI 코딩 에이전트용)

이 파일은 Codex·ChatGPT·Claude 같은 에이전트가 이 저장소에서 일을 시작할 때 읽는 안내입니다. 사용자 설명서는 `docs/MANUAL.md` 한 곳에서 유지하고 앱의 `/manual`에서 같은 파일을 보여줍니다. 다른 `docs/` 파일은 개발·검증 근거입니다.

## 프로젝트 한 줄 요약

개인용 고분자·단백질 연구 워크벤치(Linux 데스크톱 + 로컬 웹 UI)입니다. 모든 계산은 "작업(job)"으로 실행되며 입력·로그·버전·해시가 함께 저장됩니다. 상용 프로그램(Materials Studio 등)의 기능은 **공개 문헌과 오픈소스로 새로 구현**합니다. 상용 파일이나 바이너리는 쓰지 않습니다.

## 저장소 구조

| 위치 | 내용 |
|---|---|
| `workbench/server.py`, `jobs.py`, `models.py` | FastAPI 로컬 서버(127.0.0.1 전용), 작업 관리자, JobConfig 검증 |
| `workbench/science.py`, `external.py`, `engines.py` | 계산 경로(route) 실행. 외부 엔진(LAMMPS, RadonPy, GROMACS, EMC, xtb 등) 호출 |
| `workbench/pcff.py`, `class2.py`, `crosslink.py` | PCFF 벌크 셀(EMC), msi2lmp 규칙 대조, 에폭시–아민 가교(fix bond/react) |
| `workbench/amorphous.py` | 자체 비정질 셀 빌더(배위 편향 성장). `cell_builder: "native"` |
| `workbench/blends.py` | Blends 방식 χ(T) (Fan 1992), Rust 커널 사용, 실험적 |
| `workbench/structure_analysis.py` | RDF·MSD·자유 부피·Widom 분석(Rust 커널). 작업 API·MCP·UI 연결, 입력·계산량 검증과 h/2h 격자 비교 |
| `workbench/execution.py`, `native/src/execution.rs`, `compute_worker.py` | 실행 큐(MD·분석·통계 세 줄)와 별도 계산 프로세스. 새 계산은 이 경로로 연결하고 `LANES`에서 줄을 정함. HTTP 요청은 `request_wait`로 대기 시간 제한 |
| `workbench/workflows.py`, `workflow_api.py` | 단계별 DOE→GAFF2 벌크→자유 부피→추천·확인. 입력 일치·예산·중복·출처 검증 |
| `workbench/doe.py`, `native/src/doe.rs`, `native/src/statistics.rs` | 요인설계·면중심 중심합성설계, QR 회귀·ANOVA·잔차/PRESS·t/F 추론·예측구간, 모델 비교·범위 내 조건 탐색 |
| `workbench/drive_backup.py`, `manual.py` | 소스·설명서·연구 결과 ZIP과 검증된 Drive 업로드, 단일 설명서 표시 |
| `workbench/spectra.py`, `ingestion.py` | IR·UV-vis·탁도, DSC·TGA·GPC·인장 곡선 분석 |
| `workbench/research_store.py`, `research_api.py`, `learning.py`, `campaigns.py` | DuckDB 연구 데이터, 검토 단계, GP 모델, BayBE 캠페인 |
| `workbench/screening.py`, `compatibility.py` | PI1070 대리 모델 예측, Hansen 용해도·블렌드 |
| `workbench/mcp_server.py`, `mcp_http.py` | MCP 서버(stdio, Streamable HTTP). 도구 목록은 `docs/MCP.md` |
| `workbench/static/` | 웹 UI(`index.html`, `app.js`, `research.js`, `screening.js`) |
| `native/` | Rust 크레이트(PyO3, 모듈 `workbench_native`): contacts, trajectory, volume, widom |
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

## 지금 진행 중인 일 (2026-10-23 기준)

`docs/RUST_KERNELS.md`에 자세히 있습니다.

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

## 연결된 환경

- **Google Drive 백업**: main에 커밋이 올라오면 자동으로 됩니다(`docs/DRIVE_BACKUP.md`). rclone 공용 클라이언트는 2026년 중 종료되므로, 본인 OAuth 클라이언트 설정을 권장합니다.
- **원격 MCP**: `python -m workbench --mcp-http`와 터널(cloudflared/Tailscale Funnel)로 claude.ai 커넥터에 연결해 사용 중입니다(`docs/MCP.md`). 주소에 비밀 토큰이 들어 있습니다.
