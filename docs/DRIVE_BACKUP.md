# Google Drive 자동 백업

`main` 브랜치에 새 커밋이 올라올 때마다 GitHub Actions(`.github/workflows/drive-backup.yml`)가 그 커밋의 파일 전체를 zip 하나로 묶어 Google Drive에 올립니다.

- **올라가는 파일**
  - `PolymerLab-backups/polymer-workbench-<커밋 시각 UTC>-<커밋 8자리>.zip`: 커밋마다 하나씩 쌓입니다.
  - `PolymerLab-backups/polymer-workbench-latest.zip`: 매번 최신 커밋으로 덮어씁니다.
- **내용**: 그 커밋에 들어 있는 파일입니다(`git archive`). git 기록(history)은 들어가지 않으며, 기록은 GitHub에 남아 있습니다.
- **업로드 도구**: rclone v1.75.1입니다. Go 체크섬 데이터베이스(sum.golang.org)로 검증되는 태그 모듈에서 빌드하고, 빌드한 파일은 Actions 캐시에 보관합니다.
- **권한 범위**: rclone에 `drive.file` 범위만 허락합니다.
  - rclone이 직접 만든 폴더와 파일만 보고 쓸 수 있습니다.
  - Drive의 다른 파일은 읽지도 지우지도 못합니다.

## 한 번만 하는 설정

1. 브라우저가 있는 컴퓨터에 rclone을 설치합니다: https://rclone.org/downloads/
2. 터미널에서 다음을 실행합니다.
   ```sh
   rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlLmZpbGUifQ"
   ```
   - 두 번째 인자는 `{"scope":"drive.file"}`을 base64로 쓴 것입니다. 이 인자를 빼면 rclone이 Drive 전체 권한을 요청합니다.
   - 브라우저가 열리면 백업을 받을 Google 계정으로 로그인하고 허용합니다.
   - 터미널의 `Paste the following into your remote machine --->` 줄과 `<---End paste` 줄 사이에 `eyJ0b2tlbiI6`으로 시작하는 긴 한 줄이 나옵니다. 이 줄이 토큰을 담은 rclone 응답입니다.
3. GitHub 저장소에서 **Settings → Secrets and variables → Actions → New repository secret**으로 이동해 비밀 값을 만듭니다.
   - Name: `RCLONE_DRIVE_TOKEN`
   - Secret: 2단계의 긴 한 줄. 위아래 안내 줄까지 함께 붙여 넣어도 됩니다. 예전 rclone이 출력하던 `{"access_token":...}` 형식도 받습니다.
4. **Actions → drive-backup → Run workflow**로 바로 시험하거나, 다음 `main` 커밋을 기다립니다. 내 드라이브에 `PolymerLab-backups` 폴더가 생깁니다.

토큰이 없으면 워크플로는 실패하지 않고 "RCLONE_DRIVE_TOKEN is not set" 알림만 남깁니다. 비밀 값이 rclone 응답이 아니면 업로드 단계가 그 이유를 오류로 남기고 실패합니다. 비밀 값 자체는 로그에 찍지 않습니다(`scripts/drive_backup_config.py`).

"the Drive token has no refresh_token" 오류가 나면 Google이 갱신용 토큰 없이 응답한 것입니다. 이전에 같은 계정으로 rclone을 허용한 적이 있으면 이렇게 될 수 있습니다. 이 상태로는 한 시간 뒤 업로드가 끊깁니다. [서드파티 연결](https://myaccount.google.com/permissions)에서 rclone 접근을 해제한 뒤 2–3단계를 다시 하면 됩니다.

## 선택 설정

**Settings → Secrets and variables → Actions → Variables**에서 지정합니다.

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `DRIVE_BACKUP_FOLDER` | `PolymerLab-backups` | 백업을 넣을 Drive 폴더 경로 |
| `DRIVE_BACKUP_KEEP_DAYS` | 없음(모두 보관) | 이 일수(1–9999)보다 오래된 날짜별 zip을 Drive 휴지통으로 옮깁니다. `latest`는 지우지 않습니다 |

## 알아 둘 점

- `drive.file` 범위라서 rclone이 만들지 않은 폴더는 보이지 않습니다. 같은 이름의 폴더를 미리 만들어 두었다면 같은 이름의 폴더가 하나 더 생깁니다.
- 토큰이 무효가 되면 2–3단계를 다시 하면 됩니다. 토큰은 다음 경우에 무효가 됩니다.
  - Google 계정의 [서드파티 연결](https://myaccount.google.com/permissions)에서 rclone 접근을 해제했을 때
  - 6개월 동안 한 번도 쓰이지 않았을 때
- 서비스 계정은 쓰지 않았습니다. 서비스 계정에는 저장 용량이 없어서 개인 계정의 내 드라이브에 파일을 만들 수 없고, Google Workspace의 공유 드라이브에만 올릴 수 있습니다.
- 연구 데이터(측정값·계산 기록)는 GitHub에 없으므로 이 백업에 들어가지 않습니다. 연구 데이터도 Drive에 두려면 다음과 같이 합니다.
  - [Google Drive 데스크톱](https://www.google.com/drive/download/)이 동기화하는 폴더의 전체 경로를 앱의 **연구 데이터 → 동기화 폴더 → 기존 폴더의 전체 경로**에 넣고 저장합니다.
  - 그 뒤 **스냅샷 만들기**를 누를 때마다 완성된 스냅샷이 그 폴더로 복사되고, Drive 데스크톱이 그것을 올립니다. 실행 중인 DB 파일은 복사하지 않습니다.
