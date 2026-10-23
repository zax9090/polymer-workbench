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
| `submit_job` · `cancel_job` · `rerun_job` | 검증된 JobConfig로 계산 실행, 중단, 같은 설정으로 다시 실행 |
| `diagnose_job` · `retry_job_with_fix` | 로그 규칙 진단과 제안된 설정 수정 재실행 |
| `research_overview` · `list_experiments` · `list_measurements` | 색인된 계산 피처, 측정값, 모델·캠페인 요약 |
| `analyze_instrument_curve` | DSC·인장·TGA·GPC·IR·UV-vis·탁도 곡선(IR·UV-vis는 JCAMP-DX도)에서 저장하지 않는 측정값 초안 계산 |
| `add_measurement` | 측정값 저장. 항상 검토 전 상태로 저장 |
| `train_property_model` · `predict_property` | 검토 완료 데이터로 GP 기준 모델 학습, 예측 |
| `create_campaign` · `control_campaign` | 유한 후보 캠페인 생성, 한 배치 실행·자동 실행·중지·상태 확인 |
| `screen_polymer` | 반복단위 최대 20개의 RadonPy PI1070 참조 MD 물성 예측(불확실성·적용 범위·최근접 참조·내 벌크 계산 비교, 선택 시 교차검증) |
| `rank_solvents` · `fit_hsp_sphere` · `blend_compatibility` | Hansen 용매 순위(RED·χ·두 용매 혼합), 용해 시험으로 구 피팅(저장하지 않는 초안), 고분자 블렌드 χ와 임계 χ |
| `blend_chi_contacts` | Blends 방식 분자 접촉 χ(T)·배위수·임계점(Rust 커널, 실험적, 엔탈피 χ만) |
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
- **PCFF · 가교 · 보정:** `submit_job`의 JobConfig로 `atomistic_forcefield: pcff`(EMC PCFF 벌크 셀), `charge_method: xtb_cm5`(GAFF2 벌크 셀의 xtb CM5 전하), `atomistic_mode: crosslink`(에폭시–아민 bond/react 경화), `protein_repair: pdbfixer`를 지정합니다. 안내문은 PCFF 계수 대조 결과를 함께 보고하고, 가교 결과를 경화 속도론이 아닌 네트워크 구조로, 단축 계산을 탐색용으로 다루도록 에이전트에게 알립니다. `analyze_instrument_curve`의 DSC `analysis: melting`은 용융 온도·엔탈피·결정화도 초안을 돌려줍니다. `ir`·`uvvis`·`turbidity` 종류는 피크와 작용기 후보, 카보닐 지수·밴드 비·에폭시 전환율, λmax·농도·광학 밴드갭, 흐림점을 계산합니다. 안내문은 작용기 후보가 동정이 아니며 농도는 시료 값이라 저장되지 않는다고 에이전트에게 알립니다.
- **참조 예측 구분:** 서버 안내문은 `screen_polymer` 값이 공개 MD 데이터 기반 예측이며 측정값이 아님을, HSP 블렌드 χ가 수소결합·이온 상호작용을 표현하지 못함을 에이전트에게 알립니다. 구 피팅 결과는 초안으로만 돌려주고 저장은 `add_measurement`(검토 전)로만 합니다.
- **해석 범위:** 서버 안내문은 `completed`/`prepared`/`blocked`의 의미, 짧은 계산·CG 모델·접촉 수의 해석 한계를 에이전트에게 알립니다. 도구 결과에는 각 경로의 경고와 단위가 그대로 포함됩니다.

## 한계

MCP 도구만 제공하며 resources·prompts·진행 알림 스트리밍은 없습니다. `wait_for_job`은 클라이언트의 도구 시간 제한보다 짧게 설정해야 합니다. 시간이 오래 걸리면 `get_job`으로 다시 확인합니다. 파일 업로드는 텍스트(PDB·CSV 등)를 인자로 전달하는 방식이며 PDF 업로드 도구는 없습니다.
