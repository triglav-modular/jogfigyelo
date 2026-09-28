#!/usr/bin/env python3
"""Build web/index.html: the dashboard inside the live site's page chrome.

Takes a standard page of amunka.hu and keeps everything but its content: the
head with the theme's stylesheets and scripts, the header and navigation, the
footer, cookie consent and analytics. The content section is replaced by the
body of web/dashboard.html, and that file's head and script parts are added.
The dashboard so wears the site's current design and menu without a copy of
either in this repository. deploy.sh runs this before a full upload.

Fails, and writes nothing, if the page no longer has the markers it cuts at.
"""
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME = "https://amunka.hu/rolunk"
URL = "https://amunka.hu/jogfigyelo/"
UA = "amunka.hu jogfigyelo/1.0 (+https://amunka.hu)"


def part(fragment, name):
    m = re.search(rf"<!-- {name} -->\n(.*?)<!-- /{name} -->", fragment, re.S)
    if not m:
        sys.exit(f"web/dashboard.html: no <!-- {name} --> part")
    return m.group(1)


def replace_once(page, pattern, new, what):
    page, n = re.subn(pattern, lambda _: new, page, count=1, flags=re.S)
    if n != 1:
        sys.exit(f"{CHROME}: {what} not found; has the theme changed?")
    return page


def main():
    req = urllib.request.Request(CHROME, headers={"User-Agent": UA})
    page = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    fragment = (HERE / "web" / "dashboard.html").read_text()

    page = replace_once(page, r"<title>.*?</title>", "<title>Jogfigyelő | a Munka</title>", "<title>")
    page = replace_once(page, r'<link rel="canonical" href="[^"]*"\s*/?>',
                        f'<link rel="canonical" href="{URL}" />', "canonical link")
    # The source page's own Markdown twin is not this page's.
    page = re.sub(r'<link rel="alternate" type="text/markdown"[^>]*>\s*', "", page)
    page = replace_once(page, r"</head>", part(fragment, "head") + "</head>", "</head>")
    page = replace_once(page, r'<section id="body".*?(?=<footer id="footer")', part(fragment, "body"),
                        'content between <section id="body"> and <footer id="footer">')
    page = replace_once(page, r"</body>", part(fragment, "script") + "</body>", "</body>")

    out = HERE / "web" / "index.html"
    out.write_text(page)
    print(f"{out} ({len(page.encode()) // 1024} KB, chrome from {CHROME})")


if __name__ == "__main__":
    main()
