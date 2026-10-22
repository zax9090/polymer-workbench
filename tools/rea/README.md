# Local REA toolkit

이 폴더에는 공식 `rea-agents@6.7.0`을 프로젝트 내부에 설치했습니다. 사용자 Codex/MCP 설정은 변경하지 않았습니다. 최초 환경의 Node.js는 `v24.21.0`이며 npm이 없어 공식 npm `12.2.1`을 이 폴더 안에서만 사용했습니다.

```sh
python3 tools/rea/bootstrap.py
python3 scripts/audit_repos.py --run-rea
```

`package-lock.json`은 REA 및 의존성 버전·무결성 정보를 고정합니다. 부트스트랩은 npm tarball의 registry SRI를 확인하며 설치 스크립트와 optional dependencies를 생략합니다. 이 구성은 이번 정적 분석에 충분합니다. REA가 요구하는 지원 Node.js 버전이 필요합니다(이번 실행은 Node 24).

```sh
# 소스 파일 무결성 확인 및 요약 재생성; REA를 다시 실행하지 않음
python3 scripts/audit_repos.py

# 보관된 커밋의 정확한 원본 소스를 복원하고 분석 재실행
python3 scripts/audit_repos.py --refresh-sources --run-rea

# 확장 감사(BayBE·DuckDB·Martini 힘장 등): 고정 커밋 원본을 받아 검증하고 지정 저장소만 REA 재실행
python3 scripts/audit_extensions.py --fetch-pinned --run-rea --only martini-forcefields

# 개별 REA 명령
node tools/rea/node_modules/rea-agents/scripts/rea.mjs import-reference-source evidence/sources/radonpy --detail summary --format json
node tools/rea/node_modules/rea-agents/scripts/rea.mjs analyze-javascript-application evidence/sources/3dmol --format json
```

## Claude Code 연결

프로젝트 루트의 `.mcp.json`이 REA를 `rea` MCP 서버로 등록하고, `.claude/settings.json`이 이 서버를 승인합니다. 서버는 `tools/rea/mcp-server.sh`로 시작하며 `node_modules`가 없으면 위 부트스트랩을 먼저 실행합니다. 설치 출력은 stderr로만 보내 MCP stdio 채널을 오염시키지 않습니다. 사용자 전역 설정(`~/.claude.json`)은 바꾸지 않으며, `rea setup`도 실행하지 않았습니다.

클라우드 세션에서는 `.claude/hooks/session-start.sh`가 `.venv`(`requirements.lock.txt`)와 REA를 설치하고 테스트에 필요한 `PATH`·`LD_LIBRARY_PATH`를 세션 환경에 기록합니다. REA 6.7.0에 포함된 작업 지침은 `.claude/skills/reverse-engineer-anything/`에 MIT 라이선스 고지와 함께 복사했습니다. 이 지침에 따라 완전한 소스 저장소의 일반 구조 분석에는 REA를 쓰지 않고, 배포 바이너리·패키지·실행 근거가 필요한 경우에 사용합니다.

```sh
# MCP 서버 직접 확인: initialize 응답의 serverInfo가 rea 6.7.0
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' | tools/rea/mcp-server.sh
```

클라우드 컨테이너의 Node.js 22.22.0에서는 npm 12.2.1이 지원 범위(`^22.22.2`) 경고를 출력하지만 설치와 REA 실행(`^22.19.0`)은 정상 완료됩니다.

`catalog.json`은 공식 저장소, 고정 커밋, 읽은 파일, Git blob SHA를 기록합니다. `contracts.json`은 직접 검토한 소스의 입력·출력·연결 지점과 검색 가능한 근거 문자열을 기록합니다. 감사 스크립트는 문자열이 원본에 실제 존재하는지 검증해 행 번호와 영구 링크를 생성합니다.

공식 출처: [REA 저장소](https://github.com/morluto/rea), [공식 npm 패키지](https://www.npmjs.com/package/rea-agents).
