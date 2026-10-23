"""Render the one maintained user manual for source and packaged applications."""
import re
import sys
from pathlib import Path

from markdown_it import MarkdownIt

from .paths import resource_root


def manual_path():
    for path in (resource_root() / "docs" / "MANUAL.md", Path(sys.prefix) / "share" / "polymer-lab" / "MANUAL.md"):
        if path.is_file():
            return path
    raise FileNotFoundError("사용 설명서 파일을 찾지 못했습니다. 설치 파일을 확인하세요.")


def render_manual(source):
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    counts = {}
    def heading(tokens, index, options, env):
        title = tokens[index + 1].content if index + 1 < len(tokens) else "section"
        anchor = re.sub(r"[^\w\s-]", "", title.lower()).strip()
        anchor = re.sub(r"\s+", "-", anchor) or "section"
        count = counts.get(anchor, 0)
        counts[anchor] = count + 1
        tokens[index].attrSet("id", anchor if not count else f"{anchor}-{count}")
        return parser.renderer.renderToken(tokens, index, options, env)
    parser.renderer.rules["heading_open"] = heading
    content = parser.render(source)
    return """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymer Lab 사용 설명서</title><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.75}
body{max-width:920px;margin:auto;padding:24px;color:#dbe7ed;background:#101b24;overflow-wrap:anywhere}
nav{display:flex;flex-wrap:wrap;gap:24px;position:sticky;top:0;padding:12px 0;background:#101b24;border-bottom:1px solid #3c5562}
a{color:#96e1c7}h1,h2,h3{line-height:1.35;scroll-margin-top:80px}h2{border-bottom:1px solid #3c5562;padding-bottom:10px;margin-top:48px}
pre{overflow:auto;padding:16px;background:#1b2b37;border-radius:8px}code{font-size:.9em;overflow-wrap:anywhere}table{border-collapse:collapse;display:block;overflow-x:auto;width:100%;max-width:100%}th,td{padding:10px;border:1px solid #3c5562;text-align:left}
blockquote{border-left:3px solid #96e1c7;margin-left:0;padding-left:16px}img{max-width:100%}li{margin:6px 0}
@media(max-width:600px){body{padding:16px}nav{gap:16px}h1{font-size:1.7rem}}
@media print{body{max-width:none;background:white;color:black;padding:0}nav{display:none}a{color:black}pre{white-space:pre-wrap;background:#eee}table{display:table}h2{break-after:avoid}tr{break-inside:avoid}}
</style></head><body><nav><a href="/">← 연구실</a><a href="#">처음으로</a><a href="/manual/download" download>설명서 내려받기</a></nav><main>""" + content + "</main></body></html>"
