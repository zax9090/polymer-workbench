# 작업 인계 기록 (AI 에이전트용)

이 문서는 다음에 이 저장소에서 일을 이어받는 에이전트(Codex·ChatGPT·Claude 등)가 먼저 읽도록 남긴 작업 기록입니다. 원칙과 구조는 `AGENTS.md`, 사용 안내는 `docs/MANUAL.md`가 기준입니다. 이 문서는 "무엇을 했고, 어디서 멈췄고, 무엇이 남았는지"만 적습니다.

- 기준 날짜: 2026-10-24
- 마지막 브랜치: `claude/busy-rubin-4msaap`. PR #12 머지(`bd51435`) 이후 커밋을 한 PR로 main에 머지했습니다.
- 사용자 요청: "지금까지 작업을 정리해 AI가 읽을 수 있게 저장하고, 중간에 멈추고 머지"

## 1. 이번 묶음에서 끝낸 일 (커밋 순서)

| 커밋 | 내용 | 검증 문서 |
|---|---|---|
| `7390dc0` | AGENTS.md 원칙 7(상용 기능은 공개 문헌으로 직접 구현, 계수는 원 출처 확인) 명시 | — |
| `8e35749` | Synthia 방식 연결성 지수 Rust 커널(`native/src/topology.rs`, `workbench/correlations.py`). 반복단위를 고리로 닫아 반복단위당 값, 주쇄·곁사슬 구분 | `docs/PROPERTY_CORRELATIONS.md` |
| `ef1a858`, `81d291b` | 혼합물 설계(심플렉스 격자·중심, 꼭짓점), Scheffé 모델(L-유사성분), Derringer–Suich 만족도. API·MCP·DOE 화면 | `docs/DOE_MIXTURE.md` |
| `a5875dc`, `e9479ff` | DoE 그림: \|t\| 파레토(임계 t선), 반응표면 지도, 인자별 예측 프로파일. 단색 순차 색 척도(`window.doeColorScale`) | — |
| `85503c7` | 연구 보고서(`workbench/reports.py`): 요약·SVG/PNG 그림·CSV·자체 xlsx·해시 매니페스트, Drive 폴더 업로드 후 파일별 MD5 확인. MCP `create_research_report` | `docs/REPORTS.md` (openpyxl 대조 1,881셀 일치) |
| `24608e8` | 승인한 계획·예산 안에서 연구 흐름을 끝까지 실행(`workbench/autopilot.py`). 같은 설정 재시도, 실패 한도, 제외 사유, 확인 계산, 보고서. MCP `run_research_workflow_plan` | `docs/WORKFLOWS.md` |
| `323f8d9` | PS 시험 셀 200 ps/온도 Tg 스캔 기록 | `docs/GLASS_TRANSITION.md` |
| `730a92e` | 연구 흐름 행마다 여러 반응값 수집, 단일 반응 또는 만족도 최적화(`optimize_desirability`) | `docs/WORKFLOWS.md` (D 0.5807508 vs 격자 0.5807507) |
| `60d67c2` | 600 K 시작 Tg 스캔(컨테이너 재시작으로 중단) 13개 온도 구간 분석 기록: Tg 551 K, 구간 357–603 K | `docs/GLASS_TRANSITION.md` |
| `955a2aa` | 연구 흐름에 PCFF 벌크·에폭시–아민 가교·Tg 후속 흐름 연결. 경로별 설정 범위, 정수 설정 반올림 재예측, 셀→스캔 연결 확인 계산 | `docs/WORKFLOWS.md` (전체 537 통과, 1 건너뜀) |
| (이번 커밋) | 실행표(붙여넣어 실행) — 아래 2절. 중간 정지 상태로 커밋 | `docs/RUN_SHEETS.md` |

## 2. 이전 인계 때 멈춘 일: 실행표(paste-to-run sheets)

사용자 요청: "형식을 정해서 만들고, 복붙할 것을 만들어 복붙해서 내 컴퓨터로 돌리게."

**된 것**
- 형식 v1 정의와 해석기: `workbench/sheets.py`. 종류는 Blends χ 스크리닝과 작업 묶음입니다. 줄 번호가 붙은 한국어 오류를 모두 한 번에 모읍니다.
- 실행
  - `SheetManager`로 Blends 조합을 분석 줄에서 하나씩 돌립니다.
  - 작업은 미리 정한 작업 ID로 만들어, 이어서 제출해도 중복되지 않습니다.
  - 취소, 재시작 후 중단 표시, 남은 것만 이어서 실행을 지원합니다.
- 작업 제출 검사 공유
  - `POST /api/jobs`의 검사 코드를 `preflight.prepare_submission(manager, cfg)`로 옮겼습니다.
  - 서버와 실행표가 같은 함수를 씁니다. 서버의 응답 코드와 메시지는 같습니다.
- `blends.capped(smiles, cap)`: 3D 생성 없이 성분을 검사합니다. `prepare`는 이 함수를 먼저 씁니다.
- API(`workbench/sheet_api.py`, `/api/research/sheets...`), 서버 수명주기 연결(`app.state.sheets`, 종료 순서상 execution보다 먼저 close).
- MCP `run_sheet` (preview·run·status·list·cancel·resume·templates).
- 화면: 내비 "붙여넣어 실행"(`data-view="sheets"`), `workbench/static/sheets.js`.
  - 예제 버튼, 검사, 오류 줄 선택, 실행 확인, 2초 간격 상태 갱신, 순위 표, CSV, 기록을 갖췄습니다.
- 테스트 `tests/test_sheets.py` 7개 통과. 관련 묶음(API·Tg·Blends·MCP) 34개 통과.
- PyInstaller `hiddenimports`에 `workbench.sheets`, `workbench.sheet_api` 추가.
- 문서: `docs/RUN_SHEETS.md`, `docs/MANUAL.md` "붙여넣어 실행", `docs/MCP.md` 두 표.

**이전 인계 시점의 미확인 사항 — 후속 결과는 7절 참고**
- **이전 화면 스모크 테스트 실패(7절에서 해결)**: 헤드리스 Chromium에서 `/#sheets`로 열었을 때 예제 버튼(`[data-sheet-template]`)이 30초 안에 나타나지 않았습니다. 원인은 조사하지 못했습니다. 후보는 해시로 처음 열 때 `polymer:view` 이벤트가 `sheets.js` 리스너 등록 전에 발생하는 순서 문제입니다(`app.js` 201행 `showView(hash)`와 defer 스크립트 순서). API·MCP는 테스트로 동작을 확인했습니다.
- 화면을 실제 브라우저에서 끝까지 눌러 보지 않았습니다.
  - 확인한 것은 JS 문법 검사와 정적 연결뿐입니다.
  - 다음 에이전트는 `./start.sh` 후 `#sheets`에서 예제 "PS 용매 스크리닝"을 검사하고 실행해 보세요. 순위 표와 CSV가 나오는지 확인합니다.
- 작업 실행표로 실제 LAMMPS 작업을 돌린 적이 없습니다. 사용자 PC에서 `jobs_bulk` 예제를 돌려 확인할 차례입니다.
- 이번 변경 뒤 전체 테스트(`pytest -q`)를 끝까지 다시 돌리지 않았습니다.
  - 돌린 것은 관련 묶음뿐입니다(위 34 + 7).
  - CI(`tests.yml`)가 머지 전후로 전체를 돌립니다.
- 후속 후보는 다음과 같습니다.
  - DoE 설계·연구 흐름 실행표 종류
  - 작업 실행표 결과 요약 표(완료 작업의 밀도 등 모음)
  - 예제를 텍스트 파일로 내려받기(7절에서 구현)

**사용자에게 준 붙여넣기 예제**: `sheets.TEMPLATES`와 같습니다. 앱의 예제 버튼, `GET /api/research/sheets/templates`, MCP `run_sheet action=templates`로 볼 수 있습니다.

## 3. 해결되지 않은 과학·검증 문제

1. **Tg(PS 1,296원자 셀)**: 세 번의 스캔 모두 전이를 충분히 분해하지 못했습니다(`docs/GLASS_TRANSITION.md`). 큰 셀과 복제 계산이 필요합니다. 기본 온도 범위는 바꾸지 않았고 사용자 결정을 기다립니다.
2. **Bicerano 상관식 계수**: 원 출처를 확인해야 넣을 수 있습니다(원칙 7). 이 컨테이너의 네트워크에서는 확인하지 못했습니다. 커널(연결성 지수)만 있습니다.
3. **공정 분석 실데이터 대조**: 사용자 PC에서 NIST Furnace·UCI SECOM 대조를 해야 합니다(`scripts/validate_process_furnace.py`, `scripts/validate_secom.py`). 데이터는 커밋하지 않습니다.
4. **자유 부피·Widom·Blends χ**: `AGENTS.md` "지금 진행 중인 일"과 `docs/RUST_KERNELS.md`의 한계가 그대로입니다.
5. **자동 실행(autopilot) 실제 엔진 확인**: 9절에서 GAFF2 소형 셀의 짧은 목표 계획을 실제 엔진으로 완료했습니다. PCFF·가교·Tg 계획 전체의 실제 엔진 확인과 물성 검증은 남아 있습니다.

## 4. 사용자가 해야 할 일

- 이 머지 뒤 PC에서 `git pull`, 그리고 `bash setup.sh`를 실행합니다. Rust 커널이 바뀐 커밋이 있으므로 `pip install ./native`도 함께 합니다.
- Drive 백업에 본인 OAuth 클라이언트를 설정합니다(`docs/DRIVE_BACKUP.md`). rclone 공용 클라이언트는 2026년 중 종료됩니다.
- Tg 기본 범위와 셀 크기를 결정합니다.
- Bicerano 원문(판·표 번호)을 제공하거나 확인합니다.
- 원격 MCP 커넥터(claude.ai `Polymer_Lab`)가 2026-10-24 세션 시작 때 502로 실패했습니다. PC의 서버와 터널 상태를 확인하세요.

## 5. 작업 방식에서 배운 것 (다음 에이전트 주의)

- **LAMMPS와 테스트 동시 실행 금지**: OpenMP 과다 구독으로 둘 다 느려집니다. 꼭 필요하면 스캔 프로세스 그룹을 `kill -STOP`/`-CONT`로 멈춥니다.
- **컨테이너 재시작**: 클라우드 컨테이너는 재시작되면 긴 MD가 죽습니다. 몇 시간 걸리는 계산은 사용자 PC에서 돌리고, 부분 결과라도 바로 기록합니다.
- **백그라운드 대기**: 대기 한도는 2시간입니다. 더 긴 대기는 예약 메시지를 씁니다.
- **프로세스 종료**: `pgrep -f` 패턴이 대기 명령 자신과도 맞을 수 있습니다. PID로 정확히 골라 종료하고, `pkill -f`는 쓰지 않습니다.
- **보고서 그림**: matplotlib `rc_context`가 그림 생성 전체를 감싸야 한글 글꼴이 적용됩니다.
- **CSV 해시**: 줄바꿈 변환 전 원본 바이트로 계산합니다.
- **커밋**: 메시지는 영어로, 제목과 "무엇을, 왜, 어떻게 확인했는지"를 적습니다. 저장소 산출물에 모델 이름을 쓰지 않습니다. 비밀 값(Drive 토큰, MCP HTTP 토큰, 커넥터 주소)은 어디에도 쓰지 않습니다.

## 6. 확인 명령

```sh
.venv/bin/python -m pytest -q tests/test_sheets.py tests/test_api.py tests/test_blends.py
.venv/bin/python -m pytest -q          # 전체(엔진이 있으면 수십 분)
cargo test --release --locked --no-default-features --manifest-path native/Cargo.toml
```

## 7. 인계 후 실행표 연결 수정 (2026-10-24)

기준은 main `1f9aabe`입니다. 사용자 요청에 따라 중단된 실행표 화면을 이어서 수정했습니다.

- **직접 주소로 열 때 예제가 없던 원인:** `app.js`가 해시를 처리하며 `polymer:view`를 동기적으로 보낸 뒤 `sheets.js`가 등록됩니다. 기존 화면 이동 때만 초기화하던 코드를 현재 화면이 `sheets`인 경우에도 호출하게 바꿨습니다. 템플릿 요청은 동시에 중복하지 않으며 실패하면 다시 진입할 때 재시도합니다.
- **입력·중복 제출:** 검사·제출 동안 입력과 버튼을 잠그고, 확인한 원문을 제출할 때까지 유지합니다. 실행 확인을 거절하면 기록을 만들지 않습니다. `실행표 TXT 저장`으로 수정한 예제도 PC 사이에 옮길 수 있습니다.
- **CSV와 완료 상태:** CSV를 먼저 저장한 뒤 완료 상태를 게시합니다. 완료를 보고 폴링을 멈춘 화면에 다운로드 버튼이 빠지는 순서를 막았습니다. 저장 오류는 조합별 결과를 보존하고 한국어 오류로 남깁니다.
- **취소·재개:** 작업 제출 중 취소하면 뒤의 행을 등록하지 않습니다. 취소와 겹쳐 등록된 작업도 등록 직후 취소하며, 중단 상태를 `submitted`로 덮어쓰지 않습니다. 제출 정리가 끝나기 전에는 재개를 막고 재개할 때 정지 이벤트를 새로 만듭니다. 종료된 관리자는 생성·재개 요청을 거부합니다.
- **전체 백업:** 이전 선택 목록에서 빠졌던 `research/sheets`의 원문·기록·조합 결과·CSV를 포함합니다. 활성 실행표와 참조 작업이 아직 끝나지 않은 묶음은 제외하며, 복사 중 재개로 기록이 바뀌면 백업을 중단합니다. Drive 계정 연결·업로드 성공을 이 로컬 수정으로 주장하지 않습니다.

### 실행한 확인

- 관련 Python 테스트 **48개 통과**: 실행표, 백업, 작업 API, 제출 전 검사, MCP, 설명서. 기존 Starlette 경고 1개가 있었습니다. 전체 테스트는 이번 수정 뒤 다시 실행하지 않았습니다.
- Chromium에서 기본 **PS 용매 5종** 예제를 변경 없이 실행했습니다(표본 200000, 클러스터 2000, 시드 1). 실제 Rust 계산 → 5개 완료 → 순위표와 같은 순서의 CSV 다운로드를 확인했습니다. 직접 `#sheets`로 열기, 예제 검사만 하기, 확인 거절, 오류 줄 선택, 이중 제출 1건 처리, TXT 원문 일치, 저장 원문 다시 불러오기, 기록 재조회, 390 px 화면 가로 넘침 없음, 메뉴 재진입, JavaScript 오류 없음도 확인했습니다.
- 실제 작업 실행표도 임시 데이터 폴더에서 GAFF2 자체 벌크 2건을 끝까지 실행했습니다(PE, 사슬 2개 × 반복단위 18개, 약 220원자, 300/320 K, 시드 41/42, `protocol_scale=0.001`, Gasteiger). 작업별 온도·시드·실행표 출처가 보존되고 모두 `completed`인 것을 확인했습니다. 테스트 중 MD 프로세스 그룹을 일시 정지하고 이후 재개했습니다. 두 계산은 짧은 연동 확인이며 RadonPy 평형 수렴 조건을 만족하지 않아 평형 밀도·Tg 검증으로 쓰지 않습니다.
- 임시 연구 폴더의 실제 로컬 전체 백업도 생성했습니다. 실행표 2개·종료된 작업 2개·총 441개 파일을 포함하고, 실행표 원문·기록·CSV 및 매니페스트의 모든 SHA-256 일치를 확인했습니다. 측정값은 0개이며 인터넷 업로드는 수행하지 않았습니다.
- 브라우저 재현 스크립트: `python scripts/check_sheets_ui.py --artifacts /tmp/polymer-sheets-browser`. 실행하는 Python에 Playwright와 Chromium이 필요하며, 앱은 기본 `.venv/bin/python`으로 임시 연구 폴더에서 띄웁니다. 실제 사용자 데이터와 Drive를 사용하지 않습니다.

### 남은 범위

- **자동 연구 흐름의 실제 엔진 전체 실행:** 이 절의 실행표와 별도입니다. 후속 9절에서 GAFF2 단일 반응의 짧은 자동 연결을 실제 엔진으로 확인했습니다. 여러 반응 및 다른 경로의 실제 엔진 전체 확인은 별도입니다.
- **Tg:** 기존 작은 PS 셀의 전이를 충분히 분해하지 못한 한계는 유지됩니다. 큰 셀·독립 반복과 장시간 계산 계획이 필요합니다.
- **Bicerano:** 이번에는 상관식 계수 원 출처를 추가 확인하지 않았습니다. 연결성 지수만 구현되어 있고, 원문 식·표가 확인되지 않은 계수는 넣지 않았습니다.
- 공정 데이터 실데이터 대조와 자유 부피·Widom·Blends χ의 알려진 과학적 한계는 3절과 관련 문서를 따릅니다. 브라우저 실행 성공과 짧은 MD는 물성 검증을 뜻하지 않습니다.

## 8. 다음 수직 경로와 완료 기준 (목표 중심 연구, 2026-10-24)

사용자가 전달한 큰 그림의 우선순위는 **목표 물성·제약·단위·계산 수 → 적절한 설계 → 가능한 저비용 점검 → 선택된 계산 → 제한된 재시도 → 지표·그림 → 재현 자료 보관**입니다. 첨부에 나온 속도·탈락률·Tg·탄성률 숫자는 예시이며 요구 성능이나 계산 결과로 쓰지 않습니다.

다음 작은 구현 범위는 기존 GAFF2 벌크 연구 흐름에 목표·단위·독립 변수 범위·설계 방법·예산을 명시하는 계약과 재현 자료를 연결하는 것입니다. DOE·만족도·자동 실행·보고서 엔진을 중복 구현하지 않습니다. 혼합비 설계와 독립 변수 설계를 구분하며, 해당 경로에서 계산할 수 없는 Tg·상관식이나 저비용 예측을 가능한 것처럼 표시하지 않습니다.

완료 기준:

1. 단위와 변수 연결을 검사한 계획을 미리 볼 수 있고, 계획 준비만으로 MD를 시작하지 않습니다.
2. 기존 실행·재시도·예산 한도를 재사용하고, 취소·엔진 부족·예산 초과를 기록합니다.
3. 짧은 실제 엔진 확인은 작은 셀·고정된 수의 계산으로 제한하며, 수렴되지 않은 결과는 탐색용·검토 전으로 표시합니다.
4. 기존 보고서에 목표/실행 조건/seed/출처/경고와 재현 입력을 보관하고, 로컬 manifest/hash로 확인합니다. 실제 Drive 업로드·배포·머지는 이 후속 작업의 신규 권한에 포함하지 않습니다.
5. 변경 후 검사·브라우저 결과와 남은 과학적 한계를 아래 후속 기록에 남깁니다. Tg 장시간 생산 계산과 원 출처가 없는 Bicerano 계수 도입은 실행하지 않습니다.

## 9. 목표 계약과 재현 입력 묶음 — 후속 진행

검증은 `work`, 시작 HEAD `1f9aabe44d918736c0edd6e1f1e854574d5b8806`의 로컬 변경으로 수행했습니다. 검사 뒤 사용자가 별도 브랜치 푸시와 PC 다운로드를 요청해 `codex/target-contract-repro`로 분리했습니다. main 머지·배포·Drive 업로드는 이 요청에 포함하지 않습니다.

### 현재 구현

- `target_spec.py` / `target_api.py`의 스키마 v1: 목표 반응·단위·독립 변수·설계·최대 계산 수를 명시합니다. 기존 Rust 설계·랭크 검사·만족도 커널과 기존 워크플로우/자동 실행을 재사용합니다. 새 물성 식이나 중복 수치 커널을 만들지 않습니다.
- API `POST /api/research/targets/preview|prepare`, MCP `prepare_target_workflow`: 준비만으로 MD를 시작하지 않습니다. 지원하지 않는 목표·단위·모델 추정 불가·예산·엔진 부족은 코드와 한국어 이유를 남깁니다. 미리보기의 추정 가능성 검사는 응답값 적합성 검사가 아닌 랭크 검사이며 자유도/순수 오차 부족을 함께 표시합니다.
- 준비한 목표 계약은 저장된 흐름과 해시로 연결되고 기존 화면에서 목표·가중치·시도 수·보고서 설정을 불러옵니다. 사용자가 입력한 단위를 자동 바꾸거나 혼합비 설계를 독립 변수 CCD로 대체하지 않습니다.
- 기존 보고서 ZIP에 `inputs/`를 추가했습니다. 목표 계약·DOE 원본·자동 실행 계획·모든 시도/확인/원래 셀의 설정·seed·상태·저장된 결과 요약을 보관합니다. 미기록 엔진 버전은 현재 환경 값으로 채우지 않고 `null/not_recorded`로 표시합니다. 파일은 기존 manifest/ZIP 해시로 확인하며 입력 총량은 20 MiB로 제한합니다.
- 원본 설계·CSV·목표 계약·계획 해시가 바뀌거나 입력이 심볼릭 링크이면 거부합니다. 사라진 과거 작업은 경고와 누락으로 보존하며 입력이나 과학적 결과를 재구성하지 않습니다.

### 가능 범위와 조건

| 구분 | 판단과 다음 단계 |
|---|---|
| 지금 가능한 자동화 | GAFF2 자체 벌크의 탐색용 목표 계약 → 기존 DOE/MD/분석/만족도/독립 확인/보고서. 유한한 관측·출처를 확인하고 계산/시도 예산 안에서 실행합니다. |
| 조건부 예측 | NPT 물성은 수렴·독립 반복과 적용 조건을 확인해야 합니다. PI1070 대리 모델은 기존 300 K MD 기준 적용 범위에서만 활용할 수 있으며 새 목표 계약의 자동 탈락 모델로 연결하지 않았습니다. |
| 조건부 Tg | Python/SciPy/LAMMPS 냉각·적합 경로는 구현돼 있으나 과거 세 PS 기록 모두 `transition_resolved=false`입니다. 큰 독립 셀·반복·온도 범위/유지 시간 계획과 원본 로그 보관이 필요합니다. 과거 숫자는 문서 기록이며 이번에 원시 로그로 재검증하지 않았습니다. |
| 조건부 Bicerano | Rust 연결성 지수만 있습니다. *Prediction of Polymer Properties* 3판(2002) 또는 원 논문의 식·계수·표 번호·단위, 극성기/회전 자유도/주쇄/Si·S 원자가 정의가 미확인입니다. 원문을 확인한 한 물성부터 구현해야 하며 상용 프로그램 파일은 필요하지 않습니다. |
| 제외·대체 | 근거 없는 WLF→DSC 환산, 미확인 계수, 체적 탄성률을 영률 E로 대체, MD만으로 신뢰할 수 있는 IR 예측, 고정 70% 자동 탈락은 도입하지 않습니다. 실제 측정 곡선 분석은 기존 별도 경로를 사용합니다. |

### 검사와 실행 기록

- 실행표 수정 후 전체 검사 1차: **533 passed, 18 skipped**, 533초. 건너뜀은 GROMACS/Martini, EMC, xtb, BayBE, 미수집 상류 소스와 직접 LAMMPS 로딩의 MPI 라이브러리 조건입니다. HTTP 작업 워커의 LAMMPS 연동과는 구분합니다. 이 실행 뒤 목표/보고서 경로를 더 수정했으므로 마지막 코드의 전체 재검사 여부는 아래 추가 기록을 따릅니다.
- Rust 릴리스: **28 passed, 1 ignored**(수동 성능 비교). 의존성 `pip check` 통과. Python wheel 생성 및 새 목표 모듈·화면·단일 설명서 포함 확인. Linux HTTP/브라우저 환경에서 확인했으며 Windows 빌드·실제 PyInstaller 데스크톱 바이너리는 검증하지 않았습니다.
- 신규 목표 경로 11개와 엔진 게이트 보완 3개 부분 검사 통과; MCP 5개 통과. 순환 참조로 MCP stdio 시작이 실패했던 문제는 서버를 불러오지 않는 `target_spec`에 모델을 두어 해결했습니다. 보고서 10개 본문 통과 뒤 새 fixture의 생성 시각 누락을 고쳐 해당 테스트/종료 처리 재검사 통과.
- 해석 fixture 통합은 MCP → API → 실제 Rust → 독립 확인 → 재현 보고서까지 `(317.5 K, 0.72 g/cm³)`, 합성 반응값 `1.1`을 재현하고 모든 해시와 `verified=false`를 확인했습니다. 실제 물성 검증이 아닙니다.
- 실제 GAFF2 목표 계획 **완료**: PE 220원자(사슬 2개×18 반복단위), 300~320 K, 초기 패킹 0.60~0.65 g/cm³, `protocol_scale=0.001`, Gasteiger, 독립 요인점 4건 + 새 seed 확인 1건. 승인/사용 5/5, 시도 1회, 실패 허용 0. 입력 점검 → Rust DOE → 실제 MD → 반응 수집 → Rust 적합/만족도 → 확인 → 보고서/재현 입력의 모든 단계를 완료했습니다. 별도 취소 계획은 승인 1건 이내에서 취소됐고, 예산 4건 승인 거부와 이중 시작 409도 확인했습니다.
- 모든 5건의 평형 수렴 판정은 `false`, 관측·확인 결과는 `verified=false`, 등록 측정값은 0입니다. 목표 물성 달성이나 물리적 예측 정확도로 해석하지 않습니다. 보고서의 20개 파일과 재현 입력 해시가 일치했고 엔진 버전 미기록 5건도 그대로 표시했습니다. 재시도 동작은 합성 실패 fixture로 검사했으며 실제 엔진 실패를 인위적으로 만들어 추가 계산하지 않았습니다.
- 실제 기록은 `/tmp/polymer-autopilot-integration/integration-result.json`, 흐름 `3aa4a3ce45b3420da7db73a613e700a9`, 보고서 `60887b79abef4f26ae83eff5e95cb8fb`입니다. 원시 MD·입력·보고서는 같은 임시 데이터 폴더에 보존했고 저장소에는 넣지 않았습니다.
- 최종 목표 화면은 Chromium 1440/390 px에서 계약의 목표/모델/가중치/시도 수/업로드 꺼짐 복원, 5초 갱신 중 입력 보존, 최대 5건 계획 미리보기, 미실행 상태 유지, 완료 보고서 그림·표와 JavaScript 오류 없음을 확인했습니다. 실제 업로드나 계산 추가 없이 검사했습니다.
- **최종 코드 전체 검사 완료: 548 passed, 16 skipped, 경고 1개, 705.68초.** `PATH=.venv/bin:$PATH`, `LD_LIBRARY_PATH=.venv/lib`, OpenBLAS/OMP 1, Rayon 2로 실행했습니다. 직접 LAMMPS 로딩 검사도 통과했고, 남은 건너뜀은 GROMACS/Martini·EMC·xtb·BayBE와 미수집 상류 소스입니다. 경고는 기존 Starlette TestClient 경고입니다. 로그는 `/tmp/polymer-target-final-full-tests.log`입니다.
- 최종 wheel의 새 목표 모듈·보고서·MCP·두 화면 JS·단일 설명서 바이트가 현재 소스와 일치하는 것을 확인했습니다. PyInstaller/Windows 바이너리는 만들지 않았습니다. 임시 통합 서버는 종료했고 원시 테스트 자료와 보고서는 `/tmp`에 보존했습니다.
- 검사 완료 시점은 `work` / 기준 HEAD `1f9aabe` 위의 로컬 변경 상태였습니다. 이후 별도 브랜치 푸시 요청으로 분리했으며, 마지막 검사 뒤 실행 코드는 수정하지 않았습니다. 원격 반영 결과와 정확한 커밋은 작업의 푸시 확인 기록을 따릅니다.

### 다음 작은 작업

1. 향후 작업 워커가 처음부터 엔진/의존성 버전을 남기도록 해 새 실행의 `not_recorded`를 줄입니다. 과거 버전을 추측해 채우지는 않습니다.
2. 저비용 스크리닝을 연결하려면 목표·온도·재료가 기존 대리 모델의 적용 범위에 맞는 별도 후보 계약부터 추가합니다. 불확실성을 고려한 보류/선택을 사용하고 고정 탈락률을 넣지 않습니다.
3. Tg와 Bicerano는 위 조건부 근거를 확보한 뒤 작은 검증 문제로 진행합니다. 이번에 전체 연구소 도구의 동등성이나 물성 정확도가 검증된 것은 아닙니다.

### 별도 브랜치 다운로드와 플랫폼 범위

사용자가 요청한 게시 대상은 `codex/target-contract-repro` 기능 브랜치이며 main 직접 푸시·머지·배포는 하지 않습니다. 업로드 대상은 소스·테스트·문서뿐입니다. `/tmp`의 계산 원자료·보고서·가상 환경·인증 파일은 넣지 않습니다.

Windows에서 소스 ZIP/Git 다운로드는 가능하지만 **Windows 네이티브 실행과 exe 배포는 검증하지 않았습니다.** 현재 `scripts/build_desktop.py`는 Linux x86_64 배포만 지원합니다. 확인된 실행 경로는 Linux 환경의 `setup.sh`와 `start.sh`이며, Windows에서는 WSL2 Ubuntu를 사용해 같은 소스 경로를 실행하는 방식을 우선 안내합니다. 이번 클라우드 검사를 실제 WSL2 PC 검증으로 간주하지 않습니다.

## 10. 결과 근거와 대표 밀도 반복 검토 (2026-10-24)

### 이번 완료 범위와 게시 상태

- 기준 브랜치는 `codex/target-contract-repro`, HEAD는 `f52f22906ba37f5bef70793d718ece59bec8671c`입니다. 이 절의 개선은 **커밋·푸시 전 로컬 변경**입니다. 이전 푸시 승인을 새 변경에 확대하지 않았으며 main·배포·Drive 업로드는 실행하지 않았습니다. 별도 Kali 설치 작업에는 접근하지 않았습니다.
- 기존 흐름의 실행 완료, 실제 원본의 수렴 판정, 독립 반복, 실험·문헌 대조, 입력·출처, 사람 검토를 별도 상태로 표시합니다. 이전에는 최종 구조로 구한 밀도 관측에 평형 판정이 없었으므로 `bulk_complete.json`의 실제 판정을 읽도록 연결했습니다. 판정이 없거나 원본이 손상되면 보류하며 DOE의 서로 다른 조건을 반복으로 합치지 않습니다.
- `native/src/reliability.rs`와 PyO3 `replicate_summary`는 Welford 표본분산, SE, Student-t 95% 구간, 명시된 참조 표준 불확도의 결합을 계산합니다. Welford(1962), Student(1908)의 서지는 코드에 적었습니다. 단일 반복의 산포는 미상이며, 동일 숫자만 반복된 경우 정밀도가 입증된 것으로 취급하지 않습니다. 수치 결과는 독립성·정규성 가정하의 기술 통계이지 물리 정확도 판정이 아닙니다.
- 대표 경로는 **GAFF2/native 벌크의 고정 조건 NPT 평균 밀도**입니다. 계획은 실행하지 않고 설정과 seed만 준비합니다. 기존 작업을 연결해 같은 재료·온도·압력·힘장·셀/프로토콜인지 확인하고, 중복 seed·혼합 조건·원본 해시 변경·잘못된 단위는 제외 사유를 기록합니다. 별도 실험·문헌 측정값을 ID로 연결하며 검토 상태를 바꾸지 않습니다.
- API/MCP → Rust → 근거 평가 → 화면 → 불변 JSON·그림·보고서 ZIP/manifest가 연결되었습니다. 평가 스냅샷은 원본 변경 뒤에도 유지됩니다. 모든 시뮬레이션은 `verified=false`, `scientifically_validated=false`이고, 기준을 만족해도 `review_required`입니다. 다음 검사 제안의 계산 수는 서로 겹칠 수 있으며 합산 예산이나 실행 승인이 아닙니다. 기존 취소·재개·재시도 동작은 변경하지 않았습니다.

### 실제 확인과 과학적 한계

- **현재 코드 관련 pytest 139 passed, 기존 경고 1개, 63.72초**: 새 커널·근거 평가·밀도/API·보고서·MCP·기존 API·Rust 바인딩·DOE·목표 계약·Drive 로컬 아카이브·단일 설명서를 검사했습니다. NumPy/SciPy와 SD/SE/t 구간/참조 불확도를 독립 대조했습니다. 로그: `/tmp/polymer-quality-verified-tests.log`.
- **Rust 전체 31 passed, 1 ignored**: `/tmp/polymer-quality-rust-full.log`. `pip check` 통과, 현재 소스의 새 모듈과 UI가 포함된 wheel 빌드 및 바이트 일치 확인. PyInstaller exe/Windows 빌드는 실행하지 않았습니다. 이 초기 관련 검사 이후 현재 변경 코드의 전체 pytest도 완료했습니다(아래 후속 전체 검사). 9절의 548 passed/16 skipped는 이전 f52 기준 검사입니다.
- 테스트 중 손상 파일을 의도적으로 만든 fixture가 종료 전에 복원되지 않아 처음에는 teardown이 실패했습니다. fixture 복원을 고쳤고 위 139개 최종 검사에서 재확인했습니다. 제품의 JobManager나 사용자 자료를 변경해 오류를 숨기지 않았습니다.
- Chromium **1440/390 px**에서 미검토 참조의 보류 → 이미 검토된 합성 참조 연결 → 사람 검토 필요, 불변 JSON 다운로드, 보고서 ZIP 다운로드/모든 manifest 해시/그림, 원본 변경의 명시적 제외, 기존 흐름의 실제 수렴 판정을 확인했습니다. 화면 넘침·JS 오류·새 작업 생성·업로드는 없었습니다. 합성 fixture의 검토값은 소프트웨어 검사용이며 실제 실험/사람의 승인 기록이 아닙니다. 로그: `/tmp/polymer-quality-final-browser.log`.
- 새 MD는 실행하지 않았습니다. 9절의 기존 엔진 자료 중 같은 조건 두 건을 복사해 읽었습니다(seed 54/56, 300 K, 1 atm, PE 18 반복단위×2 사슬, `protocol_scale=0.001`). 두 건 모두 `equilibrated=false`, 최소 계획 3회에도 미달하고 외부 대조값이 없어 **결론 보류**입니다. 이 두 평균으로 계산된 통계는 탐색용이며 검증된 PE 물성으로 쓰지 않습니다.
- 원본 `/tmp/polymer-autopilot-integration`은 그대로 보존했습니다. 이번 독립 검사용 복사본/평가/보고서는 `/tmp/polymer-quality-final-integration`, 실제 자료 검토 보고서는 `671331329b86495a9f45aceb9109164a`입니다. 저장소에는 계산 원자료·개인 측정값·인증 파일을 넣지 않았습니다. 임시 서버는 대기/실행 작업이 없는 것(완료 8, 취소 1)을 확인하고 종료했습니다.

### 다음 과학적 작업과 UI 병합 경계

1. 같은 명시적 조건에서 충분한 평형화·유지 시간과 독립 준비를 갖춘 반복 계획이 필요합니다. 기존 미수렴 두 건에 한 건만 추가해 수렴을 주장하면 안 됩니다. 셀 크기나 프로토콜이 달라지면 별도 연구로 분리하고 계산 예산을 먼저 정합니다.
2. 실제 재료의 분자량·시료 준비·측정 방법까지 대조 가능한 외부 밀도와 출처·표준 불확도, 실질적인 허용 차이를 확보해야 합니다. 현재 일치하는 검토 실험값은 제공되지 않았으며 과거 엔진 버전 누락을 추측해 채우지 않았습니다. Tg의 전이 분해와 Bicerano 계수의 원문 출처 문제도 아직 해결되지 않았습니다.
3. 독립 Kali UI 변경과 대조할 파일은 `workbench/static/index.html`, `research.css`, `workflows.js`, 새 `quality.js`, `density_studies.js`입니다. 추가 영역은 DOE 화면의 `#density-study-panel`과 워크플로의 `#flow-quality-content` 및 관련 결과 근거 UI입니다. 새 코드는 백엔드/API·과학 상태와 연결되므로 공통 레이아웃/내비게이션 개선을 합칠 때 이 ID·이벤트·스크립트 로드 순서를 보존합니다. 공유 원격에는 아직 게시하지 않았습니다.

### 후속 전체 회귀 검사 — 현재 변경 코드

- **578 passed, 16 skipped, 실패/오류 0, 기존 경고 1, 665.90초**. `.venv/bin/python -m pytest -v -rs --junitxml=/tmp/polymer-quality-current-full.xml`을 현재 코드로 실행했습니다. Linux/Python 3.12.14에서 `PATH=.venv/bin:$PATH`, `LD_LIBRARY_PATH=.venv/lib`, OpenBLAS/OMP 1, Rayon 2를 사용했습니다. 로그: `/tmp/polymer-quality-current-full.log`. 이전 검사 결과를 재사용하지 않았습니다.
- 16개 건너뜀은 GROMACS 의존 3개(`bridges` 2, `research_api` 1), EMC 9.4.4 미설치 3개, xtb 6.7.1 미설치 1개, BayBE 미설치 2개, 미수집 고정 참조 자료 7개(msi2lmp 6, PI1070 1)입니다. martinize2와 polyply는 설치되어 있지만 GROMACS는 없습니다. 이 검사는 미실행이며 통과로 세지 않습니다.
- 같은 코드로 Rust 전체 **31 passed, 1 ignored**, LAMMPS CLI/라이브러리 시작 확인, `pip check`, 변경 JS 3개 문법, `git diff --check`가 모두 통과했습니다. Rust의 ignored 1개는 수동 성능 비교이며 정확성 테스트 실패가 아닙니다. 로그: `/tmp/polymer-quality-current-aggregate.log`, 종료 코드: `/tmp/polymer-quality-current-aggregate.json`.
- 첫 Cargo 호출은 `RUSTUP_HOME` 미지정으로 기본 toolchain을 못 찾아 테스트 시작 전에 종료됐습니다. 이미 설치된 `/workspace/.rustup`, `/workspace/.cargo`를 명시해 해결했습니다. 다운로드·버전 변경·코드 수정은 없었고 최초 기록은 `/tmp/polymer-quality-current-aggregate-launcher-failure.log`에 남겼습니다.
- 전체 회귀에는 기존 GAFF2 소형 MD와 PCFF 짧은 Tg 실행 테스트가 포함되어 실제로 재실행됐습니다. 연구용 대규모/장시간 MD는 추가하지 않았습니다. 이 결과는 실행 경로의 소프트웨어 회귀 확인이며 수렴한 물성이나 Tg 검증이 아닙니다. 위의 실제 밀도 검토 두 건은 여전히 보류입니다.
- 검사 전후 변경 파일 **27개의 SHA-256이 모두 일치**했습니다(`/tmp/polymer-quality-current-full-source.json`). 그 뒤 변경한 것은 이 인계 문서의 결과 기록뿐입니다. 실행 코드는 검사한 코드 그대로입니다. 기존 브라우저·JSON/ZIP·wheel 바이트 검증도 같은 실행 코드에 해당합니다.
- CI의 Python 3.13, 선택 엔진/참조 자료를 복원한 전체 경로, PySide6/PyInstaller 데스크톱 번들, Windows는 여기서 검사하지 않았습니다. 현재 설치된 연구 의존성의 명시적 고정 버전 81개는 일치합니다. 기능 브랜치 푸시만으로는 `tests.yml`이 실행되지 않으며, 이번 변경은 여전히 미커밋·미게시입니다.

### 정확한 변경 파일과 Kali UI 통합 계약

추적 파일 수정 17개:
`AGENTS.md`, `docs/HANDOFF.md`, `docs/MANUAL.md`, `docs/MCP.md`, `docs/REPORTS.md`, `native/src/lib.rs`, `native/src/python.rs`, `packaging/polymer-lab.spec`, `tests/test_drive_results.py`, `workbench/drive_backup.py`, `workbench/mcp_server.py`, `workbench/reports.py`, `workbench/server.py`, `workbench/static/index.html`, `workbench/static/research.css`, `workbench/static/workflows.js`, `workbench/workflow_api.py`.

신규 파일 10개:
`native/src/reliability.rs`, `tests/test_density_studies.py`, `tests/test_quality_reports.py`, `tests/test_reliability_kernel.py`, `tests/test_result_quality.py`, `workbench/density_api.py`, `workbench/density_studies.py`, `workbench/result_quality.py`, `workbench/static/density_studies.js`, `workbench/static/quality.js`.

Kali의 별도 로컬 브랜치 `codex/kali-ui-navigation`, 커밋 `6be8b32e03e597c077d27296f5448883fe72a6da`는 전달받은 상태 정보이며 이 환경으로 가져오지 않았습니다. 두 작업의 공통 수정 파일은 `index.html`, `research.css`입니다. Kali의 `app.js`, `sheets.js`는 이번 신뢰도 변경에서 수정하지 않았습니다. 파일 전체를 덮어쓰지 말고 각 패널·추가 CSS·내비게이션 변경을 대조해야 합니다.

보존할 신규 정적 ID 21개:
`density-study-panel`, `density-history`, `density-refresh`, `density-create-fields`, `density-create`, `density-name`, `density-source-job`, `density-minimum`, `density-maximum`, `density-max-rsd`, `density-tolerance`, `density-message`, `density-detail`, `density-summary`, `density-attach`, `density-job-ids`, `density-reference`, `density-reference-id`, `density-reference-u`, `density-assess`, `density-assessment`.

기존 `#view-doe`/`#doe`와 워크플로 선택자, 동적 `flow-quality`, `flow-quality-refresh`, `flow-quality-content`, `[data-density-job]`, 보고서의 `data-report-kind`/`data-report-id`도 유지합니다. `polymer:view`의 `detail='doe'`, `polymer:density-study`의 `detail={job_id}`가 연결 계약입니다. 워크플로의 밀도 검토 사전 입력 버튼에는 별도 ID가 없으며 이 이벤트를 보냅니다.

현재 defer 순서:
`vendor/3Dmol-min.js` → `app.js` → `reports.js` → `research.js` → `screening.js` → `structure.js` → `doe.js` → `doe_mixture.js` → `process.js` → `quality.js` → `workflows.js` → `density_studies.js` → `drive.js` → `sheets.js`.

빈 산포/허용 차이/표준 불확도를 0이나 추정값으로 채우지 않고, 검토·계산 실행을 분리하며 중복 동작 방지와 `.table-scroll`의 내부 스크롤을 유지합니다. 합친 뒤 390/1440 px, 뒤로/앞으로·입력 보존·취소와 근거 평가/다운로드를 다시 확인해야 합니다. 이번 단계에서는 병합이나 게시를 하지 않았습니다.

### 승인된 다음 통합 단계

사용자가 두 개선분의 통합 검증 후 별도 기능 브랜치 푸시와 Kali 반영을 승인했습니다. 위 미커밋·미게시 표시는 전체 검사 완료 시점의 기록입니다. 신뢰도 개선은 로컬 체크포인트 커밋으로 보존하고, 부모가 Kali 브랜치의 원격 SHA를 확인해 전달한 뒤 별도 통합 브랜치에서 진행합니다. 이 승인으로 main 머지나 배포를 하지 않습니다.

Kali 브랜치의 현재 전달된 예상 HEAD는 `6826953baf4de0d7a59cbf43e91ac5a26f96ebe0`이며 앞 UI 커밋은 `6be8b32e03e597c077d27296f5448883fe72a6da`입니다. 아직 이 객체를 가져오거나 검증하지 않았습니다. 통합 때 새 `sidebar.js`, 내비게이션·입력 보존, 신뢰도/밀도 이벤트를 함께 유지하고 전체 pytest·Rust·JS 및 데스크톱/모바일 브라우저 흐름을 다시 검사합니다. 검증된 소스만 푸시하고 계산 원자료·인증 자료는 제외합니다. Kali 설치 반영은 부모가 최종 원격 SHA를 받아 별도로 수행합니다.

커밋 직전 제한된 diff 검토에서 두 가지 후속 보완을 정했습니다. 원본 `convergence_warnings`가 숫자/문자열/혼합 목록이면 해당 작업을 명시적으로 제외해 나머지 평가를 보존하도록 고쳤고, 밀도 계획 생성은 소스 요청 전에 폼 값을 스냅샷해 대기 중 편집과 섞이지 않도록 합니다. 백엔드/API/보고서 관련 **26 passed**로 손상 경고의 세 경우와 기존 경로를 확인했습니다(`/tmp/polymer-quality-precommit-robustness-tests.log`). 위 578개 전체 검사는 이 작은 후속 보완 전의 기록이며, 보완 및 Kali UI를 합친 최종 코드로 전체 검사를 다시 실행할 예정입니다.

## 11. Kali UI와 결과 근거 통합 — 최종 검증 (2026-10-24)

- 신뢰도 개선과 위 두 후속 보완을 `5c89e2c0fd294dc62edf329ae1c988bc2eb15d79`에 보존했습니다. 원격 Kali 브랜치를 한 번 가져와 전달받은 `6826953baf4de0d7a59cbf43e91ac5a26f96ebe0`과 일치함을 확인하고 `codex/reliability-kali-integration`에서 통합했습니다. 위 미커밋·미통합 표시는 이전 단계의 기록입니다.
- CSS의 공통 끝부분 충돌 한 곳은 양쪽 추가 규칙을 모두 보존해 해결했습니다. 자동 병합된 HTML도 의미를 비교했고 두 부모의 모든 DOM ID와 상대 스크립트 순서를 확인했습니다(통합 ID 563개, 스크립트 15개). `sidebar.js`는 `app.js` 바로 다음이며 `quality.js` → `workflows.js` → `density_studies.js` 순서도 유지됩니다.
- 변경은 31개 소스·테스트·문서·패키징 경로입니다. 10절의 27개에 Kali의 `workbench/static/app.js`, `sidebar.js`, `sheets.js`, `tests/ui/sidebar.test.cjs`가 추가됩니다. 공통 `index.html`·`research.css`는 양쪽 기능을 포함합니다. 설명서는 메뉴·폭 조절 안내를 같은 `MANUAL.md`에 보완했습니다. 원시 MD·측정 DB·보고서 자료·인증 파일은 게시 대상에 포함하지 않았습니다.
- **최종 통합 코드 전체 pytest: 581 passed, 16 skipped, 실패/오류 0, 기존 Starlette 경고 1, 793.56초.** Linux/Python 3.12.14, OpenBLAS/OMP 1, Rayon 2에서 새로 실행했습니다. 로그 `/tmp/polymer-merged-full.log`, JUnit `/tmp/polymer-merged-full.xml`. 기존 578개 기록을 재사용하지 않았습니다.
- 건너뜀 16개는 GROMACS 3, EMC 3, xtb 1, BayBE 2, 미수집 고정 참조 자료 7(msi2lmp 6, PI1070 1)입니다. **Rust 31 passed, 수동 성능 비교 1 ignored**, sidebar Node 2 passed, 변경 JS 6개 문법·의존성·diff 검사와 LAMMPS CLI/라이브러리 시작 확인도 통과했습니다. 로그는 `/tmp/polymer-merged-rust-full.log`, `/tmp/polymer-merged-sidebar-node.log`, `/tmp/polymer-merged-js-dependencies.json`, `/tmp/polymer-merged-lammps-probes.log`입니다.
- 전체 검사 전후 추적 파일 **416개의 SHA-256이 모두 일치**했습니다(`/tmp/polymer-merged-full-source.json`). 그 뒤 갱신한 것은 이 인계 문서의 결과 기록뿐입니다. 통합 wheel도 빌드했고 패키지 파일 86개와 설명서 바이트가 현재 소스와 일치합니다(`/tmp/polymer-merged-wheel`). Python 3.13 CI·선택 엔진 복원 경로·PyInstaller/Windows 배포 빌드는 미실행입니다.
- 통합 브라우저의 결과 근거/참조 연결·평가 JSON·보고서 ZIP/manifest·원본 변경 제외·실제 두 건의 미수렴 보류를 1440/390 px에서 재검사했습니다(`/tmp/polymer-merged-quality-browser.log`). sidebar 폭 키보드/드래그/취소/저장·초기화, 내비게이션/뒤로·앞으로/초점/skip link, 입력 보존·반응형 구성 패널, 실행표 빈 입력/예제/중복 방지/취소를 **8개 단계 모두 통과**했습니다(`/tmp/polymer-kali-ui-integration/report.json`). 실행표 제어는 모의 API이며 실제 계산 제출은 없습니다.
- 느린 소스 GET 중 모든 밀도 폼 값을 편집하는 브라우저 검사에서 클릭 당시 설정만 저장되고 이후 편집값은 유지됐습니다(`/tmp/polymer-merged-density-snapshot-browser-recheck.log`). 최초 하네스의 SMILES 문자열 비교는 서버의 정상 정규화를 무시했고, skip link 검사는 해시 이동을 새 문서 로드로 오인했습니다. 각각 원래 POST 설정과 저장된 정규화 표현을 비교하고 새 문서를 로드하도록 하네스만 고친 뒤 통과했습니다. 이 하네스 보완으로 제품 코드를 바꾸지는 않았습니다.
- 긴 RadonPy 회귀 중 실제 자식 LAMMPS의 CPU 사용과 0.1초 전 `eq3.log` 갱신을 확인해 진행 중임을 판별했고 중복 실행·강제 종료하지 않았습니다. 기존 소형 GAFF2/짧은 PCFF Tg 테스트의 통과는 소프트웨어 경로 확인이며 수렴한 물성 검증이 아닙니다. 실제 밀도 두 건은 계속 보류이며 과학적 검증·일치하는 외부 참조·Tg/Bicerano 근거 문제는 남아 있습니다.
- 임시 브라우저 서버는 기존 9개 작업이 완료 8/취소 1이고 새 계산이 없는 것을 확인하고 종료했습니다. 복사본·합성 평가·보고서는 `/tmp/polymer-merged-integration`에 보존했습니다. 검사 요약은 `/tmp/polymer-merged-verification.json`입니다.

게시 대상은 승인된 `codex/reliability-kali-integration` 기능 브랜치입니다. main 머지·배포·Drive 업로드는 하지 않습니다. 최종 커밋/원격 SHA와 CI 조회 결과는 작업의 게시 확인 기록을 따릅니다. 기능 브랜치 푸시는 현재 `tests.yml`의 자동 트리거가 아닙니다. Kali 설치 반영은 부모 작업에서 최종 SHA를 받은 뒤 수행하며 **새 `replicate_summary`를 포함하도록 `bash setup.sh` 또는 `.venv/bin/python -m pip install ./native`로 Rust 확장을 다시 빌드해야 합니다.**
