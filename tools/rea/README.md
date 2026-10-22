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

# 개별 REA 명령
node tools/rea/node_modules/rea-agents/scripts/rea.mjs import-reference-source evidence/sources/radonpy --detail summary --format json
node tools/rea/node_modules/rea-agents/scripts/rea.mjs analyze-javascript-application evidence/sources/3dmol --format json
```

`catalog.json`은 공식 저장소, 고정 커밋, 읽은 파일, Git blob SHA를 기록합니다. `contracts.json`은 직접 검토한 소스의 입력·출력·연결 지점과 검색 가능한 근거 문자열을 기록합니다. 감사 스크립트는 문자열이 원본에 실제 존재하는지 검증해 행 번호와 영구 링크를 생성합니다.

공식 출처: [REA 저장소](https://github.com/morluto/rea), [공식 npm 패키지](https://www.npmjs.com/package/rea-agents).
