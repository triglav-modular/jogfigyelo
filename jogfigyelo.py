#!/usr/bin/env python3
"""Watch the official gazette and the courts for labour-law news.

Sources:
  Magyar Közlöny       magyarkozlony.hu/feed. Each new issue's PDF is split
                       into its acts along the table of contents, and every
                       act is scored against the patterns in config.toml.
  Bírósági határozatok eakta.birosag.hu anonymized decisions. Every decision
                       the [[bhgy]] queries in config.toml return.
  Kúria                kuria-birosag.hu. Uniformity decisions (JEH), decisions
                       on uniformity complaints, and press releases, each
                       scored on its subject and full text; and the monthly
                       Kúriai Döntések, whose labour section is reported
                       whole, other sections by headnote score.
  Alkotmánybíróság     alkotmanybirosag.hu/a-legfrissebb-dontesek/. Decisions
                       whose subject matches the patterns.

Anything not seen before goes into a Markdown report in DATA/jelentesek/ and
onto DATA/talalatok.jsonl; DATA/state.json remembers what has been seen. A
source that fails is not marked as seen, so the next run retries it, and the
run exits 1.

Each run is also logged on DATA/runs.jsonl, and --export FILE writes every
find and run as one JSON file: the dashboard at https://amunka.hu/jogfigyelo/
(web/index.html) loads it as data.json from beside itself, and deploy.sh
uploads both.

  python3 jogfigyelo.py              check every source
  python3 jogfigyelo.py --dry-run    report, but remember nothing
  python3 jogfigyelo.py --export F   write the dashboard's data file
  python3 jogfigyelo.py --pdf FILE   score a downloaded Közlöny issue

Needs Python 3.11+ and pdftotext (poppler).
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "amunka.hu jogfigyelo/1.0 (+https://amunka.hu)"

KOZLONY_FEED = "https://magyarkozlony.hu/feed"
BHGY_SEARCH = "https://eakta.birosag.hu/AnonimizaltHatarozat/Search"
BHGY_PDF = "https://eakta.birosag.hu/anonimizalt-hatarozat-pdf/"
AB_LATEST = "https://alkotmanybirosag.hu/a-legfrissebb-dontesek/"
KURIA = "https://kuria-birosag.hu"
# List pages, newest first, and the path their items live under.
KURIA_LISTS = [
    ("/hu/jogegysegi-hatarozatok-hatalyos", "/hu/joghat/"),
    ("/hu/jogegysegi-hatalyu-panasz", "/hu/jogegysegi-panasz/"),
    ("/hu/jogegysegi-panasz", "/hu/jogegysegi-panasz/"),
    ("/hu/sajto", "/hu/sajto/"),
]
KURIA_JOURNAL = "/hu/kuriai-dontesek"
MAG = {"mag": "https://magyarkozlony.hu/rss"}

HU_MONTHS = ["január", "február", "március", "április", "május", "június", "július",
             "augusztus", "szeptember", "október", "november", "december"]


def fetch(url, form=None):
    """GET, or POST `form` url-encoded. Retries what a busy server answers."""
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(10 * (attempt + 1))


def squash(text):
    """Rejoin words hyphenated at line ends and collapse whitespace."""
    text = text.replace("­", "")
    text = re.sub(r"(\w)-[ \t]*\n\s*(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_tags(fragment):
    return squash(html.unescape(re.sub(r"<[^>]+>", " ", fragment)))


class Scorer:
    def __init__(self, cfg):
        self.patterns = [(p["name"], p["weight"], re.compile(p["re"], re.I)) for p in cfg["pattern"]]
        self.full_density = cfg["full_density"]

    def score(self, title, body=""):
        """Return (score, matched pattern names, strongest first).

        A title match counts three times the pattern's weight. In the body a
        pattern earns its weight only when it is dense enough: a mention or
        two in a 40,000-word omnibus is not a labour act, the same mention in
        a 300-word decree may well be. Amending a labour act (weight 10 and
        up) counts in full wherever it appears.
        """
        words = max(len(body.split()), 1)
        total, found = 0, []
        for name, weight, rx in self.patterns:
            if rx.search(title):
                s = 3 * weight
            else:
                n = len(rx.findall(body))
                if not n:
                    continue
                s = weight if weight >= 10 else weight * min(1, 1000 * n / words / self.full_density)
            total += s
            found.append((s, name))
        return round(total), [name for _, name in sorted(found, key=lambda f: -f[0])]

    def spans(self, text):
        """[(start, end, weight)] of every pattern match in `text`."""
        return sorted((m.start(), m.end(), weight) for _, weight, rx in self.patterns for m in rx.finditer(text))


# --- Magyar Közlöny -------------------------------------------------------

# The first column of a table-of-contents line: "2026. évi XXVI. törvény",
# "9/2026. (IX. 25.) HM rendelet", "5/2026. JEH határozat",
# "Köf.5.010/2026/4. számú határozat", "442/2026. számú NVB határozat".
ACT_ID = re.compile(r"""^\s{0,24}(?P<id>
      \d{4}\.\ évi\ [IVXLCDM]+\.\ törvény
    | [\w.]+/\d{4}(?:/\d+)?\.(?:\ \([IVX]+\.\ \d{1,2}\.\))?(?:\ számú)?(?:\ [\w.]+)?
      \ (?:rendelet|határozat|végzés|utasítás|irányelv)
    )(?:\s{2,}(?P<rest>\S.*))?$""", re.X)
TOC_PAGE = re.compile(r"\s(\d{1,5})\s*$")


def pdf_text(path, layout=True):
    out = subprocess.run(["pdftotext", *(["-layout"] if layout else []), str(path), "-"], capture_output=True, check=True)
    return out.stdout.decode("utf-8", "replace")


def fetch_pdf_text(url, layout=True):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(fetch(url))
        tmp.flush()
        return pdf_text(tmp.name, layout)


def page_offset(pages):
    """Gazette page number minus PDF page index, read from the running heads."""
    votes = {}
    for i, page in enumerate(pages[1:12], start=1):
        head = next((line for line in page.splitlines() if line.strip()), "")
        m = re.match(r"\s*(\d{1,5})\s+M A G Y A R", head) or re.search(r"szám\s+(\d{1,5})\s*$", head)
        if m:
            k = int(m.group(1)) - i
            votes[k] = votes.get(k, 0) + 1
    return max(votes, key=votes.get) if votes else None


def parse_toc(pages, offset):
    """Table-of-contents entries: [{id, title, page}], page as printed.

    An entry ends with its page number, so the next line starts a new one,
    whether or not it opens with an act id: the Alaptörvény and its
    amendments are listed by title alone.
    """
    entries, cur, toc_pages = [], None, None
    last_page = offset + len(pages)
    for pi, page in enumerate(pages):
        if toc_pages is not None and pi >= toc_pages:
            break
        lines = page.splitlines()
        if pi == 0:
            k = next((i for i, line in enumerate(lines) if "Tartalomjegyzék" in line), None)
            if k is None:
                return []
            lines = lines[k + 1:]
        for line in lines:
            if not line.strip() or "M A G Y A R" in line:
                continue
            m = ACT_ID.match(line)
            if m or cur is None or cur["page"] is not None:
                cur = {"id": re.sub(r"\s+", " ", m.group("id")) if m else "", "title": "", "page": None}
                entries.append(cur)
                if m:
                    line = m.group("rest") or ""
            pm = TOC_PAGE.search(line)
            if pm and offset < int(pm.group(1)) <= last_page:
                cur["page"] = int(pm.group(1))
                line = line[:pm.start()]
                if toc_pages is None:
                    toc_pages = cur["page"] - offset  # the first act starts where the contents end
            cur["title"] = f'{cur["title"]} {line.strip()}'.strip()
    return entries


def kozlony_acts(text):
    """Split an issue into acts: [{id, title, page, body}].

    Falls back to the whole issue as one unit, with id None, when the table of
    contents cannot be read, so a format change costs precision, not hits.
    """
    pages = text.split("\f")
    offset = page_offset(pages)
    entries = parse_toc(pages, offset) if offset is not None else []
    if not entries or any(e["page"] is None for e in entries):
        return [{"id": None, "title": "", "page": None, "body": squash(text)}]

    starts, pos = [], 0
    for page in pages:
        starts.append(pos)
        pos += len(page) + 1
    full = "\n".join(pages)

    begins = []
    for e in entries:
        pi = min(max(e["page"] - offset, 0), len(pages) - 1)
        # The act's own heading carries its id minus the final word
        # ("A Kormány 106/2026. (VII. 17.) Korm. rendelete").
        words = e["id"].split()[:-1] if e["id"] else e["title"].split()[:4]
        locator = r"\s+".join(map(re.escape, words))
        m = re.compile(locator).search(full, starts[pi]) if locator else None
        end_of_page = starts[pi + 1] if pi + 1 < len(starts) else len(full)
        begins.append(m.start() if m and m.start() < end_of_page else starts[pi])
    for i in range(1, len(begins)):
        begins[i] = max(begins[i], begins[i - 1])
    begins.append(len(full))

    return [dict(e, body=squash(full[begins[i]:begins[i + 1]])) for i, e in enumerate(entries)]


BOILERPLATE = re.compile(r"felhatalmazás alapján|feladatkörében eljárva|a következőket rendel|lép hatályba|hatályát veszti\.?$"
                         r"|^\[\d+\]|s\. k\.,|M A G Y A R")


def act_excerpt(act, scorer, most=2):
    """The act's own sentences that carry its strongest labour terms.

    Returns (text, marks): up to `most` sentences in document order, and the
    [start, end] of each match inside the text, for highlighting.
    """
    body = act["body"]
    first = re.search(r"\b1\. §|^1\. ", body)
    body = body[first.start():] if first else body
    sentences = re.split(r"(?<=[.;:])\s+(?=[A-ZÁÉÍÓÖŐÚÜŰ„(\d])", body)
    ranked = []
    for i, sent in enumerate(sentences):
        if len(sent) < 25 or BOILERPLATE.search(sent):
            continue
        spans = scorer.spans(sent)
        if spans:
            ranked.append((-sum({w for _, _, w in spans}), i, sent))
    picked = sorted(sorted(ranked)[:most], key=lambda r: r[1])
    text = " … ".join(clip(re.sub(r"^\d+\. §\s*", "", sent), 320) for _, _, sent in picked)
    marks = [[a, b] for a, b, _ in scorer.spans(text)]
    return text, marks


def act_effective(act):
    """The closing provision on entry into force, as the act words it."""
    m = re.search(r"[^.]*\blép(?:nek)? hatályba\b[^.]*\.", act["body"])
    return clip(re.sub(r"^\s*\d+\. §\s*", "", m.group(0)).strip(), 240) if m else ""


# An amending act is technical when every change it makes is to wording:
# phrases swapped ("szövegrész helyébe a … szöveg lép") or struck out
# ("… szövegrész hatályát veszti"). New or replaced provisions are not.
SUBSTANTIVE = re.compile(r"helyébe a következő|egészül ki|a következő [^.]{0,60}?(?:lép|kerül)\b"
                         r"|melléklet(?:e)? helyébe|(?:§-a|bekezdése|pontja|alpontja) hatályát veszti", re.I)


def technical_act(act):
    if not act["id"] or "módosításáról" not in act["title"] or "szövegrész" not in act["body"]:
        return ""
    return "" if SUBSTANTIVE.search(act["body"]) else "csak szövegrészeket cserél vagy töröl"


class Package:
    """The base statutes (torvenyek.toml), and which sections an act amends."""

    STATUTE_NUMBER = r"\d{4}\.\s*évi\s+[IVXLCDM]+\.\s*törvény\w*"
    DECREE_NUMBER = r"\d+/\d{4}\.\s*\([IVX]+\.\s*\d+\.\)\s*[\w.]+\s*rendelet\w*"
    # The legal basis a decree is issued under cites sections without changing them.
    LEGAL_BASIS = re.compile(r"felhatalmazás|hatáskörében eljárva|feladatkörében eljárva|a következőket rendel", re.I)
    SECTION = r"(\d+(?:/[A-Z])?)\.\s*§"
    AMENDS = re.compile(r"helyébe|egészül ki|kiegészül|hatályát veszti|szövegrész", re.I)

    def __init__(self, cfg):
        self.strong = cfg["strong_weight"]
        self.statutes = cfg["statute"]
        self.topics = [(re.compile(t["re"], re.I), t["weight"], t["note"]) for t in cfg.get("topic", [])]

    def topic(self, title):
        """{note, weight} of the heaviest topic the title names, or None."""
        found = [(w, note) for rx, w, note in self.topics if rx.search(title)]
        return {"note": max(found)[1], "weight": max(found)[0]} if found else None

    def weight(self, statute, section):
        return statute.get("sections", {}).get(section, statute["weight"])

    def changes(self, act):
        """[{statute, section, par, weight, wording, clause, new}], strongest first."""
        body, found = act["body"], {}
        for st in self.statutes:
            # A statute and its implementing decree often share a name:
            # match the kind of act the entry is.
            number = self.STATUTE_NUMBER if "törvény" in st.get("number", "törvény") else self.DECREE_NUMBER
            cite = re.escape(st["name"]) + r"\s+szóló\s+" + number
            aliases = {st["short"]}
            aliases.update(m.group(1).strip() for m in re.finditer(cite + r"\s*\(a továbbiakban:\s*([^)]{1,20})\)", body))
            anchor = re.compile(cite + r"(?:\s*\(a továbbiakban:[^)]*\))?|(?<![\w.])(?:" +
                                "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)) + r")(?![\w])")
            for m in anchor.finditer(body):
                ref = re.match(r"(?:-\w+)?\s*(?:a\s+következő\s+)?" + self.SECTION + r"(?:-\w+)?(?:\s*\((\d+[a-z]?)\)\s*bekezdés\w*)?",
                               body[m.end():m.end() + 120])
                if not ref:
                    continue
                start = max(body.rfind(". ", 0, m.start()), body.rfind(": ", 0, m.start()), body.rfind("” ", 0, m.start()), 0)
                end = re.compile(r":|\.\s+(?=[A-ZÁÉÍÓÖŐÚÜŰ„\d])").search(body, m.end() + ref.end())
                stop = end.start() if end else min(len(body), m.end() + 400)
                clause = body[start:stop + 1].lstrip(". :”")
                if not self.AMENDS.search(clause) or self.LEGAL_BASIS.search(clause):
                    continue  # a mere reference, or the legal basis of a decree
                new = ""
                if end and body[end.start()] == ":":
                    q = re.match(r"\s*„(.*?)”", body[end.end():end.end() + 3000], re.S)
                    new = clip(q.group(1), 420) if q else ""
                wording = "szövegrész" in clause and not re.search(r"helyébe a következő|egészül ki", clause)
                sections = [ref.group(1)] + re.findall(self.SECTION, body[m.end() + ref.end():stop])
                for i, sec in enumerate(dict.fromkeys(sections)):
                    key = (st["short"], sec, ref.group(2) if i == 0 else None)
                    if key not in found:
                        found[key] = {"statute": st["short"], "section": sec, "par": key[2], "weight": self.weight(st, sec),
                                      "wording": wording, "clause": clip(clause, 420), "new": new}
            # An act titled as amending the statute counts even where its
            # provisions escape the pattern above.
            if not any(k[0] == st["short"] for k in found) and re.search(re.escape(st["name"]) + r"\s+szóló[^.]{0,80}?módosításáról", act["title"]):
                found[(st["short"], None, None)] = {"statute": st["short"], "section": None, "par": None, "weight": st["weight"],
                                                    "wording": False, "clause": act["title"], "new": ""}
        # Substantive changes first: a changed phrase is housekeeping
        # whatever the section's weight.
        return sorted(found.values(), key=lambda c: (c["wording"], -c["weight"]))

    def level(self, changes):
        """Weight of the strongest substantive change, else of any change."""
        real = [c["weight"] for c in changes if not c["wording"]]
        return max(real) if real else max((c["weight"] for c in changes), default=None)

    def is_strong(self, changes):
        return any(c["weight"] >= self.strong and not c["wording"] for c in changes)


def score_acts(acts, scorer, cfg):
    skip = [re.compile(rx) for rx in cfg.get("skip_acts", [])]
    for act in acts:
        if act["id"] and any(rx.search(act["id"]) for rx in skip):
            act["score"], act["terms"] = 0, []
        else:
            act["score"], act["terms"] = scorer.score(act["title"], act["body"])
    return acts


def check_kozlony(cfg, scorer, package, seen, cutoff, errors):
    root = ET.fromstring(fetch(KOZLONY_FEED))
    hits, marked = [], {}
    for item in root.iter("item"):
        if item.findtext("mag:type", namespaces=MAG) not in cfg["kozlony_types"]:
            continue
        guid = item.findtext("guid")
        if guid in seen:
            continue
        published = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
        if cutoff and published < cutoff:
            marked[guid] = published.date().isoformat()
            continue
        issue = item.findtext("title").strip()
        pdf_url = item.find("enclosure").get("url")
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                tmp.write(fetch(pdf_url))
                tmp.flush()
                acts = score_acts(kozlony_acts(pdf_text(tmp.name)), scorer, cfg)
        except Exception as e:
            errors.append(f"Magyar Közlöny, {issue}: {e}")
            continue
        if acts[0]["id"] is None:
            errors.append(f"Magyar Közlöny, {issue}: a tartalomjegyzék nem volt értelmezhető, "
                          "a teljes számot egyben pontoztam")
        for act in acts:
            changes = package.changes(act) if act["id"] else []
            topic = package.topic(act["title"]) if act["id"] else None
            scored = cfg.get("keyword_scoring", False) and act["score"] >= cfg["min_score"]
            if not (changes or topic or scored):
                continue
            if changes:
                top = changes[0]
                excerpt = top["clause"] + (f" „{top['new']}”" if top["new"] else "")
                marks = [[m.start(), m.end()] for m in re.finditer(re.escape(top["statute"]) + r"|\d+(?:/[A-Z])?\.\s*§(?:\s*\(\d+[a-z]?\))?", excerpt)]
                technical = "csak szövegrészeket cserél vagy töröl" if all(c["wording"] for c in changes) else ""
            else:
                excerpt, marks = act_excerpt(act, scorer)
                technical = technical_act(act)
            level = max([w for w in (package.level(changes), topic and topic["weight"]) if w], default=None)
            hits.append({
                "changes": [{k: c[k] for k in ("statute", "section", "par", "weight", "wording", "new") if c[k] not in (None, "")}
                            for c in changes],
                "level": level,
                "source": "kozlony",
                "id": f'{guid}#{act["id"] or ""}',
                "excerpt": excerpt,
                "marks": marks,
                "effective": act_effective(act),
                "technical": technical,
                "date": published.date().isoformat(),
                "issue": issue,
                "ref": act["id"] or issue,
                "title": act["title"],
                "page": act["page"],
                "url": guid,
                "pdf": pdf_url,
                "score": act["score"] if cfg.get("keyword_scoring", False) else None,
                "strong": package.is_strong(changes) or bool(topic and topic["weight"] >= package.strong and not technical),
                "topic": topic,
                "terms": act["terms"] if cfg.get("keyword_scoring", False) else [],
            })
        marked[guid] = published.date().isoformat()
    return hits, marked


# --- Anonymized court decisions ------------------------------------------

SPACED = lambda word: r"\s*".join(word)
# Matched after letter-spaced words ("v é g z é s t") are closed up. A
# qualifier may stand before the form: közbenső, kiegészítő, kijavító, rész-.
OPERATIVE = re.compile(
    r"^\s*(?:(?P<rr>Rendelkező rész)|(?P<qcap>(?:közbenső|kiegészítő|kijavító|rész)\s*)?(?P<cap>ÍTÉLET|VÉGZÉS|HATÁROZAT)\w*\s*[:.]?)\s*$"
    r"|(?:következő|alábbi)\s*\n?\s*(?P<q>[\wáéíóöőúüű]+\s+)?(?P<acc>[\wáéíóöőúüű]*?(?:ítéletet|végzést|határozatot))\s*[:.]?\s*$",
    re.M | re.I)
REASONS = re.compile(r"^\s*(?:Indokolás|INDOKOLÁS)\s*[:.]?\s*$", re.M)
QUALIFIERS = ("közbenső", "kiegészítő", "kijavító", "rész")
RUNNING_HEAD = re.compile(r"^\s*(?:\d{1,3}|[IVX]*\.?\s*[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]+\.[IVX]*\.?[\d.\s]+/\d{4}/\d+[-IVX.]*"
                          r"|.*(?:Ítélőtábla|Törvényszék|Kúria|Bíróság)|alkotmanybirosag\.hu)\s*$")


def decision_parts(text):
    """The operative part of a decision, its form and date, from its text.

    The operative part runs from "Rendelkező rész" (Kúria), "ÍTÉLET" or
    "…meghozta a következő ítéletet/végzést/határozatot" to "Indokolás"; the
    reasons are left out. Sums spelt out in words and the standard notice on
    hearings are dropped. Returns {} when the markers are not there.
    """
    text = re.sub(r"\b(?:\w ){3,}\w\b", lambda w: w.group(0).replace(" ", ""), text)  # k i j a v í t ó
    m = OPERATIVE.search(text)
    end = REASONS.search(text, m.end()) if m else None
    if not end:
        return {}
    word = (m.group("cap") or m.group("acc") or "").lower()
    qualifier = ((m.group("q") or m.group("qcap") or "") + word).lower()
    qualifier = next((q for q in QUALIFIERS if qualifier.startswith(q)), "")
    if m.group("rr"):
        head = text[:m.start()][:400]
        word = "végzés" if re.search(r"\bvégzése\b", head) else "ítélet" if re.search(r"\bítélete\b", head) else "határozat"
    form = "végzés" if "végz" in word else "ítélet" if "ítél" in word else "határozat"
    if qualifier:
        form = f"{qualifier}{'' if qualifier == 'rész' else ' '}{form}"
    lines = [line.strip() for line in text[m.end():end.start()].splitlines() if line.strip() and not RUNNING_HEAD.match(line)]
    out = re.sub(r"(\w)- (\w)", r"\1\2", " ".join(lines))
    out = re.sub(r"\s*\((?:azaz:?\s*)?(?=[^)]*[a-zőű]-[a-zőű])[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű :-]{12,}\)", "", out)
    out = re.sub(r"\bforint(?: forint)+\b", "forint", out)
    out = re.split(r"\s*A bíróság tájékoztatja a feleket", out)[0]
    decided = ""
    for y, month, d in reversed(re.findall(r"^\s*[A-ZÁÉÍÓÖŐÚÜŰ][\wáéíóöőúüű-]+,\s*(\d{4})\.\s*(\w+)\s+(\d{1,2})\.", text, re.M)):
        if month.lower() in HU_MONTHS:
            decided = f"{y}-{HU_MONTHS.index(month.lower()) + 1:02d}-{int(d):02d}"
            break
    return {"form": form, "decided": decided, "outcome": clip(squash(out), 1500)}


def technical_order(cfg, form, ref, outcome):
    """Why a court order settles procedure rather than the claim, or ""."""
    if form and form.split()[0] in ("kijavító", "kiegészítő"):
        return f"{form}: egy korábbi határozatot javít vagy egészít ki"
    if not form or not form.endswith("végzés"):
        return ""
    kind = re.match(r"[A-Za-z]+", ref or "")
    if kind and kind.group(0) in cfg.get("technical_case_types", []):
        return f"eljárási végzés ({kind.group(0)})"
    for rx in cfg.get("technical_orders", []):
        if re.search(rx, outcome or "", re.I):
            return "eljárási végzés: " + re.search(rx, outcome, re.I).group(0)
    return ""


def check_bhgy(cfg, query, seen, cutoff):
    form = {k: v for k, v in query.items() if k != "label"}
    form.update(Rendezes="IndexelesIdejeCsokkeno", NemHivatkozhato="nem", ResultCount=100)
    hits, marked = [], {}
    for page in range(10):
        form["ResultStartIndex"] = page * 100
        data = json.loads(fetch(BHGY_SEARCH, form))
        if not data.get("Success"):
            raise RuntimeError(data.get("Message") or "a keresés sikertelen")
        items = data.get("List") or []
        done = len(items) < 100
        for x in items:
            key = x["IndexId"]
            # .NET writes seven fractional digits; fromisoformat takes six.
            when = dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", x["IndexelesIdeje"]))
            if key in seen or key in marked:
                done = True
                continue
            marked[key] = when.date().isoformat()
            if cutoff and when < cutoff:
                done = True
                continue
            pdf = BHGY_PDF + "?" + urllib.parse.urlencode(
                {"birosagName": x["MeghozoBirosag"], "ugyszam": x["Azonosito"], "azonosito": key})
            try:
                parts = decision_parts(fetch_pdf_text(pdf, layout=False))
                time.sleep(0.3)
            except Exception:
                parts = {}  # the decision is still reported, with its principle only
            hits.append({
                **parts,
                "technical": technical_order(cfg, parts.get("form"), x["Azonosito"], parts.get("outcome")),
                "source": "bhgy",
                "label": query["label"],
                "id": key,
                "date": when.date().isoformat(),
                "ref": x["Azonosito"],
                "court": x["MeghozoBirosag"],
                "year": x.get("HatarozatEve"),
                "summary": squash((x.get("Rezume") or "").replace("Ⓝ", " ")),
                "pdf": pdf,
            })
        if done:
            break
    return hits, marked


# --- Kúria -----------------------------------------------------------------

def main_content(page):
    """The Drupal page's own content, without navigation, pager or footer."""
    i = page.find('id="main-content"')
    page = page[i:] if i >= 0 else page
    for end in ('class="pager', "<footer"):
        j = page.find(end)
        if j >= 0:
            page = page[:j]
    return page


def kuria_links(path, prefix, page_no=0):
    """[(url, title)] of the items a list page links to, in page order."""
    page = main_content(fetch(f"{KURIA}{path}?page={page_no}").decode("utf-8", "replace"))
    links = {}
    rx = r'<a[^>]*href="(?:' + re.escape(KURIA) + r')?(' + re.escape(prefix) + r'[^"?#]+)"[^>]*>(.*?)</a>'
    for m in re.finditer(rx, page, re.S):
        title = strip_tags(m.group(2))
        if title and not title.startswith("Tovább"):
            links.setdefault(KURIA + m.group(1), title)
    if not links:
        raise RuntimeError(f"{path}: nincs tétel az oldalon; megváltozott a szerkezete?")
    return list(links.items())


def kuria_item(url):
    """(subject, full text, date) of a decision or press page."""
    content = main_content(fetch(url).decode("utf-8", "replace"))
    text = strip_tags(content)
    stamp = re.search(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})', content)
    signed = re.findall(r"Budapest, (\d{4}\. \w+ \d{1,2}\.)", text)
    date = dt.date.fromisoformat(stamp.group(1)) if stamp else hu_date(signed[-1]) if signed else None
    # Complaint decisions name the case's subject and the rulings attacked
    # (an "Mfv." there is a labour case); JEH carry a one-line subject.
    subject = " ".join(m.group(1) for m in re.finditer(
        r"(?:A per tárgya|támadott határozatok? száma):\s*(.*?)\s*(?:A jogegységi panasz|Rendelkező rész|A Kúria)", text))
    if not subject:
        m = re.search(r"Jogegységi határozat \(.*?\)\s*(.*?)\s*A Kúria Jogegységi", text)
        subject = m.group(1) if m else ""
    return subject, text, date


def kuria_outcome(text):
    m = re.search(r"(?:Rendelkező rész|jogegységi határozatot:|határozatot:|végzést:)\s*(.*?)\s*Indokolás\b", text)
    return clip(m.group(1), 1500) if m else ""


def check_kuria_lists(cfg, scorer, seen, cutoff):
    hits, marked = [], {}
    for path, prefix in KURIA_LISTS:
        # Page back until the list reaches what was seen before, or (first
        # run) the cutoff: the press list shows five items a page.
        old = False
        for page_no in range(5):
            known = False
            for url, title in kuria_links(path, prefix, page_no):
                if url in seen or url in marked:
                    known = True
                    continue
                if old:  # past the cutoff, the rest of the list is older still
                    marked[url] = ""
                    continue
                subject, text, date = kuria_item(url)
                marked[url] = date.isoformat() if date else ""
                if cutoff and date and date < cutoff.date():
                    old = True
                    continue
                score, terms = scorer.score(f"{title} {subject}", text)
                if score < cfg["min_score"]:
                    continue
                outcome = "" if prefix == "/hu/sajto/" else kuria_outcome(text)
                hits.append({
                    "source": "kuria",
                    "outcome": outcome,
                    "technical": technical_order(cfg, "végzés" if "végzés" in title else "", title, outcome),
                    "id": url,
                    "date": date.isoformat() if date else "",
                    "kind": "Sajtó" if prefix == "/hu/sajto/" else "Jogegységi eljárás",
                    "ref": title,
                    "title": clip(subject, 400),
                    "url": url,
                    "score": score,
                    "strong": score >= cfg["strong_score"],
                    "terms": terms,
                })
            if known or old:
                break
    return hits, marked


def pdf_columns(path):
    """Page texts of a two-column PDF in reading order.

    Rebuilt word by word from pdftotext's bounding boxes. A line spans both
    columns when a word crosses the middle of the page or it is an
    all-capitals heading; such lines cut the page into bands, and each band
    is read left column first, then right. pdftotext's own blocks and a crop
    into halves both merge or cut lines that sit level across the gutter.
    """
    xml = subprocess.run(["pdftotext", "-bbox-layout", str(path), "-"],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    num = r'"([\d.]+)"'
    pages = []  # per page: [(y, side, text)], side 0 spans the page, 1 left, 2 right
    for page in re.finditer(rf'<page width={num}[^>]*>(.*?)</page>', xml, re.S):
        mid = float(page.group(1)) / 2
        parts = []
        for line in re.finditer(rf'<line xMin={num} yMin={num}[^>]*>(.*?)</line>', page.group(2), re.S):
            y = float(line.group(2))
            words = [(float(a), float(b), html.unescape(t)) for a, b, t in
                     re.findall(rf'<word xMin={num} yMin="[\d.]+" xMax={num}[^>]*>(.*?)</word>', line.group(3))]
            text = " ".join(t for _, _, t in words)
            left = [t for a, b, t in words if (a + b) / 2 < mid]
            if any(a < mid - 3 and b > mid + 3 for a, b, _ in words) or (left and len(left) < len(words) and text.isupper()):
                parts.append((y, 0, text))
                continue
            right = [t for a, b, t in words if (a + b) / 2 >= mid]
            parts += [(y, side, " ".join(ws)) for side, ws in ((1, left), (2, right)) if ws]
        pages.append(parts)

    # Page numbers run with the page index; find that offset by vote so they
    # are not taken for the numbers the text itself carries.
    votes = {}
    for i, parts in enumerate(pages):
        for k in {int(t) - i for _, _, t in parts if t.isdigit()}:
            votes[k] = votes.get(k, 0) + 1
    offset = max(votes, key=votes.get, default=None)
    if offset is not None and votes[offset] < len(pages) / 3:
        offset = None

    out = []
    for i, parts in enumerate(pages):
        parts = [p for p in parts if offset is None or p[2] != str(offset + i)]
        cuts = sorted(y for y, side, _ in parts if side == 0)
        band = lambda y: sum(c <= y for c in cuts)
        parts.sort(key=lambda p: (band(p[0]), p[1] != 0, p[1], p[0]))
        out.append("\n".join(text for _, _, text in parts))
    return out


JOURNAL_HEAD = re.compile(r"^\s*((?:BÜNTETŐ|POLGÁRI|KÖZIGAZGATÁSI|GAZDASÁGI|MUNKAÜGYI|JOGEGYSÉGI PANASZ)"
                          r" (?:KOLLÉGIUM|SZAKÁG|TANÁCS))\s*$", re.M)
# A decision's own reference stands on a line of its own after its text;
# citations of other rulings sit inside sentences.
JOURNAL_REF = re.compile(r"^\s*\(((?:Kúria|Legfelsőbb Bíróság) [^()]{5,80})\)\s*$", re.M)


def journal_decisions(pages):
    """[(section, serial, headnote, reference)] from a Kúriai Döntések issue."""
    body = "\n".join(p for p in pages if not re.search(r"^\s*(?:TARTALOM|Tartalom)\s*$", p, re.M))
    # Running heads name the section in title case: "Munkaügyi Szakág".
    body = re.sub(r"^\s*(?:Büntető|Polgári|Közigazgatási|Gazdasági|Munkaügyi|Jogegységi Panasz)"
                  r" (?:Kollégium|Szakág|Tanács)\s*$", "", body, flags=re.M)
    marks = sorted([(m.start(), m.end(), "head", m.group(1)) for m in JOURNAL_HEAD.finditer(body)] +
                   [(m.start(), m.end(), "ref", m.group(1)) for m in JOURNAL_REF.finditer(body)])
    out, section, pos = [], "", 0
    for start, end, kind, value in marks:
        if kind == "head":
            section = value  # a SZAKÁG heading follows its KOLLÉGIUM, so the last one wins
            pos = end
            continue
        chunk = body[pos:start]
        pos = end
        # A decision opens with its headnote and serial number, then the
        # facts; anything shorter than that is a citation inside the text.
        m = re.search(r"A felülvizsgálat alapjául|\[1\]|Az ügy tárgya|A tényállás", chunk)
        if not m or m.start() < 40:
            continue
        head = chunk[:m.start()]
        serial = re.search(r"^\s*(\d{1,4})\s*$", head, re.M)
        head = re.sub(r"^\s*\d{1,4}\s*$", "", head, flags=re.M)
        last = re.split(r"\n\s*\[\d+\]\s*", chunk)[-1]
        verdict = squash(last) if re.search(r"hatályában fenntart|hatályon kívül|helybenhagy|megváltoztat|elutasít|megsemmisít", last) else ""
        if serial:
            out.append((section, int(serial.group(1)), squash(head), value, clip(verdict, 900)))
    # Serials run on through the year; a number far off the issue's run is
    # a page number or paragraph caught next to a citation.
    if out:
        median = sorted(d[1] for d in out)[len(out) // 2]  # (section, serial, head, ref, verdict)
        out = [d for d in out if abs(d[1] - median) < 100]
    return out


def check_kuria_journal(cfg, scorer, seen, cutoff, errors):
    page = main_content(fetch(KURIA + KURIA_JOURNAL).decode("utf-8", "replace"))
    issues = re.findall(r'href="(/sites/default/files/kuriai_dontesek/[^"]+\.pdf)"[^>]*>([^<]+)<', page)
    if not issues:
        raise RuntimeError("nincs lapszám az oldalon; megváltozott a szerkezete?")
    hits, marked = [], {}
    for i, (path, label) in enumerate(issues):
        url = KURIA + path
        if url in seen:
            continue
        # The list carries no dates; with a cutoff, take one monthly issue
        # per month of the window (a first run: only the newest).
        if cutoff and i >= max(1, (dt.datetime.now(cutoff.tzinfo) - cutoff).days // 31):
            marked[url] = ""
            continue
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(fetch(url))
            tmp.flush()
            pages = pdf_columns(tmp.name)
        m = re.search(r"(\d{4})/(\d{2})", pages[0])
        year = m.group(1) if m else ""
        # The list has no dates, but each file is named for its upload day
        # ("bh_szeptember_0926.pdf", "bh_januar_20260121.pdf").
        up = re.search(r"_(\d{4})?(\d{2})(\d{2})(?:_[\d_]*)?\.pdf$", path)
        published = ""
        if up and m and abs(int(up.group(2)) - int(m.group(2))) <= 1:
            published = f"{up.group(1) or year}-{up.group(2)}-{up.group(3)}"
        decisions = journal_decisions(pages)
        if not decisions:
            errors.append(f"Kúriai Döntések, {label.strip()}: nem találtam benne határozatot")
            continue
        for section, serial, head, ref, verdict in decisions:
            if section.startswith("JOGEGYSÉGI"):
                continue  # these come from the uniformity lists
            score, terms = scorer.score(head)
            if "MUNKAÜGYI" not in section and score < cfg["ab_min_score"]:
                continue
            hits.append({
                "source": "kuria_bh",
                "id": f"{url}#{ref}",
                "date": published or (f"{year}-{m.group(2)}" if m else ""),
                "issue": f"Kúriai Döntések {year}/{m.group(2) if m else ''} ({label.strip()})",
                "url": url,
                "ref": f"BH {year}.{serial}" if serial and year else ref,
                "case": ref,
                "section": section.capitalize(),
                "outcome": verdict,
                "title": clip(head),
                "terms": terms,
            })
        marked[url] = f"{year}-{m.group(2)}" if m else ""
    return hits, marked


# --- Alkotmánybíróság ------------------------------------------------------

def hu_date(text):
    y, month, d = re.match(r"(\d{4})\.\s*(\w+)\s+(\d{1,2})\.", text.strip()).groups()
    return dt.date(int(y), HU_MONTHS.index(month.lower()) + 1, int(d))


def check_ab(cfg, scorer, seen, cutoff):
    page = fetch(AB_LATEST).decode("utf-8", "replace")
    blocks = page.split('class="decision-item')[1:]
    if not blocks:
        raise RuntimeError("nincs döntés az oldalon; megváltozott a szerkezete?")
    hits, marked = [], {}
    for b in blocks:
        def field(label):
            m = re.search(rf"<strong>\s*{label}:\s*</strong>\s*<span>(.*?)</span>", b, re.S)
            return strip_tags(m.group(1)) if m else ""

        date = hu_date(re.search(r'class="date">([^<]+)<', b).group(1))
        kind = strip_tags(re.search(r"<h2>(.*?)</h2>", b, re.S).group(1))
        case, subject = field("Ügyszám"), field("Az ügy tárgya")
        pdf = re.search(r'href="([^"]+\.pdf)"', b)
        record = re.search(r'href="([^"]*ugyadatlap[^"]*)"', b)
        key = pdf.group(1) if pdf else f"{case}|{kind}|{date}"
        if key in seen:
            continue
        marked[key] = date.isoformat()
        if cutoff and date < cutoff.date():
            continue
        score, terms = scorer.score(subject)
        if score < cfg["ab_min_score"]:
            continue
        parts = {}
        if pdf:
            try:
                parts = decision_parts(fetch_pdf_text(pdf.group(1), layout=False))
            except Exception:
                pass  # reported with its subject only
        hits.append({
            "outcome": parts.get("outcome", ""),
            "technical": technical_order(cfg, "végzés" if "végzés" in kind else "", "", parts.get("outcome", "")),
            "source": "ab",
            "id": key,
            "date": date.isoformat(),
            "ref": case,
            "kind": kind,
            "title": subject,
            "pdf": pdf.group(1) if pdf else None,
            "url": record.group(1) if record else None,
            "score": score,
            "terms": terms,
        })
    return hits, marked


# --- Report ----------------------------------------------------------------

COURT_RANK = [("Kúria", 0), ("Ítélőtábla", 1), ("Törvényszék", 2)]


def court_rank(court):
    return next((r for word, r in COURT_RANK if word in court), 3)


def clip(text, n=700):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def where(c):
    return f'{c["statute"]} {c["section"]}. §' + (f' ({c["par"]})' if c.get("par") else "") if c.get("section") else f'{c["statute"]} (cím)'


def report(hits, errors, now):
    technical = [h for h in hits if h.get("technical")]
    hits = [h for h in hits if not h.get("technical")]
    kozlony = [h for h in hits if h["source"] == "kozlony"]
    bhgy = [h for h in hits if h["source"] == "bhgy"]
    ab = [h for h in hits if h["source"] == "ab"]
    kuria = [h for h in hits if h["source"] == "kuria"]
    journal = [h for h in hits if h["source"] == "kuria_bh"]
    out = [f"# Jogfigyelő, {now:%Y. %m. %d. %H:%M}", ""]

    if kozlony:
        out += [f"## Magyar Közlöny: {len(kozlony)} jogszabály", ""]
        issues = {}
        for h in kozlony:
            issues.setdefault((h["date"], h["issue"], h["url"], h["pdf"]), []).append(h)
        for (date, issue, url, pdf), acts in sorted(issues.items(), reverse=True):
            out += [f"**[{issue}]({url})** ({date}, [PDF]({pdf}))", ""]
            for h in sorted(acts, key=lambda h: -(h.get("level") or 0)):
                page = f' · {h["page"]}. oldal' if h["page"] else ""
                out.append(f'- **{h["ref"]}**: {h["title"]}{" · **ERŐS JELZÉS**" if h.get("strong") else ""}  ')
                changes = h.get("changes") or []
                if changes:
                    out.append("  " + ", ".join(f'{where(c)} (súly {c["weight"]}{", szövegrész" if c.get("wording") else ""})' for c in changes[:8]) + page + "  ")
                if h.get("excerpt"):
                    out.append(f'  > {h["excerpt"]}  ')
                if h.get("effective"):
                    out.append(f'  Hatálybalépés: {h["effective"]}  ')
            out.append("")

    if bhgy:
        out += [f"## Bírósági határozatok: {len(bhgy)}", ""]
        labels = {}
        for h in bhgy:
            labels.setdefault(h["label"], []).append(h)
        for label, group in labels.items():
            out += [f"### {label} ({len(group)})", ""]
            # Parallel cases often carry the same principle word for word.
            same = {}
            for h in sorted(group, key=lambda h: (court_rank(h["court"]), h["court"], h["date"])):
                same.setdefault(h["summary"][:200].lower() or h["id"], []).append(h)
            for hs in same.values():
                refs = ", ".join(f'[{h["court"]} {h["ref"]}]({h["pdf"]})' for h in hs)
                dates = sorted({h["date"] for h in hs})
                form = f' · {hs[0]["form"]}' if hs[0].get("form") else ""
                out.append(f'- **{refs}**{form} · közzétéve {" – ".join(dates[::max(len(dates) - 1, 1)])}')
                if hs[0].get("outcome"):
                    out.append(f'  Döntés: {clip(hs[0]["outcome"], 500)}  ')
                if hs[0]["summary"]:
                    out.append(f'  > {clip(hs[0]["summary"])}')
            out.append("")

    if kuria or journal:
        out += [f"## Kúria: {len(kuria) + len(journal)}", ""]
        for h in sorted(kuria, key=lambda h: h["date"], reverse=True):
            level = "erős" if h.get("strong") else "lehetséges"
            out.append(f'- **{h["date"]} · {h["ref"]}** · {h["kind"]} · [oldal]({h["url"]})  ')
            if h["title"]:
                out.append(f'  {h["title"]}  ')
            out.append(f'  *{level}, {h["score"]} pont · {", ".join(h["terms"][:5])}*')
        if kuria:
            out.append("")
        issues = {}
        for h in journal:
            issues.setdefault((h["issue"], h["url"]), []).append(h)
        for (issue, url), group in issues.items():
            out += [f"### [{issue}]({url})", ""]
            for h in group:
                out.append(f'- **{h["ref"]}** ({h["case"]}) · {h["section"]}')
                out.append(f'  > {h["title"]}')
            out.append("")

    if ab:
        out += [f"## Alkotmánybíróság: {len(ab)}", ""]
        for h in sorted(ab, key=lambda h: h["date"], reverse=True):
            links = " · ".join(f"[{name}]({u})" for name, u in (("döntés", h["pdf"]), ("adatlap", h["url"])) if u)
            out.append(f'- **{h["date"]} · {h["kind"]}** · {h["ref"]} · {links}  ')
            out.append(f'  {h["title"]}  ')
            out.append(f'  *{", ".join(h["terms"][:5])}*')
        out.append("")

    if technical:
        out += [f"## Technikai tételek: {len(technical)}", ""]
        for h in technical:
            out.append(f'- {" ".join(x for x in (h.get("court"), h["ref"]) if x)}: {h["technical"]}')
        out.append("")

    if errors:
        out += ["## Hibák", ""] + [f"- {e}" for e in errors] + [""]
    return "\n".join(out)


# --- Main ------------------------------------------------------------------

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_jsonl(path):
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


DASHBOARD_FIELDS = ("ref", "title", "summary", "court", "label", "url", "pdf", "score", "strong",
                    "issue", "page", "kind", "section", "case", "found", "outcome", "form", "decided",
                    "technical", "excerpt", "marks", "effective", "changes", "level", "topic")


def export(data, out):
    """Write every find and run on record as the dashboard's data file.

    Rebuilt whole each time from talalatok.jsonl and runs.jsonl, so it is
    always complete; where a find was logged twice, the later record wins.
    """
    latest = {}
    for h in read_jsonl(data / "talalatok.jsonl"):
        key = (h["source"], h["id"])
        # A find logged again (a re-run with better extraction) keeps its
        # newer fields and the time it was first found.
        first = latest[key]["found"] if key in latest else h["found"]
        latest[key] = dict(h, found=min(first, h["found"]))
    rows = []
    for h in latest.values():
        row = {"k": h["source"], "id": h["id"],
               "day": h["date"] if len(h.get("date", "")) == 10 else h["found"][:10]}
        row.update({f: h[f] for f in DASHBOARD_FIELDS if h.get(f) not in (None, "", [])})
        if "summary" in row:
            row["summary"] = clip(row["summary"], 900)
        if h.get("terms"):
            row["terms"] = h["terms"][:5]
        rows.append(row)
    runs = read_jsonl(data / "runs.jsonl")
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    doc = {"generated": now.isoformat(timespec="seconds"), "hits": rows, "runs": runs}
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(rows)} találat, {len(runs)} futás: {out}")


def score_pdfs(paths, cfg, scorer, package):
    for path in paths:
        acts = score_acts(kozlony_acts(pdf_text(path)), scorer, cfg)
        print(f"== {path}: {len(acts)} aktus")
        if acts[0]["id"] is None:
            print("   (a tartalomjegyzék nem értelmezhető, a teljes szám egyben)")
        for a in acts:
            changes = package.changes(a) if a["id"] else []
            topic = package.topic(a["title"]) if a["id"] else None
            if topic and not changes:
                print(f'{topic["weight"]:3d} {"ERŐS" if topic["weight"] >= package.strong else "    "} {a["id"]:38s} {a["title"][:70]}')
                print(f'{"":10s}tárgy: {topic["note"]}')
            if not changes:
                continue
            mark = "ERŐS" if package.is_strong(changes) else "TECH" if all(c["wording"] for c in changes) else "    "
            print(f'{package.level(changes):3d} {mark} {a["id"]:38s} {a["title"][:70]}')
            for c in changes[:6]:
                where = f'{c["statute"]} {c["section"]}. §' + (f' ({c["par"]})' if c["par"] else "") if c["section"] else f'{c["statute"]} (cím)'
                print(f'{"":10s}{where:22s} súly {c["weight"]:2d}{" szövegrész" if c["wording"] else ""} | {c["clause"][:90]}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=HERE / "data", help="state and reports (default: %(default)s)")
    ap.add_argument("--config", type=Path, default=HERE / "config.toml")
    ap.add_argument("--since", type=int, metavar="DAYS", help="ignore anything unseen that is older than this")
    ap.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    ap.add_argument("--pdf", type=Path, nargs="+", metavar="FILE", help="score downloaded Közlöny issues and exit")
    ap.add_argument("--only", metavar="SOURCES", help="check only these sources, comma-separated (kozlony,bhgy,kuria,kuria_bh,ab)")
    ap.add_argument("--export", type=Path, metavar="FILE", help="write every find and run as the dashboard's data file and exit")
    args = ap.parse_args()
    if args.export:
        export(args.data, args.export)
        return 0

    cfg = tomllib.loads(args.config.read_text())
    scorer = Scorer(cfg)
    package = Package(tomllib.loads((HERE / "torvenyek.toml").read_text()))
    if not shutil.which("pdftotext"):
        sys.exit("pdftotext is missing: brew install poppler, or apt-get install poppler-utils")
    if args.pdf:
        score_pdfs(args.pdf, cfg, scorer, package)
        return 0

    now = dt.datetime.now(dt.timezone.utc).astimezone()
    state = load_json(args.data / "state.json", {"seen": {}})
    hits, errors = [], []
    # Decided up front: the second BHGY query must not see the first one's
    # marks and take the source for one that has run before.
    new_sources = {s for s in ("kozlony", "bhgy", "kuria", "kuria_bh", "ab") if s not in state["seen"]}

    # How far back a first run (or --since) looked, per source: the cutoff,
    # or the oldest item a source still lists where it keeps less than that.
    coverage = {}

    only = set(args.only.split(",")) if args.only else None

    def run(source, check):
        if only and source not in only:
            return
        seen = state["seen"].setdefault(source, {})
        days = args.since if args.since is not None else cfg["first_run_days"] if source in new_sources else None
        cutoff = now - dt.timedelta(days=days) if days is not None else None
        try:
            found, marked = check(seen, cutoff)
        except Exception as e:
            errors.append(f"{source}: {e}")
            return
        hits.extend(found)
        seen.update(marked)
        dates = [d for d in marked.values() if len(d) == 10]
        if cutoff and dates:
            start = max(cutoff.date().isoformat(), min(dates))
            coverage[source] = min(coverage.get(source, start), start)

    run("kozlony", lambda seen, cutoff: check_kozlony(cfg, scorer, package, seen, cutoff, errors))
    for query in cfg.get("bhgy", []):
        run("bhgy", lambda seen, cutoff, q=query: check_bhgy(cfg, q, seen, cutoff))
    run("kuria", lambda seen, cutoff: check_kuria_lists(cfg, scorer, seen, cutoff))
    run("kuria_bh", lambda seen, cutoff: check_kuria_journal(cfg, scorer, seen, cutoff, errors))
    run("ab", lambda seen, cutoff: check_ab(cfg, scorer, seen, cutoff))

    text = report(hits, errors, now) if hits or errors else None
    if args.dry_run:
        print(text or "Nincs új találat.")
    else:
        args.data.mkdir(parents=True, exist_ok=True)
        if text:
            reports = args.data / "jelentesek"
            reports.mkdir(exist_ok=True)
            path = reports / f"{now:%Y-%m-%d-%H%M}.md"
            path.write_text(text)
            with open(args.data / "talalatok.jsonl", "a") as log:
                for h in hits:
                    log.write(json.dumps(dict(h, found=now.isoformat(timespec="seconds")), ensure_ascii=False) + "\n")
            print(f"{len(hits)} új találat, {len(errors)} hiba: {path}")
        else:
            print("Nincs új találat.")
        write_atomic(args.data / "state.json", json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))
        counts = {}
        for h in hits:
            counts[h["source"]] = counts.get(h["source"], 0) + 1
        record = {"at": now.isoformat(timespec="seconds"), "found": counts, "errors": errors}
        if coverage:
            record["coverage"] = coverage
        with open(args.data / "runs.jsonl", "a") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
    for e in errors:
        print(f"hiba: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
