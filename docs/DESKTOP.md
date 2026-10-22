# Polymer Lab 데스크톱 프로그램

Polymer Lab은 고분자·단백질 연구 워크플로를 한 창에서 실행하는 로컬 데스크톱 앱입니다. Qt 창 안에서 3D 구조, 계산 설정, 진행 상태와 연구 결과를 확인합니다. 일반 브라우저를 따로 열 필요가 없습니다.

## 바로 실행

Linux 배포 압축 파일 `PolymerLab-<버전>-linux-x86_64.tar.gz`를 풀고 `PolymerLab/launch.sh` 또는 `PolymerLab/PolymerLab`를 실행합니다. Python, 가상 환경이나 개발용 서버를 따로 설치할 필요가 없습니다. `_internal` 폴더도 함께 보관해야 합니다.

```bash
tar -xzf PolymerLab-0.3.0-linux-x86_64.tar.gz
cd PolymerLab
./launch.sh
```

앱 메뉴와 바탕화면에 추가하려면 압축을 푼 폴더에서 다음 명령을 한 번 실행합니다. 현재 사용자에게만 설치하며 관리자 권한을 쓰지 않습니다.

```bash
./install-desktop.sh
```

설치 위치는 `~/.local/opt/polymer-lab/`, 메뉴 이름은 **Polymer Lab**입니다. `~/.local/bin/polymer-lab` 실행 명령도 추가됩니다. 일부 파일 관리자에서는 바탕화면 바로가기를 처음 열 때 실행 허용을 선택해야 합니다.

메뉴와 바로가기를 제거할 때는 `./install-desktop.sh --uninstall`을 실행합니다. 연구 데이터와 설치 폴더는 보존합니다. 설치 파일까지 정리하려면 앱을 종료한 후 `~/.local/opt/polymer-lab/` 폴더를 파일 관리자로 삭제합니다.

## 포함된 기능

| 구성 | 배포 파일에 포함된 범위 |
|---|---|
| 데스크톱 창 | PySide6/Qt WebEngine, 앱 아이콘, 다운로드·폴더 열기 기능 |
| 고분자 계산 | NumPy, RDKit, LAMMPS, RadonPy, pysimm, MDAnalysis |
| 모델 준비 | PACKMOL, polyply, martinize2, 관련 힘장 및 템플릿 데이터 |
| 연구 데이터 | DuckDB 색인, Parquet 스냅샷, CSV 측정값·기기 곡선 검토, PDF 페이지 텍스트 추출 |
| 분석과 예측 | MDAnalysis 피처, pymbar 평형 후보 구간, scikit-learn 회귀·재료 그룹 교차검증 |
| 실험계획 | 유한 후보·예산을 사용하는 캠페인, scikit-learn 및 BayBE/BoTorch CPU 백엔드 |
| 시각화 | 로컬 3Dmol.js, 구조·궤적 화면 |
| AlphaFold | 기존 AlphaFold DB 예측 가져오기, 구조·신뢰도 분석, 추론용 입력 생성 |
| REA | 저장소 등록·해시·분석 근거 자료 |

AlphaFold/ColabFold 신규 추론 환경과 모델 가중치, GROMACS, HOOMD-blue, Psi4, AmberTools, OpenFF는 포함하지 않습니다. 기존 앱과 동일하게 외부 환경을 연결하는 경로를 제공합니다. Martini 경로에서는 명시한 이차구조를 사용한 단백질 조대화와 지원되는 polyply 고분자 구성요소 준비를 실행합니다. GROMACS를 설치하면 같은 경로의 MD 실행 모드가 용매화·중화한 Martini 3 혼합계의 단계별 평형화와 짧은 생산 계산을 수행합니다. Kali/Debian/Ubuntu에서는 `sudo apt install gromacs` 후 앱을 다시 시작하면 `/usr/bin/gmx`를 감지하며, 다른 위치는 `GROMACS_EXEC`로 지정합니다. Martini 3.0.0 기본 파라미터(약 16 MB, Apache-2.0)는 첫 실행 때 고정 커밋에서 내려받아 SHA-256을 확인한 뒤 데이터 폴더의 `forcefields/`에 보관합니다. 오프라인 환경에서는 같은 파일을 `MARTINI3_FF_DIR`로 지정합니다. Amber/OpenFF 경로는 필요한 입력과 도구를 확인하고 준비 계획을 생성합니다.

합성 고분자 물성과 단백질–고분자 지표를 함께 저장할 수 있습니다. 물성 예측에는 검토된 실험·문헌 학습 데이터가 필요하며, 제공되는 앱에 학습 완료 물성 모델이 들어 있지는 않습니다. 짧은 MD 피처나 통계적 평형 후보 구간만으로 실제 벌크 물성의 정확도와 물리적 평형을 보장하지 않습니다. BayBE는 CPU 전용 PyTorch를 사용하며 CUDA 라이브러리나 GPU는 필요하지 않습니다.

외부 `RADONPY_PYTHON` 또는 `PYSIMM_PYTHON` 환경을 연결할 때는 그 환경에 맞는 `LAMMPS_EXEC`도 함께 지정해야 합니다. 외부 Python과 번들의 LAMMPS/MPI 라이브러리를 섞으면 로더 충돌이 생길 수 있습니다. 기본 번들 경로는 추가 설정 없이 사용할 수 있습니다.

REA의 `node_modules` 개발 환경은 포함하지 않습니다. 배포 앱은 이미 작성된 분석 근거를 보여 주며, REA 자체를 새로 실행하는 기능이 있다고 표시하지 않습니다. 추가 분석 작업은 소스 체크아웃의 개발 환경에서 진행합니다.

## 저장과 실행 방식

앱 창이 필요한 로컬 서버를 직접 시작하고 종료합니다. 통신은 이 컴퓨터의 루프백 주소에서만 이루어집니다. 계산은 별도 작업 프로세스로 실행하며, 프로세스는 배포 파일에 포함된 Python 런타임을 사용합니다.

연구 데이터의 기본 위치는 `~/.local/share/polymer-lab/jobs/`입니다. `XDG_DATA_HOME`, `POLYMER_APP_DATA_DIR` 또는 작업 폴더를 직접 지정하는 `POLYMER_DATA_DIR` 환경 변수로 변경할 수 있습니다. 소스 체크아웃에서 설치 스크립트를 실행하면 기존 `data/jobs/`의 완료된 작업을 처음 한 번 복사하며 원본은 보존합니다. 배포 폴더만 있는 경우 기존 작업 위치를 자동 검색하지 않습니다. 필요한 경우 최초 실행 전에 `POLYMER_LEGACY_DATA_DIR`로 기존 작업 폴더를 지정합니다. 정확한 현재 저장 위치는 앱의 데이터 폴더 메뉴에서 열 수 있습니다.

연구 색인, 확인한 측정값, 학습 모델 및 캠페인은 작업 폴더 옆의 `research/`에 저장합니다. 앱 업데이트는 이 데이터를 삭제하지 않습니다. 스냅샷 내보내기는 일관된 Parquet 파일과 해시를 생성합니다. 이미 연결한 Google Drive/rclone 동기화 폴더를 백업 경로로 설정하면 완성된 스냅샷을 복사하며, 실행 중인 DuckDB 파일은 동기화하지 않습니다. 앱 자체가 Google 계정에 로그인하거나 Drive를 연결하지는 않습니다.

Qt WebEngine의 샌드박스를 기본 설정 그대로 사용합니다. 배포 실행기는 샌드박스 해제 플래그를 추가하지 않습니다. 데스크톱 로그인 세션과 운영체제의 Qt 실행 의존 라이브러리가 필요합니다.

## 지원 환경과 배포 파일

현재 배포 파일은 **Linux x86_64**에서 생성하고 검증한 빌드입니다. Windows `.exe` 또는 macOS `.app`이 아닙니다. 다른 운영체제는 해당 운영체제에서 별도 빌드해야 합니다.

빌드 호스트는 Debian/Kali 계열, glibc 2.43입니다. 더 오래된 Linux 배포판과의 호환성을 보장하지 않습니다. Qt가 요구하는 X11/Wayland, NSS, EGL/OpenGL, 오디오 등 시스템 라이브러리도 필요합니다. 구체적인 빌드 정보는 `build-manifest.json`, 파일 무결성 해시는 배포 폴더의 `SHA256SUMS`에 기록됩니다.

`.deb`도 제공하는 경우 패키지 내용은 `/opt/polymer-lab/`에 설치되도록 구성됩니다. 관리자 권한이 필요한 시스템 설치용 파일이며, 현재 사용자 설치에는 위의 `install-desktop.sh`가 더 간단합니다. 빌드 과정은 `.deb`를 생성할 뿐 시스템에 설치하지 않습니다.

## 소스에서 실행·다시 빌드

개발 환경에는 과학 패키지 외에 `PySide6`, `PySide6-WebEngine`, `PyInstaller`가 필요합니다. 버전에 따라 WebEngine이 별도 휠로 배포됩니다. `requirements.lock.txt`는 기본 연구 기능을 고정하며, `requirements.desktop.lock.txt`가 BayBE와 CPU 전용 `torch==2.14.1+cpu` 및 데스크톱 빌드 도구를 추가합니다. CPU 휠은 PyTorch 공식 인덱스에서 가져오도록 설정되어 있습니다. 빌드 스크립트는 CUDA 빌드를 발견하면 중단합니다.

```bash
.venv/bin/python -m pip install -r requirements.desktop.lock.txt
./launch-desktop.sh
.venv/bin/python scripts/build_desktop.py --deb
```

빌드 결과는 `dist/PolymerLab/`, 압축 파일·Debian 패키지·체크섬은 `releases/`에 생성됩니다. 빌드 로그는 `build/desktop-build.log`입니다. 생성된 번들에는 라이브러리 버전과 라이선스 파일을 함께 넣습니다. LGPL 등 구성 요소의 조건은 `THIRD-PARTY-NOTICES.txt` 및 `_internal/licenses/`를 확인하세요.

이 빌드는 [PyInstaller의 공식 패키지 수집 방식](https://www.pyinstaller.org/en/stable/hooks.html)을 사용합니다. 원본 라이브러리와 힘장 자료의 출처는 앱의 REA 근거 화면과 `_internal/evidence/repositories.json`에서 확인할 수 있습니다.
