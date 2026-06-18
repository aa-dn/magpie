"""Run this to preview the app's HTML without starting a server.
   python preview.py  — writes preview.html then opens it in your browser.
"""
import webbrowser, pathlib
from app import _HTML

out = pathlib.Path("preview.html")
out.write_text(_HTML, encoding="utf-8")
webbrowser.open(out.resolve().as_uri())
print(f"Opened {out.resolve()}")
