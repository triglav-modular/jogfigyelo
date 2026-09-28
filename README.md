# Jogfigyelő

Watches the official gazette and the courts for new labour-law material and
publishes what it finds at **https://amunka.hu/jogfigyelo/**, with an RSS
feed of the latest 100 finds at https://amunka.hu/jogfigyelo/feed.xml.

| Source | What is reported |
|---|---|
| Magyar Közlöny (`magyarkozlony.hu/feed`) | Each act that amends a statute of the base labour-law package (`torvenyek.toml`), with the sections it changes, the amending text and the entry into force; and acts whose title is labour-related |
| Anonymized court decisions (`eakta.birosag.hu`) | Every new decision of the labour chamber, and public-service disputes, with the operative part of the decision |
| Kúria (`kuria-birosag.hu`) | Labour-related uniformity decisions, uniformity complaints and press releases; the monthly *Kúriai Döntések*, labour section whole |
| Alkotmánybíróság (`alkotmanybirosag.hu`) | New decisions whose subject is labour-related, with their operative part |

## Running it

```
python3 jogfigyelo.py              # check every source, report what is new
./deploy.sh --data                 # publish the updated data.json and feed.xml
```

A run writes a Markdown report to `data/jelentesek/`, appends its finds to
`data/talalatok.jsonl` and itself to `data/runs.jsonl`, and remembers what it
has seen in `data/state.json`. `data/` is local and not in git. A source that
fails is left unmarked, retried on the next run, and the run exits 1.

`./deploy.sh` with no argument also rebuilds and uploads the page; `--dry-run`
lists what it would upload. It needs `rclone`, and a `.env` with `FTPhost`,
`FTPuser` and `FTPpass`.

The page is `web/dashboard.html`, the dashboard alone. `build_page.py` puts it
inside the chrome of a live amunka.hu page (head, header and menu, footer,
cookie consent), so it carries the site's current design without a copy of
it here, and writes `web/index.html`. The folder sits outside Grav: the
site's rewrite rules serve real folders directly, and the aMunka repo's
`sync-from-server.sh` excludes it.

Requires Python 3.11+ and poppler (`pdftotext`, `pdfinfo`).

## Tuning

**Gazette acts** are matched against the base package in `torvenyek.toml`:
the statutes followed, a weight for each, and weights for single sections
where one matters more or less than the rest (Mt. 69. §, the notice period,
is 10; Mt. 102. §, public holidays, is 2). An act that changes a section of
weight `strong_weight` or more is a strong alert. A change that only swaps or
strikes out a phrase never is. See what an issue would trigger with:

```
python3 jogfigyelo.py --pdf MK_26_138.pdf
```

**Technical entries** are reported but hidden on the dashboard by default:
acts that only swap or strike out wording, and court orders that settle
procedure rather than the claim (`technical_case_types`, `technical_orders`
in `config.toml`).

The older keyword weighting (`keyword_scoring` in `config.toml`) is switched
off; court decisions and Kúria press releases are still matched on keywords.
