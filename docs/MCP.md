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

### claude.ai 계정(웹·데스크톱·모바일 앱)에 연결

claude.ai의 **사용자 지정 커넥터**는 인터넷에서 HTTPS로 닿는 원격 MCP 주소가 필요합니다. 그래서 내 컴퓨터의 Polymer Lab을 계정에 붙이려면 HTTP MCP 서버 하나와 터널 하나가 필요합니다.

- **`--mcp-http` 모드**: Polymer Lab의 HTTP MCP 서버입니다(Streamable HTTP, `workbench/mcp_http.py`).
  - 127.0.0.1에서만 받습니다.
  - 주소 경로에 긴 비밀 토큰이 들어갑니다.
  - 도구와 검토 규칙은 stdio 모드와 같습니다.
- **터널**: 이 주소를 바깥에 HTTPS로 공개합니다.

1. Polymer Lab 데스크톱 앱이나 `./start.sh`를 실행해 둡니다. 계산은 이 컴퓨터에서 돌기 때문에, 커넥터를 쓰는 동안 컴퓨터가 켜져 있어야 합니다.
2. HTTP MCP 서버를 켭니다.
   ```sh
   <저장소>/.venv/bin/python -m workbench --mcp-http          # 소스 체크아웃
   ~/.local/bin/polymer-lab --mcp-http                         # 이 버전 이후로 다시 빌드한 데스크톱 앱
   ```
   - 기본 포트는 8766이고, `--port`로 바꿀 수 있습니다.
   - 처음 실행하면 비밀 토큰이 `<작업 폴더>/.mcp_http_token`(0600)에 만들어지고, 화면에 `/mcp/<토큰>` 경로가 나옵니다.
3. 다른 터미널에서 터널을 엽니다. 둘 중 하나를 고르면 됩니다.
   - **Tailscale Funnel**(주소가 바뀌지 않아 권장)
     - Tailscale을 설치하고 `sudo tailscale up`으로 로그인합니다.
     - `sudo tailscale funnel --bg 8766`을 실행합니다.
     - 주소는 `https://<컴퓨터 이름>.<tailnet>.ts.net`입니다.
   - **cloudflared 빠른 터널**(계정 불필요)
     - `cloudflared tunnel --url http://127.0.0.1:8766`을 실행합니다.
     - 나오는 `https://….trycloudflare.com` 주소는 실행할 때마다 바뀝니다. 바뀌면 커넥터 주소도 다시 넣어야 합니다.
4. 커넥터 주소를 확인합니다. 터널 주소를 넣고 다시 실행하면 전체 주소가 나옵니다.
   ```sh
   .venv/bin/python -m workbench --mcp-http --public-url https://<터널 주소>
   ```
5. claude.ai에서 **설정 → 커넥터 → 사용자 지정 커넥터 추가**를 엽니다. 이름에 `Polymer Lab`, 주소에 4단계의 전체 주소를 넣습니다.
6. 대화 화면의 도구 메뉴에서 Polymer Lab을 켜면 됩니다. 웹에서 추가한 커넥터는 같은 계정의 모바일 앱에서도 쓸 수 있습니다.

**알아 둘 점**
- **커넥터 주소가 곧 비밀번호입니다.**
  - 주소를 아는 사람은 이 컴퓨터에서 계산을 실행하고, 작업 결과 파일을 읽고, 검토 전 측정값을 넣을 수 있습니다. 측정값을 검토 완료로 바꾸는 것은 앱의 사람만 할 수 있습니다.
  - 주소가 새었다고 생각되면 `--new-token`으로 토큰을 바꾸고 커넥터 주소를 다시 넣습니다.
  - 쓰지 않을 때는 터널을 끕니다(`sudo tailscale funnel --bg off` 또는 cloudflared 종료).
- **보안 장치**: 브라우저의 다른 웹사이트가 보내는 요청(`Origin`이 claude.ai/claude.com이 아닌 요청)은 거절합니다. 잘못된 경로는 404를 돌려주고, 로그에는 비밀 경로를 남기지 않습니다.
- **오래 기다리는 도구**: `wait_for_job`은 최대 600초까지 기다리지만, 원격 커넥터 요청이 그보다 먼저 끊길 수 있습니다. 긴 계산은 제출한 뒤 나중에 `get_job`으로 확인하게 하세요.
- **확인한 범위**:
  - 공식 MCP Python SDK 2.3.0의 Streamable HTTP 클라이언트로 접속, 도구 목록, 도구 호출, 실제 계산 작업 제출·완료를 확인했습니다.
  - claude.ai 커넥터 자체로는 아직 연결해 보지 않았습니다.

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
| `analyze_job_structure` | 완료된 벌크·가교 작업의 RDF·MSD·자유 부피·Widom 분석. 원본 작업을 유지하고 별도 불변 결과 저장·다운로드 링크 반환 |
| `validate_job` | 제출 전 검사(작업을 만들지 않음): JobConfig 검증, 반복단위 연결점 `*` 2개 규칙, 단량체 SMILES(예: `C=Cc1ccccc1`)의 반복단위 후보, 가교 수지·경화제 검사, 경로별 원자 수 제한(벌크 200개 이상), 필요한 엔진 |
| `submit_job` · `cancel_job` · `rerun_job` | 검증된 JobConfig로 계산 실행, 중단, 같은 설정으로 다시 실행 |
| `diagnose_job` · `retry_job_with_fix` | 로그 규칙 진단과 제안된 설정 수정 재실행 |
| `research_overview` · `list_experiments` · `list_measurements` | 색인된 계산 피처, 측정값, 모델·캠페인 요약 |
| `analyze_instrument_curve` | DSC·인장·TGA·GPC·IR·UV-vis·탁도 곡선(IR·UV-vis는 JCAMP-DX도)에서 저장하지 않는 측정값 초안 계산 |
| `analyze_process_data` | 공정 CSV 하나를 저장 없이 분석: 데이터 사전(키·중복·균형), 관리도(I-MR·X̄-S, 신호 규칙), 공정 능력(Cp·Cpk·Pp·Ppk), 분산 성분(균형 내포 ANOVA), 층별 불량률(Mantel–Haenszel), 불량 판정 평가(기준별 놓침·오탐·비용, 모두 정상 판정 기준선), 가상계측 오차(규격 경계 구간), 검증 분할 비교(무작위·로트·시간). 통계 계산 줄에서 실행 |
| `create_experiment_design` | Rust로 무작위 실행 순서의 완전 요인·면 중심 중심합성 설계를 만들고 설계 CSV·결과 기록 저장 |
| `analyze_response_surface` | Rust 회귀·ANOVA·적합성 결여·잔차 영향도·예측 R²·모델 비교·예측구간과 확인 실험표를 저장. 추천 조건은 실험 검증 전 |
| `create_mixture_design` | 성분 비율 합이 1인 심플렉스 격자·심플렉스 중심·꼭짓점 설계(축 점검 배합·중심점 선택)를 만들고 저장. 하한은 L-유사성분 |
| `analyze_mixture_response` | Scheffé 1차·2차·특수 3차 적합(유사성분 기준), ANOVA·모델 비교, 성분 범위 안 추천 배합과 예측구간. 1차 혼합 계수는 검정하지 않음 |
| `optimize_desirability` | 공정 인자 또는 혼합물 성분의 여러 반응을 각각 맞춘 뒤 Derringer–Suich 종합 만족도 D가 가장 큰 후보, 개별 d·예측구간·관측 최고 D 행 |
| `add_measurement` | 측정값 저장. 항상 검토 전 상태로 저장 |
| `train_property_model` · `predict_property` | 검토 완료 데이터로 GP 기준 모델 학습, 예측 |
| `create_campaign` · `control_campaign` | 유한 후보 캠페인 생성, 한 배치 실행·자동 실행·중지·상태 확인 |
| `screen_polymer` | 반복단위 최대 20개의 RadonPy PI1070 참조 MD 물성 예측(불확실성·적용 범위·최근접 참조·내 벌크 계산 비교, 선택 시 교차검증) |
| `rank_solvents` · `fit_hsp_sphere` · `blend_compatibility` | Hansen 용매 순위(RED·χ·두 용매 혼합), 용해 시험으로 구 피팅(저장하지 않는 초안), 고분자 블렌드 χ와 임계 χ |
| `blend_chi_contacts` | Blends 방식 분자 접촉 χ(T)·배위수·임계점(Rust 커널, 실험적, 엔탈피 χ만) |
| `rea_evidence` | 엔진별 REA 감사 저장소의 고정 커밋·라이선스·검증 상태 |
| `run_research_workflow_plan` | 벌크 연구 흐름 계획의 최대 계산 수 미리보기, 사용자가 승인한 계획·예산으로 끝까지 실행(같은 설정 재시도·실패 한도·최적화·확인 계산·보고서), 멈추기 |
| `create_research_report` | DoE 기록·연구 흐름·연구 데이터 마스터 표의 보고서 폴더(요약·그림·CSV·xlsx·해시) 저장, 요청할 때만 Drive 업로드 후 파일별 MD5 확인 |
| `export_research_snapshot` | Parquet 스냅샷 생성(설정된 동기화 폴더로 복사) |

각 도구는 MCP `readOnlyHint`/`destructiveHint` 표시를 갖습니다. 큰 시계열은 요약하고 원본 파일은 `read_artifact`로 읽게 합니다. `analyze_job_structure`와 `analyze_process_data`는 기본으로 요약(`detail: "summary"`)을 돌려줍니다. 20개를 넘는 숫자 배열은 개수·최솟값·최댓값으로, 긴 기록 목록은 처음 몇 개로 줄입니다. 관리도는 3σ 밖 점을 먼저 최대 20개까지 남깁니다. 전체 배열은 `detail: "full"`이나 저장된 결과의 다운로드 링크로 받습니다.

`submit_job`은 작업자 안에서만 실패하던 화학 오류(연결점 수, 수지 에폭시 수, 원자 수 부족)를 작업을 만들기 전에 HTTP 422로 거절합니다. 같은 검사는 `POST /api/jobs/preflight`와 `validate_job`으로 미리 할 수 있습니다.

### 완료된 셀의 구조·궤적 분석

`analyze_job_structure`는 `job_id`, `analysis`(`rdf`, `msd`, `free_volume`, `widom`), 선택적 `parameters`를 받습니다. 같은 입력을 HTTP로 쓰려면 `POST /api/jobs/{job_id}/analysis`에 `{"analysis": "rdf", "parameters": {"pairs": ["C-C", "C-H"], "r_max": 8.0, "bins": 160}}`를 보냅니다. 완료된 `polymer_atomistic` 벌크·가교 작업만 분석하며, 원본 작업·해시 목록·연구 측정값은 변경하지 않습니다. 성공할 때마다 `research/analyses` 아래 별도 불변 결과를 저장하고 `/api/research/analyses/{id}/download` 다운로드 링크와 전체 분석 배열을 반환합니다. 새 기록을 만들므로 `readOnlyHint`와 `idempotentHint`는 모두 false입니다.

| 분석 | 주요 매개변수 | 입력·해석 제한 |
|---|---|---|
| `rdf` | `pairs`, `r_max`(Å), `bins`, `every`, `max_frames`, `exclude_same_molecule` | `C-C`, `C-O`, `heavy-heavy` 등의 원소 쌍. 거리 상한은 가장 짧은 상자 길이의 절반. 궤적이 없으면 최종 구조 한 장 |
| `msd` | `selection`, `remove_drift`, `time_step_fs`, `fit_window`, `every`, `max_frames` | 이미지 인덱스가 있는 등간격 궤적 필요. 시간 간격은 벌크 메타데이터 또는 명시적 값 필요. 확산 영역인지 경고 확인 |
| `free_volume` | `probes`(Å), `spacing`(Å), `frame`(`last` 또는 0부터 시작하는 번호) | 최종 구조 또는 지정 궤적 프레임. h와 2h의 점유·접근 가능·탐침 점유율 차이는 격자 민감도이며 엄밀한 오차 한계가 아님 |
| `widom` | `gas`, `temperature`(K), `insertions`, `every`, `max_frames`, `cutoff`(Å), `seed` | GAFF2 벌크 셀의 12-6 LJ만 지원. PCFF·가교 제외. 단일 점 기체·고정 고분자 근사로 실험 용해도를 보장하지 않음 |

구조 분석은 한 번에 하나씩 실행되며 MD 계산과는 다른 실행 줄을 씁니다. 다른 구조·접촉 분석이 진행 중이면 최대 30초 기다린 뒤 HTTP 429, 미완료 작업은 409, 잘못된 매개변수·입력은 422, Rust 커널이 없으면 503을 반환합니다. 프레임 수·격자 크기·삽입 횟수에는 계산 예산 제한이 있습니다. 큰 요청은 클라이언트의 시간 제한에 걸릴 수 있으므로 먼저 적은 프레임·거친 격자로 확인합니다. 결과의 `provenance`에는 적용한 매개변수, 입력 파일 SHA-256, 커널 버전·해시, 분석 코드 해시가 들어갑니다.

### 실험계획·반응표면 API

| MCP 도구 | HTTP 경로 | 주요 입력 |
|---|---|---|
| `create_experiment_design` | `POST /api/research/doe/design` | `factors: [{name, low, high}]`, `kind: factorial 또는 central_composite`, `center_points`, `replicates`, `seed` |
| `analyze_response_surface` | `POST /api/research/doe/analyze` | `text: 측정 CSV`, `factors`, `response: 반응값 열`, `model: linear·interaction·quadratic`, `objective: maximize·minimize·target`, 선택적 `target`, `prediction_points: [{인자명: 실제값}]`(최대 50개) |
| `create_mixture_design` | `POST /api/research/doe/mixture-design` | `components: [{name, low, high}]`(비율 0~1), `kind: simplex_lattice·simplex_centroid·extreme_vertices`, `degree`, `axial`, `centroid`, `replicates`, `seed` |
| `analyze_mixture_response` | `POST /api/research/doe/mixture-analyze` | `text`, `components`, `response`, `model: scheffe_linear·scheffe_quadratic·scheffe_special_cubic`, `objective`, 선택적 `target`, `prediction_points` |
| `optimize_desirability` | `POST /api/research/doe/desirability` | `text`, `responses: [{name, model, goal, lower, target, upper, lower_shape, upper_shape, importance}]`, `factors` 또는 `components` 중 하나 |
| `run_research_workflow_plan` | `POST /api/research/workflows/{id}/autopilot/preview`, `…/autopilot`, `…/autopilot/stop` | `action: preview·start·stop`, `plan: {rows, max_attempts, max_failed_rows, optimize, model, objective, target, confirm, report, upload_report}`, start에는 `approved_budget`(미리보기 최대 계산 수 이상) |
| `create_research_report` | `POST /api/research/reports`, 선택 시 `POST /api/research/reports/{id}/drive` | `kind: doe·workflow·lab`, `id`(doe·workflow), `upload_to_drive` |

두 도구는 `research/doe/{id}`에 불변 JSON과 설계 또는 측정 CSV를 저장하므로 `readOnlyHint=false`입니다. 결과의 `download_url`은 JSON, `csv_download_url`은 CSV입니다. 기록 목록은 `GET /api/research/doe`, 상세는 `GET /api/research/doe/{id}`로 읽습니다. CSV는 UTF-8 기준 1 MB 이하이며 누락된 행을 자동 제거하지 않습니다. 저장해도 검토 완료 측정값이 되지 않습니다.

## 안전 장치와 기록

- **검토는 사람의 일:** `add_measurement`는 에이전트가 `verified: true`를 보내도 `verified=false`로 저장하고 `entry: {"via": "mcp", "client": ...}`를 남깁니다. 모델 학습·실험 목표 캠페인은 검토 완료 실험·문헌값만 쓰므로, 에이전트가 입력한 값은 사람이 앱에서 검토해야 반영됩니다.
- **요청자 기록:** MCP로 만든 작업은 `job.json`의 `submitted_via`에 `mcp:<클라이언트 이름>`이 기록되고 화면에 "AI 에이전트 요청 (MCP)"로 표시됩니다.
- **루프백 전용:** MCP 서버는 127.0.0.1/localhost/::1 이외의 주소에 요청하지 않습니다. 계산 서버도 기존처럼 루프백에서만 응답합니다.
- **세션 파일:** 데스크톱 세션의 주소·접속 토큰을 소유자만 읽을 수 있는(0600) `.server.json`에 기록하고 종료 시 지웁니다. 같은 사용자 계정의 프로세스는 이 토큰으로 API에 접근할 수 있습니다. 브라우저의 다른 웹사이트는 이 파일을 읽을 수 없습니다.
- **데이터 폴더 잠금:** 한 작업 폴더에는 서버 하나만 열립니다. 두 번째 서버는 시작하지 않고 오류를 냅니다. 서로의 실행 중 작업을 중단 처리하는 일을 막습니다.
- **계산 예산:** `control_campaign`의 `start`는 캠페인 예산(최대 50개)까지 계산을 자동 실행합니다. 서버 안내문(instructions)은 예산을 사용자와 먼저 확인하도록 에이전트에게 요구합니다.
- **Martini 3 혼합계 옵션:** `submit_job`의 혼합계 MD(`bridge_mode: simulate`)에 `elastic_plddt_min`(탄성망을 pLDDT 신뢰 구간 안으로 제한, `elastic_network`·`bfactor_is_plddt` 필요)과 실험적 `protein_polymer_lambda`(단백질–고분자 LJ ε만 배율)를 줄 수 있습니다. 안내문은 λ가 이 계에 맞춘 값이 아니므로 λ = 1 결과와 함께 보고하도록 에이전트에게 알립니다.
- **pH와 전하:** 단백질 결과의 PROPKA3 분석·PDB2PQR 양성자화 구조와 `packmol_target` `negative_patch`/`positive_patch`, 혼합계 `protein_protonation: propka`는 `submit_job`의 JobConfig(`protein_ph`, `ionic_strength_molar`)로 지정합니다. 안내문은 pKa가 pH ±1 안인 기를 불확실한 상태로 다루고, 패치 배치를 결합 근거로 해석하지 않도록 에이전트에게 알립니다.
- **PCFF · 가교 · 보정:** `submit_job`의 JobConfig로 `atomistic_forcefield: pcff`(EMC PCFF 벌크 셀), `charge_method: xtb_cm5`(GAFF2 벌크 셀의 xtb CM5 전하), `atomistic_mode: crosslink`(에폭시–아민 bond/react 경화, `cure_cutoff_schedule: stepwise`와 `secondary_amine_probability`), `atomistic_mode: tg_scan`(완료된 벌크·가교 작업을 `source_job_id`로 받아 단계 냉각 NPT와 쌍곡선 Tg), `protein_repair: pdbfixer`를 지정합니다. 안내문은 PCFF 계수 대조 결과를 함께 보고하고, Tg에는 냉각 속도를 붙이며 DSC 값으로 환산하지 말라고 알리고, 가교 결과를 경화 속도론이 아닌 네트워크 구조로, 단축 계산을 탐색용으로 다루도록 에이전트에게 알립니다. `analyze_instrument_curve`의 DSC `analysis: melting`은 용융 온도·엔탈피·결정화도 초안을 돌려줍니다. `ir`·`uvvis`·`turbidity` 종류는 피크와 작용기 후보, 카보닐 지수·밴드 비·에폭시 전환율, λmax·농도·광학 밴드갭, 흐림점을 계산합니다. 안내문은 작용기 후보가 동정이 아니며 농도는 시료 값이라 저장되지 않는다고 에이전트에게 알립니다.
- **참조 예측 구분:** 서버 안내문은 `screen_polymer` 값이 공개 MD 데이터 기반 예측이며 측정값이 아님을, HSP 블렌드 χ가 수소결합·이온 상호작용을 표현하지 못함을 에이전트에게 알립니다. 구 피팅 결과는 초안으로만 돌려주고 저장은 `add_measurement`(검토 전)로만 합니다.
- **해석 범위:** 서버 안내문은 `completed`/`prepared`/`blocked`의 의미, 짧은 계산·CG 모델·접촉 수의 해석 한계를 에이전트에게 알립니다. 도구 결과에는 각 경로의 경고와 단위가 그대로 포함됩니다.

## 한계

MCP 도구만 제공하며 resources·prompts·진행 알림 스트리밍은 없습니다. `wait_for_job`은 클라이언트의 도구 시간 제한보다 짧게 설정해야 합니다. 시간이 오래 걸리면 `get_job`으로 다시 확인합니다. 파일 업로드는 텍스트(PDB·CSV 등)를 인자로 전달하는 방식이며 PDF 업로드 도구는 없습니다.


### 단계별 벌크 연구 연결

`get_research_workflow`는 연결 기록을 조회하고, `create_research_workflow`는 DOE 설계와 GAFF2 자체 벌크 템플릿을 연결합니다. `metric`(대표 반응값)과 `responses`(함께 수집할 값)는 `final_frame_density`·`probe_accessible_fraction`(자유 부피 분석)과 `npt_density`·`bulk_modulus`·`volume_expansion`(작업의 NPT 표본 평균, RadonPy 수렴 판정 포함) 중에서 고릅니다. 생성은 계산을 시작하지 않습니다. `control_research_workflow`는 사용자가 확인한 단계 하나(`submit`, `attach`, `analyze`, `optimize`(선택 `response`), `optimize-desirability`(`goals`), `confirm`, `analyze-confirmation`, `cancel`)만 실행합니다. 다음 단계를 자동 호출하지 않습니다. 반응값의 출처와 검증은 `docs/WORKFLOWS.md`에 있습니다. 모든 값은 미검증 시뮬레이션 결과로 유지됩니다. REST 경로는 `/api/research/workflows`, 실행 상태는 `/api/execution`입니다. 단계별 입력 형식은 `/docs`의 OpenAPI를 따릅니다.
