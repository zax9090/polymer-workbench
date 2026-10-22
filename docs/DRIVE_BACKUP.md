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

## 본인 Google 클라이언트 ID (2026년 안에 필요)

위 설정은 rclone이 여러 사용자와 함께 쓰는 Google 클라이언트를 씁니다. rclone은 이 공용 클라이언트를 2026년 중에 끝낸다고 알렸습니다. 끝나면 업로드가 실패하니, 본인 클라이언트를 만들어 두는 것이 좋습니다. 한 번만 하면 됩니다.

1. https://console.cloud.google.com/ 에서 새 프로젝트를 만듭니다(이름은 자유).
2. **API 및 서비스 → 라이브러리**에서 **Google Drive API**를 찾아 **사용**을 누릅니다.
3. **Google 인증 플랫폼(OAuth 동의 화면)**을 설정합니다.
   - 사용자 유형: **외부**
   - 앱 이름과 이메일만 넣으면 됩니다.
   - **대상 → 앱 게시(프로덕션으로 푸시)**를 누릅니다. **테스트** 상태로 두면 토큰이 7일 뒤 만료되어 백업이 멈춥니다.
   - `drive.file`은 민감하지 않은 범위라 Google 심사 없이 게시됩니다.
4. **클라이언트 → 클라이언트 만들기**에서 애플리케이션 유형을 **데스크톱 앱**으로 고릅니다. 클라이언트 ID(`...apps.googleusercontent.com`)와 보안 비밀(`GOCSPX-...`)이 나옵니다.
5. rclone이 있는 컴퓨터의 터미널에서 다음을 실행합니다. 따옴표 안에 4단계의 두 값을 넣습니다.
   ```sh
   ID='여기에 클라이언트 ID'
   SECRET='여기에 보안 비밀'
   BLOB="$(printf '{"client_id":"%s","client_secret":"%s","scope":"drive.file"}' "$ID" "$SECRET" | base64 -w0 | tr -d '=')"
   rclone authorize drive "$BLOB"
   ```
   - 브라우저에서 허용하면 앞의 2단계처럼 `eyJ0b2tlbiI6`으로 시작하는 긴 줄이 나옵니다.
   - "Google에서 확인하지 않은 앱" 화면이 나오면 **고급 → (앱 이름)(으)로 이동**을 누릅니다. 본인이 만든 앱이라 괜찮습니다.
6. GitHub **Settings → Secrets and variables → Actions**에서 비밀 값 세 개를 넣습니다.
   - `DRIVE_CLIENT_ID`: 클라이언트 ID
   - `DRIVE_CLIENT_SECRET`: 보안 비밀
   - `RCLONE_DRIVE_TOKEN`: 5단계의 새 긴 줄. 기존 값을 바꿉니다.

토큰은 그 토큰을 받은 클라이언트에서만 쓸 수 있습니다. 그래서 세 값은 함께 바꿔야 합니다. 클라이언트 ID와 보안 비밀 중 하나만 넣으면 업로드 단계가 오류를 냅니다.

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
