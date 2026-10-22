# AI 에이전트 연동 (MCP)

Polymer Lab은 [Model Context Protocol](https://modelcontextprotocol.io) stdio 서버를 내장합니다. Claude Code·Claude Desktop·Codex 같은 MCP 클라이언트가 계산 작업을 만들고, 진행 상태와 결과를 읽고, 측정값 초안·캠페인을 다룰 수 있습니다. MCP 프로세스는 기존 로컬 HTTP API의 얇은 클라이언트입니다. 계산 실행, 입력 검증, 검토 단계, 원본·해시 보관은 Polymer Lab 서버가 그대로 맡습니다.

## 연결

| 클라이언트 | 설정 |
|---|---|
| Claude Code (이 저장소) | 저장소의 `.mcp.json`에 `polymer-lab`이 등록되어 있습니다. 저장소 폴더에서 Claude Code를 열면 연결됩니다. |
| Claude Code (설치한 데스크톱 앱) | `claude mcp add --scope user polymer-lab -- ~/.local/bin/polymer-lab --mcp` |
| Claude Desktop | 설정 파일 `mcpServers`에 `{"polymer-lab": {"command": "/home/<사용자>/.local/bin/polymer-lab", "args": ["--mcp"]}}` |
| Codex | `~/.codex/config.toml`에 `[mcp_servers.polymer-lab]`, `command = "/home/<사용자>/.local/bin/polymer-lab"`, `args = ["--mcp"]` |

소스 체크아웃에서는 `<저장소>/.venv/bin/python -m workbench --mcp`를 명령으로 지정합니다. 설치한 데스크톱 앱의 `--mcp` 모드는 이 버전 이후로 다시 빌드한 배포본에 포함됩니다.

MCP 서버는 다음 순서로 실행 중인 Polymer Lab을 찾습니다.

1. `POLYMER_LAB_URL`(+ `POLYMER_LAB_TOKEN`) 환경 변수. 루프백 주소만 허용합니다.
2. 데스크톱 앱 세션 파일 `<작업 폴더>/.server.json`. 데스크톱 앱이 실행 중이면 그 창과 같은 서버·데이터에 붙습니다. 에이전트가 만든 작업이 앱 화면에 바로 나타납니다.
3. `./start.sh`의 기본 주소 `http://127.0.0.1:8765`(`POLYMER_PORT`).

찾지 못하면 `lab_status`가 이유를 알려 줍니다. 에이전트가 `start_lab_server`를 호출하면 MCP 세션이 소유하는 서버를 띄웁니다. **이 서버는 MCP 세션이 끝나면 함께 종료되며 실행 중인 계산을 중단합니다.** 긴 계산은 데스크톱 앱이나 `./start.sh`를 먼저 열어 둔 상태에서 요청하세요.

## 도구

| 도구 | 동작 |
|---|---|
| `lab_status` | 연결, 버전, 엔진·경로 가용성, Martini/GROMACS 준비 상태, 연구 데이터 개수 |
| `start_lab_server` | 서버가 없을 때 세션 소유 서버 시작 |
| `list_jobs` · `get_job` · `wait_for_job` | 작업 목록, 상태·단계·결과 요약, 완료 대기(최대 600초, 연구 DB 색인까지 확인) |
| `read_artifact` | JSON·로그·PDB/GRO·토폴로지·CSV·XVG 등 텍스트 결과 파일 읽기(바이너리 궤적 제외) |
| `submit_job` · `cancel_job` · `rerun_job` | 검증된 JobConfig로 계산 실행, 중단, 같은 설정으로 다시 실행 |
| `diagnose_job` · `retry_job_with_fix` | 로그 규칙 진단과 제안된 설정 수정 재실행 |
| `research_overview` · `list_experiments` · `list_measurements` | 색인된 계산 피처, 측정값, 모델·캠페인 요약 |
| `analyze_instrument_curve` | DSC·인장·TGA·GPC 곡선에서 저장하지 않는 측정값 초안 계산 |
| `add_measurement` | 측정값 저장. 항상 검토 전 상태로 저장 |
| `train_property_model` · `predict_property` | 검토 완료 데이터로 GP 기준 모델 학습, 예측 |
| `create_campaign` · `control_campaign` | 유한 후보 캠페인 생성, 한 배치 실행·자동 실행·중지·상태 확인 |
| `screen_polymer` | 반복단위 최대 20개의 RadonPy PI1070 참조 MD 물성 예측(불확실성·적용 범위·최근접 참조·내 벌크 계산 비교, 선택 시 교차검증) |
| `rank_solvents` · `fit_hsp_sphere` · `blend_compatibility` | Hansen 용매 순위(RED·χ·두 용매 혼합), 용해 시험으로 구 피팅(저장하지 않는 초안), 고분자 블렌드 χ와 임계 χ |
| `rea_evidence` | 엔진별 REA 감사 저장소의 고정 커밋·라이선스·검증 상태 |
| `export_research_snapshot` | Parquet 스냅샷 생성(설정된 동기화 폴더로 복사) |

각 도구는 MCP `readOnlyHint`/`destructiveHint` 표시를 갖습니다. 큰 시계열은 요약하고 원본 파일은 `read_artifact`로 읽게 합니다.

## 안전 장치와 기록

- **검토는 사람의 일:** `add_measurement`는 에이전트가 `verified: true`를 보내도 `verified=false`로 저장하고 `entry: {"via": "mcp", "client": ...}`를 남깁니다. 모델 학습·실험 목표 캠페인은 검토 완료 실험·문헌값만 쓰므로, 에이전트가 입력한 값은 사람이 앱에서 검토해야 반영됩니다.
- **요청자 기록:** MCP로 만든 작업은 `job.json`의 `submitted_via`에 `mcp:<클라이언트 이름>`이 기록되고 화면에 "AI 에이전트 요청 (MCP)"로 표시됩니다.
- **루프백 전용:** MCP 서버는 127.0.0.1/localhost/::1 이외의 주소에 요청하지 않습니다. 계산 서버도 기존처럼 루프백에서만 응답합니다.
- **세션 파일:** 데스크톱 세션의 주소·접속 토큰을 소유자만 읽을 수 있는(0600) `.server.json`에 기록하고 종료 시 지웁니다. 같은 사용자 계정의 프로세스는 이 토큰으로 API에 접근할 수 있습니다. 브라우저의 다른 웹사이트는 이 파일을 읽을 수 없습니다.
- **데이터 폴더 잠금:** 한 작업 폴더에는 서버 하나만 열립니다. 두 번째 서버는 시작하지 않고 오류를 냅니다. 서로의 실행 중 작업을 중단 처리하는 일을 막습니다.
- **계산 예산:** `control_campaign`의 `start`는 캠페인 예산(최대 50개)까지 계산을 자동 실행합니다. 서버 안내문(instructions)은 예산을 사용자와 먼저 확인하도록 에이전트에게 요구합니다.
- **pH와 전하:** 단백질 결과의 PROPKA3 분석·PDB2PQR 양성자화 구조와 `packmol_target` `negative_patch`/`positive_patch`, 혼합계 `protein_protonation: propka`는 `submit_job`의 JobConfig(`protein_ph`, `ionic_strength_molar`)로 지정합니다. 안내문은 pKa가 pH ±1 안인 기를 불확실한 상태로 다루고, 패치 배치를 결합 근거로 해석하지 않도록 에이전트에게 알립니다.
- **PCFF · 가교 · 보정:** `submit_job`의 JobConfig로 `atomistic_forcefield: pcff`(EMC PCFF 벌크 셀), `charge_method: xtb_cm5`(GAFF2 벌크 셀의 xtb CM5 전하), `atomistic_mode: crosslink`(에폭시–아민 bond/react 경화), `protein_repair: pdbfixer`를 지정합니다. 안내문은 PCFF 계수 대조 결과를 함께 보고하고, 가교 결과를 경화 속도론이 아닌 네트워크 구조로, 단축 계산을 탐색용으로 다루도록 에이전트에게 알립니다. `analyze_instrument_curve`의 DSC `analysis: melting`은 용융 온도·엔탈피·결정화도 초안을 돌려줍니다.
- **참조 예측 구분:** 서버 안내문은 `screen_polymer` 값이 공개 MD 데이터 기반 예측이며 측정값이 아님을, HSP 블렌드 χ가 수소결합·이온 상호작용을 표현하지 못함을 에이전트에게 알립니다. 구 피팅 결과는 초안으로만 돌려주고 저장은 `add_measurement`(검토 전)로만 합니다.
- **해석 범위:** 서버 안내문은 `completed`/`prepared`/`blocked`의 의미, 짧은 계산·CG 모델·접촉 수의 해석 한계를 에이전트에게 알립니다. 도구 결과에는 각 경로의 경고와 단위가 그대로 포함됩니다.

## 한계

MCP 도구만 제공하며 resources·prompts·진행 알림 스트리밍은 없습니다. `wait_for_job`은 클라이언트의 도구 시간 제한보다 짧게 설정해야 합니다. 시간이 오래 걸리면 `get_job`으로 다시 확인합니다. 파일 업로드는 텍스트(PDB·CSV 등)를 인자로 전달하는 방식이며 PDF 업로드 도구는 없습니다.
