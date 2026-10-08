#!/usr/bin/env python3
"""Ask Claude Haiku what each find means, and keep the answers.

For every find on DATA/talalatok.jsonl that has no analysis yet on
DATA/elemzesek.jsonl, the source is read in full (the decision's PDF, the
Kúria's page, the gazette act re-read from its issue) and sent to Claude
Haiku in one request, which answers with:

  impact      what it means for workers, in two or three plain sentences
  importance  magas / közepes / alacsony, and a sentence on why
  changes     for a gazette act: each amended section, before and after
  technical   whether it is only technical, and why

The answer is appended to DATA/elemzesek.jsonl, and jogfigyelo.py --export
puts it beside the find on the dashboard and in the feed. A find whose source
cannot be read, or whose request fails, is tried again on the next run.

The wording a gazette act replaces comes from DATA/njt/: a copy of each
statute in torvenyek.toml from the Nemzeti Jogszabálytár (njt.hu), which
shows only the text in force today. The copies are refreshed weekly, after
the run's analyses, so an amendment is read against the text from before it
was published even where njt.hu has already consolidated it.

  python3 elemzes.py              analyse every find without an analysis
  python3 elemzes.py --limit 5    at most five, newest first
  python3 elemzes.py --dry-run    print the first request and the sizes, send nothing

Needs ANTHROPIC_API_KEY and the anthropic package (pip install anthropic);
--dry-run needs neither.
"""
import argparse
import concurrent.futures
import datetime as dt
import json
import re
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

import jogfigyelo as jf

MODEL = "claude-haiku-5-5"
NJT = "https://njt.hu/jogszabaly/"
NJT_BLOCKS = "https://njt.jog.gov.hu/ajax/njtGetBlock.json"
NJT_DAYS = 7
WORKERS = 4

SYSTEM = """\
You analyse new Hungarian labour-law material for a Munka (amunka.hu), a \
workers' organisation. Its watch page lists new gazette acts and court \
decisions for workers, shop stewards and labour lawyers, and your analysis \
appears under each entry, marked as machine-written.

Write every field in Hungarian, in plain language a worker understands; keep \
a legal term only where there is no plain word for it. Work only from the \
text you are given. Do not add facts, section contents or case law from \
memory. Where the text does not say something, say so rather than guess.

impact: two or three sentences on who is affected (which workers, employers \
or sector) and what changes for them in practice. For a court decision, the \
general point a worker in a similar situation can rely on, not the story of \
the parties.

importance: how much this matters to workers.
- magas: it changes or settles a core worker protection: notice period, \
dismissal and its protections, severance, wages and the minimum wage, working \
time, rest and overtime, public holidays and which days are worked, leave, \
health and safety duties, strike, union and works council rights, collective \
agreements; or a Kúria or court of appeal decision on such a question.
- közepes: it matters to one group (public servants, teachers, health \
workers, the armed services) or concerns a narrower right; or a procedural \
decision that changes how workers can enforce a claim.
- alacsony: renaming institutions, reorganising authorities, updating cross \
references, a purely procedural order, or a case decided on its own facts \
with no lesson for others.
importance_reason: one sentence.

changes: only for a gazette act, one entry per amended section of the \
followed statutes listed in the request, in the order given. "where" is the \
location as given ("Mt. 69. § (1)"). "before" is what the rule said, in one \
sentence, taken from the current text supplied with it; leave it empty when \
no current text is supplied or when the supplied text already reads like the \
new wording (the amendment is then already in force there). "after" is what \
the rule says under the amendment, in one sentence. For a court decision, an \
empty list.

technical: true when it changes nothing of substance in workers' rights and \
duties: only swapping wording, renaming bodies, moving competences between \
authorities, or a court order that settles procedure (rejecting an appeal as \
inadmissible, correcting a clerical error, suspending, fixing costs) without \
deciding the claim. Otherwise false.
technical_reason: one sentence."""

SCHEMA = {
    "type": "object",
    "properties": {
        "impact": {"type": "string"},
        "importance": {"type": "string", "enum": ["magas", "közepes", "alacsony"]},
        "importance_reason": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"where": {"type": "string"}, "before": {"type": "string"}, "after": {"type": "string"}},
                "required": ["where", "before", "after"],
                "additionalProperties": False,
            },
        },
        "technical": {"type": "boolean"},
        "technical_reason": {"type": "string"},
    },
    "required": ["impact", "importance", "importance_reason", "changes", "technical", "technical_reason"],
    "additionalProperties": False,
}


# --- Nemzeti Jogszabálytár --------------------------------------------------

ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
SECTION_ANCHOR = re.compile(r'<span class="jhId" id="SZ(\d+[A-Z]?)"></span>')
# A section runs to the next one, or to a heading or the footnotes before it.
SECTION_END = re.compile(r'<div id="sc[^"]*" class="(?:alcim|fejezet|fejezetCim|resz|reszcim)"|<div class="footnote"')


def roman(numeral):
    values = [ROMAN[c] for c in numeral]
    return sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))


def njt_id(number):
    """"2012. évi I. törvény" as njt.hu numbers it: "2012-1-00-00"."""
    m = re.match(r"(\d{4})\.\s*évi\s+([IVXLCDM]+)\.\s*törvény", number)
    return f"{m.group(1)}-{roman(m.group(2))}-00-00" if m else None


def njt_sections(page):
    """{"69": "69. § (1) A felmondási idő harminc nap. …", "69/A": …} of a statute's njt.hu page."""
    marks = list(SECTION_ANCHOR.finditer(page))
    out = {}
    for m, nxt in zip(marks, marks[1:] + [None]):
        stop = nxt.start() if nxt else len(page)
        end = SECTION_END.search(page, m.end(), stop)
        chunk = re.sub(r'<sup class="fnSup".*?</sup>', "", page[m.end():end.start() if end else stop], flags=re.S)
        chunk = re.sub(r"</?a\b[^>]*>", "", chunk)  # cross-references sit inside words: "pont<a>ban</a>"
        out[re.sub(r"(\d+)([A-Z])$", r"\1/\2", m.group(1))] = jf.strip_tags(chunk)
    return out


def snapshot_path(data, ident):
    return data / "njt" / f"{ident}.json"


def njt_page(ident):
    """A statute's whole text on njt.hu: the page, and the blocks it loads later.

    A long statute arrives with only its first part; the rest are empty
    placeholders, each block's first one marked borderStart, which the page
    fills from njtGetBlock.json as the reader scrolls. One request asks for
    them all.
    """
    page = jf.fetch(NJT + ident).decode("utf-8", "replace")
    borders = [dict(re.findall(r'data-([a-z-]+)="(\d+)"', b))
               for b in re.findall(r'<div id="sc[^"]*" class="pH borderStart"([^>]*)>', page)]
    if not borders:
        return page
    blocks = [{"start": int(b["show-order"]), **({"last": int(b["last-show-order"])} if "last-show-order" in b else {})}
              for b in borders]
    req = urllib.request.Request(NJT_BLOCKS, data=json.dumps({"documentId": ident, "data": blocks}).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": jf.UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        return page + r.read().decode("utf-8", "replace")


def fetch_snapshot(statute, data):
    ident = njt_id(statute["number"])
    sections = njt_sections(njt_page(ident))
    if not sections:
        raise RuntimeError(f"njt.hu {ident}: nincs szakasz az oldalon; megváltozott a szerkezete?")
    snap = {"statute": statute["short"], "url": NJT + ident, "fetched": dt.date.today().isoformat(), "sections": sections}
    path = snapshot_path(data, ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    jf.write_atomic(path, json.dumps(snap, ensure_ascii=False, indent=0))
    return snap


def snapshot(statute, data):
    """The statute's copy from njt.hu, taken now if there is none yet."""
    ident = njt_id(statute.get("number", ""))
    if not ident:
        return None
    return jf.load_json(snapshot_path(data, ident), None) or fetch_snapshot(statute, data)


def refresh_snapshots(statutes, data, errors):
    today = dt.date.today()
    for st in statutes:
        ident = njt_id(st.get("number", ""))
        if not ident:
            continue
        snap = jf.load_json(snapshot_path(data, ident), None)
        if snap and (today - dt.date.fromisoformat(snap["fetched"])).days < NJT_DAYS:
            continue
        try:
            fetch_snapshot(st, data)
        except Exception as e:
            errors.append(f"njt.hu, {st['short']}: {e}")
        time.sleep(1)


# --- What is sent ------------------------------------------------------------

def where(c):
    return f'{c["statute"]} {c["section"]}. §' + (f' ({c["par"]})' if c.get("par") else "") if c.get("section") else f'{c["statute"]} (cím)'


def gazette_request(h, package, data):
    """The act re-read from its issue, its amendments with the text they replace."""
    act_id = h["id"].split("#", 1)[1]
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(jf.fetch(h["pdf"]))
        tmp.flush()
        acts = jf.kozlony_acts(jf.pdf_text(tmp.name))
    act = next((a for a in acts if (a["id"] or "") == act_id), None)
    if act is None:
        raise RuntimeError(f"{h['ref']}: nincs meg a jogszabály a lapszámban")
    by_short = {st["short"]: st for st in package.statutes}
    lines = [f"Forrás: {h.get('issue', 'Magyar Közlöny')}, {h.get('page', '?')}. oldal",
             f"Jogszabály: {h['ref']} – {h.get('title', '')}"]
    if h.get("effective"):
        lines.append(f"Hatálybalépés: {h['effective']}")
    changes = package.changes(act)
    if changes:
        lines.append("\nA követett törvényeket érintő módosítások:")
    for i, c in enumerate(changes, 1):
        lines.append(f"\n{i}. {where(c)}")
        lines.append(f"Módosító rendelkezés: {c['clause']}")
        if c["new"]:
            lines.append(f"Új szöveg: „{c['new']}”")
        snap = snapshot(by_short[c["statute"]], data) if c["section"] else None
        old = snap and snap["sections"].get(c["section"])
        if old:
            lines.append(f"Hatályos szöveg a Nemzeti Jogszabálytárban ({snap['fetched']}-i másolat): {old}")
        elif c["section"]:
            lines.append("Hatályos szöveg: nem áll rendelkezésre.")
    lines.append(f"\nA jogszabály teljes szövege:\n{jf.squash(act['body'])}")
    return "\n".join(lines)


def decision_request(h):
    k = h["source"]
    head = {"bhgy": f"{h.get('court', '')} {h.get('ref', '')} ({h.get('label', '')})",
            "ab": f"Alkotmánybíróság {h.get('ref', '')} ({h.get('kind', '')})",
            "kuria": f"Kúria: {h.get('ref', '')}",
            "kuria_bh": f"Kúriai Döntések, {h.get('ref', '')}, {h.get('case', '')}"}[k]
    lines = [f"Forrás: {head}"]
    if h.get("form"):
        lines.append(f"Döntés: {h['form']}" + (f", {h['decided']}" if h.get("decided") else ""))
    if h.get("title") and k != "kuria":
        lines.append(f"{'Tárgy' if k == 'ab' else 'Elvi tartalom'}: {h['title']}")
    if h.get("summary"):
        lines.append(f"Elvi tartalom (a bíróság összefoglalója): {h['summary']}")
    if h.get("outcome"):
        lines.append(f"Rendelkező rész: {h['outcome']}")
    if k in ("bhgy", "ab") and h.get("pdf"):
        lines.append(f"\nA határozat teljes szövege:\n{jf.squash(jf.fetch_pdf_text(h['pdf'], layout=False))}")
    elif k == "kuria" and h.get("url"):
        lines.append(f"\nAz oldal szövege:\n{jf.strip_tags(jf.main_content(jf.fetch(h['url']).decode('utf-8', 'replace')))}")
    # Kúriai Döntések: the headnote and the operative part above are what the
    # journal's issue gives for one decision.
    return "\n".join(lines)


def request_text(h, package, data):
    return gazette_request(h, package, data) if h["source"] == "kozlony" else decision_request(h)


# --- Asking ------------------------------------------------------------------

def ask(client, text):
    """One request; the parsed answer, or a record of why there is none."""
    import anthropic

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
        return None, f"átmeneti hiba: {e}"  # tried again on the next run
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    if resp.stop_reason == "refusal":
        category = resp.stop_details.category if resp.stop_details else None
        return {"refused": category or True, "usage": usage}, None
    if resp.stop_reason == "max_tokens":
        return None, "a válasz elérte a max_tokens korlátot"
    answer = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return dict(answer, usage=usage), None


def latest_finds(data):
    latest = {}
    for h in jf.read_jsonl(data / "talalatok.jsonl"):
        latest[(h["source"], h["id"])] = h
    return sorted(latest.values(), key=lambda h: (h.get("date", ""), h.get("found", "")), reverse=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=jf.HERE / "data")
    ap.add_argument("--limit", type=int, help="analyse at most this many finds, newest first")
    ap.add_argument("--dry-run", action="store_true", help="print the first request and every request's size, send nothing")
    args = ap.parse_args()

    package = jf.Package(tomllib.loads((jf.HERE / "torvenyek.toml").read_text()))
    out = args.data / "elemzesek.jsonl"
    done = {(a["source"], a["id"]) for a in jf.read_jsonl(out)}
    todo = [h for h in latest_finds(args.data) if (h["source"], h["id"]) not in done][:args.limit]
    errors = []

    if args.dry_run:
        for i, h in enumerate(todo):
            try:
                text = request_text(h, package, args.data)
            except Exception as e:
                print(f"{h['source']} {h.get('ref')}: {e}")
                continue
            if i == 0:
                print(f"--- system ---\n{SYSTEM}\n--- user ---\n{text}\n---")
            print(f"{h['source']:8s} {h.get('ref', '')[:40]:40s} {len(text):7d} karakter")
        print(f"{len(todo)} elemzetlen találat")
        return 0

    import anthropic

    client = anthropic.Anthropic()

    def work(h):
        text = request_text(h, package, args.data)
        return ask(client, text)

    analysed = 0
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool, out.open("a", encoding="utf-8") as log:
        futures = {pool.submit(work, h): h for h in todo}
        for fut in concurrent.futures.as_completed(futures):
            h = futures[fut]
            try:
                answer, failure = fut.result()
            except anthropic.APIStatusError as e:
                answer, failure = None, f"API-hiba {e.status_code}: {e.message}"
            except Exception as e:
                answer, failure = None, f"{type(e).__name__}: {e}"
            if failure:
                errors.append(f"elemzés, {h['source']} {h.get('ref', '')}: {failure}")
                continue
            log.write(json.dumps(dict(answer, source=h["source"], id=h["id"], model=MODEL,
                                      at=dt.datetime.now().astimezone().isoformat(timespec="seconds")), ensure_ascii=False) + "\n")
            log.flush()
            analysed += 1

    refresh_snapshots(package.statutes, args.data, errors)
    print(f"{analysed}/{len(todo)} elemzés: {out}")
    for e in errors:
        print(f"hiba: {e}")
    # Transient failures are retried next run; they fail the run only when
    # nothing got through, which is how a bad key or an outage looks.
    return 1 if todo and not analysed else 0


if __name__ == "__main__":
    sys.exit(main())
