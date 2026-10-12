# Decisions

What was decided, why, and what was rejected. The README describes the
watcher as it is; this file keeps how it got there.

## 2026-10-12 · The page is built from a kept copy when amunka.hu refuses

The scheduled run on 2026-10-12 failed at Publish: `build_page.py` got
HTTP 403 from amunka.hu (its proxy answers as `RHProxy`) for `/rolunk`. The
runs of 9–11 October fetched the same page from GitHub, it answers 200 from
a home connection with the watcher's user agent and a browser's, and a re-run
on another runner published. So the host refuses some GitHub runner
addresses, not the request.

Each successful build now keeps the borrowed page as `data/chrome.html`, and
a refused fetch builds from that copy with a warning. Tested by pointing the
build at a page that does not exist: the result was byte-identical to the
live build. "Keep the state" moved after Publish and runs whatever failed, so
the copy is committed even on a failed run.

Rejected: retrying the fetch (a refusal is not transient for that runner);
building the page only when `dashboard.html` changes (the page would miss
menu and theme changes on the site).

## 2026-10-08 · Summaries are one sentence

Each find gets a one-sentence summary from `claude-haiku-5-5` (at most about
30 words; one over a sentence or 40 words is sent back once to be shortened).
The owner cut, in turn: the importance rating (237 of 340 finds came out
"magas", so the pill marked nothing), the reason line under it, the separate
before/after list for amended sections, and the three-sentence length. The
model is not named on the page, in the feed or in `data.json`; the footer
says the summary is automatic and can be wrong.

Measured: the first full pass cost $1.19 for 340 finds (9.6 M input tokens,
the full decision or act each time); each later full pass about $1.15. A
month of new finds costs about $0.40–0.60.

## 2026-10-08 · A summary overrules the technical flag only for gazette acts

The first full pass read 14 rule-technical entries as substantive. One was
right: an AB decision annulling a phrase of the Mmtv., which the rules took
for a wording change because it says "szövegrész". The other 13 were court
orders, mostly refusals to admit a review, whose reasoning restates case
law; they had been hidden on purpose. Court flags come from case type and
outcome, so they stand.

## 2026-10-08 · The old wording comes from weekly njt.hu copies

njt.hu shows only the text in force today, and older time states need a
login, so by the time an amendment is read the site may already show the new
wording. The run keeps its own copy of each statute in `torvenyek.toml`,
refreshed weekly after the summaries, so an amendment is read against the
text from before it was published. A long statute arrives with only its
first part; the rest comes from `njtGetBlock.json` in one request (the Hszt.
went from 248 to 567 sections).

A source that answers 404 or 410 is summarised from what was stored when it
was found (the AB decision IV/2294/2025's PDF disappeared), instead of being
retried daily.

## 2026-10-08 · The daily run is on GitHub Actions

The owner wanted a daily run that does not depend on a Mac. The workflow
checks the sources, summarises, publishes over FTP with TLS (the host accepts
it; the password had gone in clear), and keeps the state on the `data`
branch, which holds what `data.json` already publishes plus the njt.hu
copies. Every source answered GitHub's runners on the first run; each
scraper raises on an empty or unreadable page, so a block would have shown.

Rejected: a launchd job on the Mac (the owner's call); a scheduled cloud
Claude session doing the scraping (an agent run per day, less predictable
than plain code, and credentials in another environment).

## 2026-09-28 · Base-package changes, not keyword weights, select gazette acts

Balázs judged the keyword weighting unsound. Gazette acts are reported when
they amend a statute of the base labour-law package (`torvenyek.toml`, with
per-section weights: Mt. 69. § notice period 10, Mt. 102. § public holidays
2) or when their title names a weighted topic (which days are worked, the
minimum wage). Acts that only swap wording are technical and hidden by
default, as are procedural court orders. Weights other than the two Mt.
sections are placeholders until Balázs's earlier amending acts calibrate
them.
