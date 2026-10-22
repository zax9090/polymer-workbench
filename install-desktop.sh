#!/bin/bash
# Install the portable release for the current user. No sudo or package changes.
set -euo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="$HOME/.local/opt/polymer-lab"
MENU_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/512x512/apps"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || printf '%s/Desktop' "$HOME")"
if [[ "$DESKTOP_DIR" == "$HOME" || -z "$DESKTOP_DIR" ]]; then DESKTOP_DIR="$HOME/Desktop"; fi

remove_managed_entry() {
    if [[ -f "$1" ]] && grep -q '^X-PolymerLab-Managed=true$' "$1"; then rm -- "$1"; fi
}
if [[ "${1:-}" == "--uninstall" ]]; then
    remove_managed_entry "$MENU_DIR/polymer-lab.desktop"
    remove_managed_entry "$DESKTOP_DIR/Polymer Lab.desktop"
    if [[ -L "$BIN_DIR/polymer-lab" ]] && [[ "$(readlink "$BIN_DIR/polymer-lab")" == "$PREFIX/current/launch.sh" ]]; then rm -- "$BIN_DIR/polymer-lab"; fi
    if [[ -L "$ICON_DIR/polymer-lab.png" ]] && [[ "$(readlink "$ICON_DIR/polymer-lab.png")" == "$PREFIX/current/polymer-lab.png" ]]; then rm -- "$ICON_DIR/polymer-lab.png"; fi
    command -v update-desktop-database >/dev/null && update-desktop-database "$MENU_DIR" || true
    printf '%s\n' 'Polymer Lab 메뉴와 바로가기를 제거했습니다. 연구 데이터와 설치 파일은 보존했습니다.' "설치 파일: $PREFIX"
    exit 0
fi

SOURCE="${1:-}"
if [[ -z "$SOURCE" ]]; then
    if [[ -x "$SCRIPT_DIR/PolymerLab" ]]; then SOURCE="$SCRIPT_DIR"; else SOURCE="$SCRIPT_DIR/dist/PolymerLab"; fi
fi
SOURCE=$(CDPATH= cd -- "$SOURCE" && pwd)
if [[ ! -x "$SOURCE/PolymerLab" || ! -f "$SOURCE/.polymer-lab-bundle" || ! -f "$SOURCE/launch.sh" ]]; then
    printf '%s\n' '완성된 Linux 배포 폴더를 지정하세요: ./install-desktop.sh ./dist/PolymerLab' >&2
    exit 1
fi
if [[ -x "$SCRIPT_DIR/.venv/bin/python" && -f "$SCRIPT_DIR/workbench/paths.py" && -d "$SCRIPT_DIR/data/jobs" ]]; then
    # The source checkout knows the legacy job location; migrate before the
    # portable app creates its one-time migration record in the user data dir.
    (cd "$SCRIPT_DIR" && POLYMER_LEGACY_DATA_DIR="$SCRIPT_DIR/data/jobs" .venv/bin/python -c 'from workbench.paths import prepare_desktop_data; print("연구 데이터:", prepare_desktop_data())')
fi
mkdir -p "$PREFIX/releases" "$MENU_DIR" "$ICON_DIR" "$BIN_DIR" "$DESKTOP_DIR"
TARGET=$(mktemp -d "$PREFIX/releases/install-XXXXXXXX")
trap 'if [[ -n "${TARGET:-}" && ! -f "$TARGET/.polymer-lab-bundle" ]]; then rmdir -- "$TARGET" 2>/dev/null || true; fi' EXIT
cp -a "$SOURCE/." "$TARGET/"
ln -sfn -- "$TARGET" "$PREFIX/current"
if [[ -e "$BIN_DIR/polymer-lab" && ! -L "$BIN_DIR/polymer-lab" ]]; then
    printf '%s\n' "기존 $BIN_DIR/polymer-lab 파일은 보존했습니다. 메뉴에서 실행할 수 있습니다." >&2
else
    ln -sfn -- "$PREFIX/current/launch.sh" "$BIN_DIR/polymer-lab"
fi
ln -sfn -- "$PREFIX/current/polymer-lab.png" "$ICON_DIR/polymer-lab.png"
# Desktop Entry quoted values escape backslash, double quote, backtick and $.
desktop_quote() { local value="$1"; value="${value//\\/\\\\}"; value="${value//\"/\\\"}"; value="${value//\`/\\\`}"; value="${value//\$/\\\$}"; printf '"%s"' "$value"; }
EXEC_VALUE=$(desktop_quote "$PREFIX/current/PolymerLab")
cat > "$MENU_DIR/polymer-lab.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Polymer Lab
Name[ko]=Polymer Lab 고분자 연구실
Comment=Polymer and protein research workflows
Comment[ko]=고분자·단백질 시뮬레이션 연구 프로그램
Exec=$EXEC_VALUE
Icon=polymer-lab
Terminal=false
Categories=Science;Chemistry;
StartupWMClass=polymer-lab
StartupNotify=true
X-PolymerLab-Managed=true
EOF
chmod 644 "$MENU_DIR/polymer-lab.desktop"
cp "$MENU_DIR/polymer-lab.desktop" "$DESKTOP_DIR/Polymer Lab.desktop"
chmod +x "$DESKTOP_DIR/Polymer Lab.desktop"
if command -v gio >/dev/null; then gio set "$DESKTOP_DIR/Polymer Lab.desktop" metadata::trusted true 2>/dev/null || true; fi
command -v update-desktop-database >/dev/null && update-desktop-database "$MENU_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
printf '%s\n' 'Polymer Lab 설치 완료. 앱 메뉴 또는 바탕화면에서 실행하세요.' "실행 파일: $PREFIX/current/PolymerLab" "설치 폴더: $TARGET"
