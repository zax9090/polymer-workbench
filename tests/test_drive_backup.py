"""Drive backup: reading the RCLONE_DRIVE_TOKEN secret the way rclone config reads authorize's answer."""
import base64
import importlib.util
import json
from pathlib import Path
import stat

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drive_backup_config", ROOT / "scripts/drive_backup_config.py")
drive_backup_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drive_backup_config)

TOKEN = {"access_token": "ya29.a0-example_token", "token_type": "Bearer",
         "refresh_token": "1//0g-example-refresh", "expiry": "2026-10-22T23:49:45.123456+09:00", "expires_in": 3599}


def blob(entries):
    # configmap.Simple.Encode: base64.RawStdEncoding of the JSON map.
    return base64.b64encode(json.dumps(entries).encode()).decode().rstrip("=")


def pasted(answer):
    return f"Paste the following into your remote machine --->\r\n{answer}\r\n<---End paste\r\n"


def test_reads_the_blob_rclone_authorize_prints_for_a_scope_blob():
    answer = blob({"token": json.dumps(TOKEN, separators=(",", ":"))})
    assert answer.startswith("eyJ0b2tlbiI6")
    wrapped = "\n".join(answer[i:i + 60] for i in range(0, len(answer), 60))
    for secret in (answer, pasted(answer), pasted(wrapped), " " + answer + "\n"):
        assert json.loads(drive_backup_config.drive_token(secret)) == TOKEN


def test_reads_a_token_json_from_older_rclone_or_an_rclone_conf():
    text = json.dumps(TOKEN)
    for secret in (text, pasted(text), "token = " + json.dumps(TOKEN, separators=(",", ":"))):
        result = drive_backup_config.drive_token(secret)
        assert json.loads(result) == TOKEN and "\n" not in result


@pytest.mark.parametrize("secret", ["", "Paste the following into your remote machine --->\n<---End paste",
                                    "not a token", blob({"scope": "drive.file"}), blob(["token"]),
                                    blob({"token": "{}"}), "eyJ0b2tlbiI6", '{"access_token": "x"'])
def test_rejects_anything_else_without_echoing_it(secret):
    with pytest.raises(ValueError, match="not what rclone authorize printed") as error:
        drive_backup_config.drive_token(secret)
    assert secret not in str(error.value) or not secret


def test_a_token_without_refresh_token_is_refused():
    token = {key: value for key, value in TOKEN.items() if key != "refresh_token"}
    with pytest.raises(ValueError, match="refresh_token"):
        drive_backup_config.drive_token(blob({"token": json.dumps(token)}))


def test_main_writes_a_private_drive_file_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_DRIVE_TOKEN", pasted(blob({"token": json.dumps(TOKEN)})))
    assert drive_backup_config.main([str(config)]) == 0
    head, token = config.read_text().split("token = ")
    assert head == "[gdrive]\ntype = drive\nscope = drive.file\n" and json.loads(token) == TOKEN
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    monkeypatch.setenv("RCLONE_DRIVE_TOKEN", "1//0g-example-refresh")
    missing = tmp_path / "missing.conf"
    assert drive_backup_config.main([str(missing)]) == 1
    output = capsys.readouterr().out
    assert output.startswith("::error::") and "1//0g" not in output and not missing.exists()


def test_an_own_oauth_client_goes_into_the_config(tmp_path, monkeypatch):
    config = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_DRIVE_TOKEN", blob({"token": json.dumps(TOKEN)}))
    monkeypatch.setenv("DRIVE_CLIENT_ID", " 1234-abc.apps.googleusercontent.com\n")
    monkeypatch.setenv("DRIVE_CLIENT_SECRET", "GOCSPX-example")
    assert drive_backup_config.main([str(config)]) == 0
    assert config.read_text().startswith("[gdrive]\ntype = drive\nscope = drive.file\n"
                                         "client_id = 1234-abc.apps.googleusercontent.com\nclient_secret = GOCSPX-example\ntoken = {")


@pytest.mark.parametrize("client_id, client_secret", [("1234-abc.apps.googleusercontent.com", ""), ("", "GOCSPX-x"),
                                                      ("not-a-client", "GOCSPX-x"), ("1234-abc.apps.googleusercontent.com", "a b")])
def test_a_partial_or_malformed_client_is_refused(client_id, client_secret):
    with pytest.raises(ValueError, match="DRIVE_CLIENT"):
        drive_backup_config.drive_client(client_id, client_secret)
