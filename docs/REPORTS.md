# 연구 보고서

`workbench/reports.py`는 저장된 기록 하나(DoE 기록, 벌크 연구 흐름) 또는 연구 데이터 전체(마스터 표)를 사람이 읽는 보고서 폴더로 만듭니다. 보고서는 기록의 숫자와 검토 표시를 옮기기만 합니다. 모델을 다시 맞추거나, 값을 검토하거나, 추천을 확인된 것으로 바꾸지 않습니다.

## 구성

| 파일 | 내용 |
|---|---|
| `summary.md` | 한 쪽 요약(한국어), 계산 주의사항 원문, 출처 기록과 SHA-256 |
| `report.html` | 같은 내용과 SVG 그림. 스크립트 없음. 앱은 `default-src 'none'` 콘텐츠 보안 정책으로 제공 |
| `figures/*.svg`, `*.png` | matplotlib 3.11.2. SVG는 글자를 글자로 유지(`svg.fonttype=none`), PNG는 160 dpi |
| `tables/*.csv` | UTF-8 BOM, CRLF. 실수는 `repr`(왕복 가능한 최단 표기), `= + - @ 탭 CR`로 시작하는 글자에는 `'`를 붙임(CSV 수식 주입 방지) |
| `tables/report.xlsx` | 모든 표를 시트로. 자체 ECMA-376 SpreadsheetML 작성기(인라인 문자열, 숫자, 논리값, 굵은 머리글, 첫 행 고정) |
| `manifest.json` | 파일별 크기·SHA-256·MD5, 출처 기록 ID·해시, `reports.py` 해시, matplotlib 버전, 경고 |

색은 흰 바탕용 단일 색상(파랑) 순차 척도를 씁니다(낮음 = 밝음, 높음 = 어두움). 유의 여부·추천점처럼 두 상태만 나누는 표시는 진한 파랑과 회색, 기준선은 주황 점선입니다.

## 검증 (2026-10-23)

| 항목 | 결과 |
|---|---|
| xlsx 작성기와 openpyxl 3.1.5 대조 | 무작위 시트 6개, 셀 1 881개(유니코드, XML 특수 문자, 제어 문자, 40 000자 문자열, 5e-324·1.797e308·−0.0, 논리값, JSON)가 openpyxl로 읽은 값과 모두 같음. 시트 이름 정리·중복 처리, 첫 행 고정 확인. 보고서 4개(반응표면·혼합물·만족도·설계)의 모든 시트가 같은 표의 CSV와 같음 (`scripts/crosscheck_report_xlsx_openpyxl.py`) |
| 저장소 테스트의 xlsx 판독 | openpyxl 없이 ZIP과 XML을 직접 읽어 값·형식·시트 이름을 확인 |
| 숫자 보존 | 계수 CSV의 추정값·p값이 기록의 값과 비트 단위로 같음(`float(repr(x)) == x`). 만족도 표의 개별 d도 같음 |
| 다항식 | 보고서의 부호화 다항식으로 계산한 추천점 예측값이 기록의 예측값과 상대 10⁻¹² 안에서 같음 |
| 검토 표시 | 연구 흐름 보고서의 모든 반응값이 `검토 = FALSE`, 마스터 표의 물성·출처 종류·검토별 개수가 저장한 측정값과 같음. 보고서를 만들어도 측정값이 생기지 않음 |
| 불변성 | 원본 기록의 SHA-256이 보고서 전후로 같음. ZIP 안 파일의 해시가 manifest와 같음 |
| 이름 처리 | `<b>&`, `$`가 들어간 인자 이름이 HTML에서 이스케이프되고 matplotlib 수식으로 해석되지 않음 |
| Drive 업로드 | 가짜 rclone 원격(로컬 폴더 복사, 실제 MD5 목록)으로 모든 파일 일치 시에만 `uploaded`·`verified=true`. 전송 중 한 파일이 바뀌면 502, 상태는 `local_ready` 유지. 만든 뒤 로컬 파일이 바뀌면 rclone을 부르지 않고 422 |
| 화면 | Chromium 1280·390 px에서 보고서 HTML 6종(DoE 설계·반응표면·혼합물·만족도, 연구 흐름, 마스터 표) 가로 넘침·콘솔 오류 없음. 연구 흐름 보고서의 등고선이 알려진 최적점(317.5 K, 0.72 g/cm³)에 추천 표시. 앱 1360·390 px에서 DoE 결과와 마스터 표의 보고서 만들기 → 보기(200, CSP 헤더) → Drive 미설정 안내 |

## 한계

- 실제 Google Drive로의 업로드는 이 개발 환경에 rclone과 사용자 원격이 없어 확인하지 못했습니다. 검증 경로는 `drive_backup._rclone`과 같은 명령(`copy --immutable`, `lsjson --recursive --files-only --hash`)을 쓰는 가짜 원격입니다.
- PNG의 한글은 설치된 글꼴에 따라 달라집니다. 이 환경에는 한국어 전용 글꼴이 없어 WenQuanYi Zen Hei로 그렸습니다. SVG와 HTML은 보는 컴퓨터의 글꼴을 씁니다.
- 엑셀의 시트당 행 제한(머리글 포함 1 048 576행)을 넘는 표는 `report.xlsx`에 앞부분만 넣고 경고를 남깁니다. 전체 행은 같은 이름의 CSV에 있습니다.
- 연구 흐름 보고서는 마지막 반응표면 분석만 그림으로 보여 줍니다. 이전 분석은 연구 흐름 기록에 그대로 있습니다.

## 재실행

```sh
.venv/bin/python -m pytest -q tests/test_reports.py
.venv/bin/python -m pip install --target /tmp/xlsxlib --no-deps openpyxl==3.1.5 et_xmlfile==2.0.0
PYTHONPATH=/tmp/xlsxlib .venv/bin/python scripts/crosscheck_report_xlsx_openpyxl.py . data/research/reports
```

## 워크플로우 재현 입력

워크플로우 보고서의 `inputs/`에는 흐름 스냅샷, 원본 설계 JSON/CSV, 행별 모든 시도·확인·Tg 원래 셀의 설정/seed/역할/상태, 저장된 결과 요약을 담습니다. 목표 계약과 승인한 자동 실행 계획은 저장돼 있을 때 포함하며 각 해시를 확인합니다. 결과 요약의 힘장·프로토콜·수렴·경고는 원래 값을 유지하고, 원래 기록에 엔진 버전이 없으면 `null/not_recorded`로 표시합니다. 현재 설치 버전으로 과거 버전을 채우지 않습니다.

입력은 기존 manifest·ZIP 해시 대상이며 합계 20 MiB를 넘으면 게시하지 않습니다. 원본 해시 변경·심볼릭 링크는 거부하고, 사라진 과거 자료는 경고와 누락으로 보존합니다. 원시 MD 궤적·엔진·인증 설정을 포함하지 않고 자동 재실행도 하지 않습니다. 이 묶음으로 물리적 타당성이나 비트 단위 동일 결과를 보장하지 않습니다.
