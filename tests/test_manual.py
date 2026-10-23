"""The browser and downloaded manual share the same maintained Markdown source."""
from fastapi.testclient import TestClient

from workbench.manual import manual_path, render_manual
from workbench.server import create_app


def test_manual_routes_share_source(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        response = client.get("/manual")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert '<html lang="ko">' in response.text
        assert "DOE" in response.text and "Google Drive" in response.text
        download = client.get("/manual/download")
        assert download.status_code == 200
        assert download.content == manual_path().read_bytes()


def test_manual_headings_tables_and_literal_html():
    result = render_manual("# 실험 설계\n\n## 실험 설계\n\n|입력|결과|\n|---|---|\n|1|2|\n\n<script>alert(1)</script>")
    assert 'id="실험-설계"' in result
    assert 'id="실험-설계-1"' in result
    assert "<table>" in result
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
