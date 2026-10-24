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

## 2. 중간에 멈춘 일: 실행표(paste-to-run sheets)

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

**안 된 것 / 확인하지 않은 것**
- **화면 스모크 테스트 실패(미해결)**: 헤드리스 Chromium에서 `/#sheets`로 열었을 때 예제 버튼(`[data-sheet-template]`)이 30초 안에 나타나지 않았습니다. 원인은 조사하지 못했습니다. 후보는 해시로 처음 열 때 `polymer:view` 이벤트가 `sheets.js` 리스너 등록 전에 발생하는 순서 문제입니다(`app.js` 201행 `showView(hash)`와 defer 스크립트 순서). API·MCP는 테스트로 동작을 확인했습니다.
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
  - 예제를 텍스트 파일로 내려받기

**사용자에게 준 붙여넣기 예제**: `sheets.TEMPLATES`와 같습니다. 앱의 예제 버튼, `GET /api/research/sheets/templates`, MCP `run_sheet action=templates`로 볼 수 있습니다.

## 3. 해결되지 않은 과학·검증 문제

1. **Tg(PS 1,296원자 셀)**: 세 번의 스캔 모두 전이를 충분히 분해하지 못했습니다(`docs/GLASS_TRANSITION.md`). 큰 셀과 복제 계산이 필요합니다. 기본 온도 범위는 바꾸지 않았고 사용자 결정을 기다립니다.
2. **Bicerano 상관식 계수**: 원 출처를 확인해야 넣을 수 있습니다(원칙 7). 이 컨테이너의 네트워크에서는 확인하지 못했습니다. 커널(연결성 지수)만 있습니다.
3. **공정 분석 실데이터 대조**: 사용자 PC에서 NIST Furnace·UCI SECOM 대조를 해야 합니다(`scripts/validate_process_furnace.py`, `scripts/validate_secom.py`). 데이터는 커밋하지 않습니다.
4. **자유 부피·Widom·Blends χ**: `AGENTS.md` "지금 진행 중인 일"과 `docs/RUST_KERNELS.md`의 한계가 그대로입니다.
5. **자동 실행(autopilot) 실제 엔진 확인**: 테스트는 합성 작업으로만 했습니다. 실제 엔진으로 끝까지 돌린 기록은 없습니다.

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
