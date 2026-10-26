"""Syntax-check the page's inline script with node: one stray quote in it
stops the whole app from loading (2026-10-12: an apostrophe in "it's").
Also checks that the lyrics helper inlined in the page is the same code as app/lyrics-lib.js."""
import re, subprocess, sys, tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(js)
r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
print(r.stderr or "page script OK")

lib = (root / "app" / "lyrics-lib.js").read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
begin, end = "/* LYRICS-LIB:BEGIN", "\n/* LYRICS-LIB:END */"
text = html.replace("\r\n", "\n")
start = text.find("\n", text.find(begin)) + 1
inline = text[start:text.find(end)]
if not (begin in text and inline == lib):
    print("the lyrics helper inlined in index.html differs from app/lyrics-lib.js: copy the file between the LYRICS-LIB markers")
    sys.exit(1)
print("inlined lyrics helper matches app/lyrics-lib.js")
sys.exit(r.returncode)
