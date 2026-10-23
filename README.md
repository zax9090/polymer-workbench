# Polymer Lab

고분자·단백질 구조와 분자 시뮬레이션, 실험계획법(DOE)·조건 최적화, 연구 데이터 관리를 위한 한국어 워크벤치입니다. Linux 데스크톱과 로컬 웹 화면을 제공하며 Rust 수치 커널, 공개 계산 엔진, REA 감사 근거를 연결합니다. 소스·설명서·저장된 연구 결과를 본인 Google Drive에 함께 백업할 수 있습니다.

**[사용설명서](docs/MANUAL.md)** — 설치, 계산 경로, RDF·MSD·자유 부피·Widom, DOE, 데이터 검토, Google Drive 설정과 문제 해결을 한 문서에 정리했습니다. 앱에서도 **설명서** 메뉴로 같은 문서를 엽니다.

## 빠른 시작

Python 3.12 이상과 Rust `cargo`를 설치한 뒤 저장소 폴더에서 실행합니다.

```sh
bash setup.sh
./start.sh
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다. 외부 계산 엔진은 앱의 **계산 엔진**에서 확인합니다. Google Drive 전체 백업은 본인 rclone 원격을 설정한 뒤 `POLYMER_DRIVE_REMOTE`로 원격 이름을 전달합니다. 자세한 절차는 사용설명서에 있습니다.

Materials Studio·Minitab에서 쓰이는 일부 방법을 공개 문헌과 오픈소스로 구현합니다. 상용 제품 전체 기능이나 동일한 결과를 보장하지 않습니다. `completed`는 경로 실행 완료를 뜻하며 과학적 타당성은 수렴·출처·검증 결과로 판단합니다.

## 개발과 검증

```sh
.venv/bin/python -m pytest -q -rs
cargo test --release --locked --no-default-features --manifest-path native/Cargo.toml
.venv/bin/python -m pip install ./native
```

전체 테스트에는 선택 엔진이 필요한 검사가 있으며 미설치 시 건너뛴 이유를 표시합니다. Rust 코드를 바꾸면 커널을 다시 설치하고 앱을 재시작합니다. 의존성은 `requirements.lock.txt`와 `native/Cargo.lock`으로 고정합니다.

개발 작업 규칙은 [AGENTS.md](AGENTS.md), 과학 계산 검증은 [검증 기록](docs/VALIDATION.md)과 [Rust 커널 기록](docs/RUST_KERNELS.md), 외부 도구 출처는 [REA 감사 기록](docs/REA_AUDIT.md)에 있습니다. `docs/`의 나머지 기술 문서와 `evidence/`는 구현·검증 근거로 보존합니다.

제3자 코드·도구는 각 원본 라이선스를 따릅니다. 3Dmol.js의 BSD 고지는 `workbench/static/vendor/3Dmol-LICENSE.txt`, 데스크톱 배포 패키지의 라이선스·버전은 `_internal/licenses/`에 보관합니다.
