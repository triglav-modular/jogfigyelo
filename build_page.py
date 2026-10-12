#!/usr/bin/env python3
"""Build web/index.html: the dashboard inside the live site's page chrome.

Takes a standard page of amunka.hu and keeps everything but its content: the
head with the theme's stylesheets and scripts, the header and navigation, the
footer, cookie consent and analytics. The content section is replaced by the
body of web/dashboard.html, and that file's head and script parts are added.
The dashboard so wears the site's current design and menu without a copy of
either in this repository. deploy.sh runs this before a full upload.

Each successful build keeps the page it borrowed from as data/chrome.html (the
data branch). When amunka.hu refuses the request, as its proxy does for some
GitHub runner addresses, that copy is used instead and a warning is printed.
Fails, and writes nothing, if the page no longer has the markers it cuts at.
"""
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "chrome.html"
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


def chrome():
    """The live page, or the copy kept from the last build if it is refused."""
    req = urllib.request.Request(CHROME, headers={"User-Agent": UA})
    try:
        return urllib.request.urlopen(req, timeout=60).read().decode("utf-8"), True
    except (urllib.error.URLError, TimeoutError) as e:
        if not CACHE.exists():
            sys.exit(f"{CHROME}: {e}, and there is no {CACHE} to build from")
        # A GitHub annotation in the workflow; an ordinary line elsewhere.
        print(f"::warning::{CHROME}: {e}; built from the copy in {CACHE.relative_to(HERE)}")
        return CACHE.read_text(), False


def main():
    page, live = chrome()
    source = page
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
    if live and CACHE.parent.is_dir():
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(source)
        os.replace(tmp, CACHE)
    print(f"{out} ({len(page.encode()) // 1024} KB, chrome from {CHROME if live else CACHE})")


if __name__ == "__main__":
    main()
