"""Wrap site/index.html (a page fragment, also used for the Claude preview) into a full
HTML document and copy the data folder into _site/ for GitHub Pages."""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_site"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir()
frag = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
head = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        '[hidden]{display:none!important}img{max-width:100%}</style></head><body>')
(OUT / "index.html").write_text(head + frag + "</body></html>", encoding="utf-8")
shutil.copytree(ROOT / "site" / "data", OUT / "data")
(OUT / ".nojekyll").write_text("")
print("built", OUT)
