#!/usr/bin/env python3
"""Write the rclone config for .github/workflows/drive-backup.yml from the RCLONE_DRIVE_TOKEN secret.

The secret is what `rclone authorize "drive" <config blob>` printed (docs/DRIVE_BACKUP.md). Given a
blob, rclone answers with one: unpadded standard base64 of a JSON map whose "token" entry is the OAuth
token. Without a blob, and in older rclone, the answer is the token JSON itself. rclone config accepts
both, and so does this; the "--->" and "<---End paste" lines around the answer may be pasted along.

DRIVE_CLIENT_ID and DRIVE_CLIENT_SECRET name the user's own Google OAuth client, which the token must
have been authorized with. Without them rclone uses its shared client, which rclone retires during 2026.
"""
from __future__ import annotations
import base64
import json
import os
import sys

# Limited to the files rclone creates.
CONFIG = "[gdrive]\ntype = drive\nscope = drive.file\n{client}token = {token}\n"


def drive_client(client_id, client_secret):
    """Return the config lines for an own OAuth client, or "" for rclone's shared one."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if bool(client_id) != bool(client_secret):
        raise ValueError("set both DRIVE_CLIENT_ID and DRIVE_CLIENT_SECRET, or neither")
    if not client_id:
        return ""
    if not client_id.endswith(".apps.googleusercontent.com") or any(c.isspace() for c in client_id + client_secret):
        raise ValueError("DRIVE_CLIENT_ID must be a Google OAuth client ID (...apps.googleusercontent.com)")
    return f"client_id = {client_id}\nclient_secret = {client_secret}\n"


def drive_token(secret):
    """Return the OAuth token in rclone authorize's answer as one line of JSON."""
    answer = "".join(secret.split("--->")[-1].split("<---")[0].split())
    try:
        if "{" in answer:
            # The token JSON itself, as older rclone printed it or as it stands in an rclone.conf.
            answer = answer[answer.index("{"):answer.rindex("}") + 1]
        else:
            answer = json.loads(base64.b64decode(answer + "=" * (-len(answer) % 4), validate=True))["token"]
        token = json.loads(answer)
    except (ValueError, TypeError, KeyError):
        token = None
    if not isinstance(token, dict) or not token.get("access_token"):
        raise ValueError("RCLONE_DRIVE_TOKEN is not what rclone authorize printed; see docs/DRIVE_BACKUP.md")
    if not token.get("refresh_token"):
        # Google sends a refresh token only with a fresh consent; without one uploads stop after an hour.
        raise ValueError("the Drive token has no refresh_token: remove rclone's access at "
                         "https://myaccount.google.com/permissions, then run rclone authorize again")
    return json.dumps(token, separators=(",", ":"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: drive_backup_config.py CONFIG_PATH  (token in RCLONE_DRIVE_TOKEN)", file=sys.stderr)
        return 2
    try:
        token = drive_token(os.environ.get("RCLONE_DRIVE_TOKEN", ""))
        client = drive_client(os.environ.get("DRIVE_CLIENT_ID", ""), os.environ.get("DRIVE_CLIENT_SECRET", ""))
    except ValueError as error:
        print(f"::error::{error}")
        return 1
    descriptor = os.open(argv[0], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config:
        config.write(CONFIG.format(client=client, token=token))
    return 0


if __name__ == "__main__":
    sys.exit(main())
