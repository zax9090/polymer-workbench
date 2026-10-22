"""Native Polymer Lab window and its private, supervised computation service.

Qt is imported only by ``run_desktop``: the backend and command-line workers never
initialize Chromium, and the supervisor can be exercised without a display.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from . import __version__
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


APP_NAME = "Polymer Lab"
ACTIVE_STATUSES = {"queued", "running", "cancelling"}


def command_for_mode(mode: str, *arguments: str) -> list[str]:
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "workbench"]
    return [*prefix, mode, *map(str, arguments)]


def is_local_url(url: str, origin: str) -> bool:
    """Match the complete private origin; other loopback ports are not trusted."""
    try:
        target, expected = urlsplit(url), urlsplit(origin)
        return (target.scheme, target.hostname, target.port) == (expected.scheme, expected.hostname, expected.port) and not target.username and not target.password
    except ValueError:
        return False


def is_external_url(url: str) -> bool:
    try:
        value = urlsplit(url)
        return value.scheme == "https" and bool(value.hostname) and not value.username and not value.password
    except ValueError:
        return False


class BackendSupervisor:
    """Own exactly one loopback backend. Readiness is published after ASGI startup."""

    def __init__(self, data_dir: Path, state_dir: Path | None = None):
        self.data_dir = Path(data_dir).resolve()
        self.state_dir = Path(state_dir or self.data_dir.parent).resolve()
        self.process: subprocess.Popen | None = None
        self.origin: str | None = None
        self.token = secrets.token_urlsafe(32)
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._log = None
        self.ready_file: Path | None = None
        self.started_at = 0.0

    def start(self):
        from .paths import resource_root, runtime_environment
        if self.process is not None:
            raise RuntimeError("Backend already started")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="polymer-session-")
        self.ready_file = Path(self._temporary.name) / "ready.json"
        logs = self.state_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self._log = (logs / "desktop-backend.log").open("a", encoding="utf-8")
        env = runtime_environment()
        env.update(POLYMER_DESKTOP_TOKEN=self.token, POLYMER_DATA_DIR=str(self.data_dir),
                   POLYMER_PARENT_PID=str(os.getpid()), PYTHONUNBUFFERED="1")
        self.started_at = time.monotonic()
        try:
            self.process = subprocess.Popen(command_for_mode("--server", "--port", "0", "--ready-file", str(self.ready_file), "--data-dir", str(self.data_dir)),
                cwd=resource_root(), env=env, stdout=self._log, stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"))
        except BaseException:
            self.cleanup()
            raise
        return self

    def poll_ready(self) -> str | None:
        if not self.process:
            raise RuntimeError("Backend has not been started")
        if self.process.poll() is not None:
            raise RuntimeError(f"연구 서비스가 종료되었습니다 (코드 {self.process.returncode}). 로그: {self.state_dir / 'logs' / 'desktop-backend.log'}")
        if self.origin:
            return self.origin
        if not self.ready_file or not self.ready_file.exists():
            return None
        try:
            ready = json.loads(self.ready_file.read_text(encoding="utf-8"))
            port = int(ready["port"])
            if not 1 <= port <= 65535 or ready["pid"] != self.process.pid:
                raise ValueError("Unexpected backend readiness record")
            self.origin = f"http://127.0.0.1:{port}"
            return self.origin
        except (OSError, json.JSONDecodeError):
            return None

    def request(self, path: str, *, payload=None, timeout=2):
        if not self.origin:
            raise RuntimeError("Backend is not ready")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("A local API path is required")
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(self.origin + path, data=data, headers={"X-Polymer-Token": self.token, "Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)

    def active_jobs(self) -> list[dict]:
        jobs = []
        for metadata in self.data_dir.glob("*/job.json"):
            try:
                job = json.loads(metadata.read_text(encoding="utf-8"))
                if isinstance(job, dict) and job.get("status") in ACTIVE_STATUSES:
                    jobs.append(job)
            except (OSError, ValueError):
                continue
        return jobs

    def request_stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def stop(self, timeout=15):
        self.request_stop()
        if self.process:
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.cleanup()

    def cleanup(self):
        if self._log:
            self._log.close()
            self._log = None
        if self._temporary:
            self._temporary.cleanup()
            self._temporary = None


def run_desktop(data_dir=None) -> int:
    """Launch the installed application. No system browser or public port is used."""
    try:
        from PySide6.QtCore import QLockFile, QSettings, QStandardPaths, Qt, QTimer, QUrl
        from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow,
                                      QMessageBox, QStackedWidget, QVBoxLayout, QWidget)
        from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                           QWebEngineSettings, QWebEngineUrlRequestInterceptor)
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError as exc:
        print(f"데스크톱 구성 요소를 찾을 수 없습니다: {exc}\n설치: python -m pip install '.[desktop]'", file=sys.stderr)
        return 1

    from .paths import prepare_desktop_data, resource_root, user_data_root
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("PolymerLab")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("polymer-lab")
    icon_path = resource_root() / "assets" / "polymer-lab.png"
    if not icon_path.exists():
        icon_path = resource_root() / "assets" / "polymer-lab.svg"
    app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyle("Fusion")

    state_dir = Path(data_dir).resolve().parent if data_dir else user_data_root()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(state_dir / "desktop.lock"))
    lock.setStaleLockTime(0)
    socket_name = "polymer-lab-" + hashlib.sha256(str(state_dir).encode()).hexdigest()[:20]
    if not lock.tryLock(100):
        client = QLocalSocket()
        client.connectToServer(socket_name)
        if client.waitForConnected(1000):
            client.write(b"activate")
            client.waitForBytesWritten(1000)
            client.disconnectFromServer()
        else:
            QMessageBox.information(None, APP_NAME, "Polymer Lab이 이미 실행 중입니다. 기존 창을 확인하세요.")
        return 0
    try:
        jobs_dir = Path(data_dir).resolve() if data_dir else prepare_desktop_data()
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"연구 폴더를 준비하지 못했습니다.\n{exc}")
        lock.unlock()
        return 1
    backend = BackendSupervisor(jobs_dir, state_dir)

    class LocalRequests(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            url = info.requestUrl().toString()
            if is_local_url(url, backend.origin or ""):
                info.setHttpHeader(b"X-Polymer-Token", backend.token.encode())
            elif info.requestUrl().scheme() in {"http", "https", "file", "ftp"}:
                # Scientific downloads are performed by the backend; the renderer
                # uses bundled assets and never sends the session token elsewhere.
                info.block(True)

    class ResearchPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            address = url.toString()
            if is_local_url(address, backend.origin or ""):
                if is_main_frame and url.path().startswith("/api/"):
                    self.download(url)
                    return False
                return True
            if address == "about:blank":
                return True
            if is_main_frame and is_external_url(address):
                QDesktopServices.openUrl(url)
            return False

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Polymer Lab — 고분자 · 단백질 연구")
            self.setMinimumSize(960, 680)
            self.resize(1440, 940)
            self.settings = QSettings()
            geometry = self.settings.value("window/geometry")
            if geometry:
                self.restoreGeometry(geometry)
            self._closing = False
            self._stopped = False
            self._shutdown_started = 0.0
            self.profile = None
            self.page = None
            self.web = None
            self.downloads = []
            self.stack = QStackedWidget()
            self.loading = QWidget()
            self.loading.setStyleSheet("background:#0f1820;color:#eaf6f4;")
            layout = QVBoxLayout(self.loading)
            layout.addStretch()
            title = QLabel("Polymer Lab")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setStyleSheet("font-size:36px;font-weight:700;color:#69d7c0;")
            self.message = QLabel("연구 환경을 준비하고 있습니다…")
            self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.message.setWordWrap(True)
            self.message.setStyleSheet("font-size:16px;padding:24px;")
            layout.addWidget(title)
            layout.addWidget(self.message)
            layout.addStretch()
            self.stack.addWidget(self.loading)
            self.setCentralWidget(self.stack)
            self.statusBar().showMessage("로컬 연구 환경 시작 중")
            self._make_menus()
            self.timer = QTimer(self)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self._tick)
            self.timer.start()
            self.server = QLocalServer(self)
            QLocalServer.removeServer(socket_name)
            self.server.listen(socket_name)
            self.server.newConnection.connect(self._activate)

        def _activate(self):
            socket = self.server.nextPendingConnection()
            if socket:
                socket.disconnected.connect(socket.deleteLater)
                socket.disconnectFromServer()
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def _action(self, menu, label, callback, shortcut=None):
            action = QAction(label, self)
            action.triggered.connect(callback)
            if shortcut:
                action.setShortcut(shortcut)
            menu.addAction(action)
            return action

        def _make_menus(self):
            file_menu = self.menuBar().addMenu("파일")
            self._action(file_menu, "새 실험 화면", self._home, QKeySequence.StandardKey.New)
            self._action(file_menu, "PDB 구조 가져오기…", lambda: self._import_structure("pdb"), "Ctrl+O")
            self._action(file_menu, "FASTA 서열 가져오기…", lambda: self._import_structure("fasta"))
            file_menu.addSeparator()
            self._action(file_menu, "연구 결과 폴더 열기", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(backend.data_dir))))
            self._action(file_menu, "다운로드 폴더 열기", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation))))
            file_menu.addSeparator()
            self._action(file_menu, "종료", self.close, QKeySequence.StandardKey.Quit)
            views = self.menuBar().addMenu("보기")
            for label, view in (("워크플로우", "workflow"), ("연구 데이터", "research"), ("실험·문헌 입력", "measurements"), ("물성 모델", "models"), ("탐색 캠페인", "campaigns"), ("혼합계 브릿지", "bridges"), ("실험 기록", "history"), ("REA 분석 근거", "evidence"), ("계산 엔진", "engines")):
                self._action(views, label, lambda checked=False, value=view: self._navigate(value))
            views.addSeparator()
            self._action(views, "새로고침", lambda: self.web and self.web.reload(), QKeySequence.StandardKey.Refresh)
            self._action(views, "확대", lambda: self._zoom(0.1), QKeySequence.StandardKey.ZoomIn)
            self._action(views, "축소", lambda: self._zoom(-0.1), QKeySequence.StandardKey.ZoomOut)
            self._action(views, "기본 배율", lambda: self.web and self.web.setZoomFactor(1), "Ctrl+0")
            help_menu = self.menuBar().addMenu("도움말")
            self._action(help_menu, "실행 로그 폴더 열기", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(state_dir / "logs"))))
            self._action(help_menu, "Polymer Lab 정보", lambda: QMessageBox.about(self, APP_NAME,
                "<h3>Polymer Lab " + __version__ + "</h3><p>고분자·단백질 연구를 위한 로컬 데스크톱 프로그램</p>"
                "<p>구조 생성 → 계산 → 분석 → 연구 기록</p><p>AlphaFold DB 조회 및 ColabFold 연결 지원</p>"
                "<p>연구 데이터는 이 컴퓨터의 연구 결과 폴더에 저장됩니다.</p>"))

        def _home(self):
            if self.web and backend.origin:
                self.web.setUrl(QUrl(backend.origin + "/"))

        def _navigate(self, view):
            if self.page:
                self.page.runJavaScript("typeof showView === 'function' && showView(" + json.dumps(view) + ")")

        def _zoom(self, amount):
            if self.web:
                self.web.setZoomFactor(max(0.65, min(1.75, self.web.zoomFactor() + amount)))

        def _import_structure(self, kind):
            if not self.page:
                return
            title = "PDB 구조 가져오기" if kind == "pdb" else "FASTA 서열 가져오기"
            pattern = "PDB 구조 (*.pdb *.ent);;모든 파일 (*)" if kind == "pdb" else "FASTA 서열 (*.fasta *.fa *.faa *.txt);;모든 파일 (*)"
            filename, _ = QFileDialog.getOpenFileName(self, title, "", pattern)
            if not filename:
                return
            try:
                source = Path(filename)
                if source.stat().st_size > (5_000_000 if kind == "pdb" else 100_000):
                    raise ValueError("파일이 너무 큽니다. PDB는 5 MB, FASTA는 100 KB까지 가져올 수 있습니다.")
                content = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            field = "pdb-text" if kind == "pdb" else "sequence"
            mode = "import" if kind == "pdb" else "export"
            script = """(() => {
                if (typeof showView !== 'function') return;
                showView('workflow');
                document.getElementById('route-select').value = 'protein';
                document.getElementById('protein-mode').value = %s;
                document.getElementById(%s).value = %s;
                document.getElementById('job-name').value = %s;
                if (typeof state !== 'undefined') state.handoff = null;
                updateRoute();
                toast('파일을 가져왔습니다. 실행 조건을 확인한 뒤 워크플로우를 시작하세요.');
            })()""" % (json.dumps(mode), json.dumps(field), json.dumps(content), json.dumps(source.stem[:100]))
            self.page.runJavaScript(script)

        def _tick(self):
            if self._closing:
                if not backend.process or backend.process.poll() is not None:
                    backend.cleanup()
                    self._stopped = True
                    self.close()
                elif time.monotonic() - self._shutdown_started > 18:
                    backend.stop(timeout=0)
                    self._stopped = True
                    self.close()
                return
            try:
                origin = backend.poll_ready()
                if origin and not self.web:
                    self._load(origin)
                elif not origin and time.monotonic() - backend.started_at > 60:
                    raise RuntimeError("연구 서비스 시작 시간이 초과되었습니다. 실행 로그를 확인하세요.")
            except Exception as exc:
                self.timer.stop()
                self.message.setText(str(exc))
                self.stack.setCurrentWidget(self.loading)
                self.statusBar().showMessage("연구 서비스 시작 오류")
                QMessageBox.critical(self, APP_NAME, str(exc))
                self.close()

        def _load(self, origin):
            self.profile = QWebEngineProfile(self)
            self.interceptor = LocalRequests(self.profile)
            self.profile.setUrlRequestInterceptor(self.interceptor)
            self.profile.downloadRequested.connect(self._download)
            self.page = ResearchPage(self.profile, self)
            self.page.newWindowRequested.connect(self._new_window)
            settings = self.page.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
            self.web = QWebEngineView(self)
            self.web.setPage(self.page)
            self.web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.web.loadFinished.connect(self._loaded)
            self.stack.addWidget(self.web)
            self.web.setUrl(QUrl(origin + "/"))
            self.statusBar().showMessage("연구 화면을 불러오는 중…")

        def _loaded(self, ok):
            if ok:
                self.stack.setCurrentWidget(self.web)
                self.statusBar().showMessage("로컬 연구 · " + str(backend.data_dir))
            elif not self._closing:
                self.message.setText("연구 화면을 불러오지 못했습니다. 보기 → 새로고침을 사용하거나 실행 로그를 확인하세요.")
                self.stack.setCurrentWidget(self.loading)

        def _new_window(self, request):
            url = request.requestedUrl()
            if is_external_url(url.toString()):
                QDesktopServices.openUrl(url)
            elif is_local_url(url.toString(), backend.origin or ""):
                if url.path().startswith("/api/"):
                    self.page.download(url)
                else:
                    self.web.setUrl(url)

        def _download(self, download):
            if not is_local_url(download.url().toString(), backend.origin or ""):
                download.cancel()
                return
            folder = self.settings.value("download/directory", QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation))
            suggested = Path(folder) / Path(download.downloadFileName()).name
            filename, _ = QFileDialog.getSaveFileName(self, "연구 결과 저장", str(suggested))
            if not filename:
                download.cancel()
                return
            target = Path(filename)
            self.settings.setValue("download/directory", str(target.parent))
            download.setDownloadDirectory(str(target.parent))
            download.setDownloadFileName(target.name)
            self.downloads.append(download)
            download.isFinishedChanged.connect(lambda item=download: self._download_finished(item))
            download.accept()
            self.statusBar().showMessage("저장 중: " + str(target))

        def _download_finished(self, download):
            if not download.isFinished():
                return
            from PySide6.QtWebEngineCore import QWebEngineDownloadRequest
            if download.state() == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                self.statusBar().showMessage("저장 완료: " + download.downloadFileName(), 10000)
            elif download.state() == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted:
                QMessageBox.warning(self, "파일 저장 실패", download.interruptReasonString())
            if download in self.downloads:
                self.downloads.remove(download)

        def closeEvent(self, event):
            if self._stopped:
                self.settings.setValue("window/geometry", self.saveGeometry())
                self.timer.stop()
                self.server.close()
                # Defer in this order: pages must be gone before their profile.
                if self.page:
                    self.page.deleteLater()
                if self.profile:
                    self.profile.deleteLater()
                event.accept()
                return
            event.ignore()
            if self._closing:
                return
            active = backend.active_jobs()
            if active:
                answer = QMessageBox.question(self, "실행 중인 계산 종료", f"실행 또는 대기 중인 작업이 {len(active)}개 있습니다.\n종료하면 계산을 중단하고 현재 기록을 보존합니다.\n프로그램을 종료할까요?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._closing = True
            self._shutdown_started = time.monotonic()
            self.message.setText("작업 기록을 보존하고 연구 서비스를 종료하고 있습니다…")
            self.stack.setCurrentWidget(self.loading)
            self.menuBar().setEnabled(False)
            backend.request_stop()
            self.timer.start()

    window = MainWindow()
    try:
        backend.start()
    except Exception as exc:
        QMessageBox.critical(window, APP_NAME, f"연구 환경을 시작하지 못했습니다.\n{exc}")
        lock.unlock()
        return 1
    window.show()
    app.aboutToQuit.connect(lambda: backend.stop())
    # Keep Python signal handling responsive, including a terminal SIGTERM.
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda *_: window.close())
    try:
        return app.exec()
    finally:
        backend.stop()
        lock.unlock()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
