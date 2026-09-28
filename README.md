# Jogfigyelő

Watches the official gazette and the courts for new labour-law material and
publishes what it finds at **https://amunka.hu/jogfigyelo/**.

| Source | What is reported |
|---|---|
| Magyar Közlöny (`magyarkozlony.hu/feed`) | Each act of each new issue, scored against the labour-law patterns in `config.toml` |
| Anonymized court decisions (`eakta.birosag.hu`) | Every new decision of the labour chamber, and public-service disputes |
| Kúria (`kuria-birosag.hu`) | Labour-related uniformity decisions, uniformity complaints and press releases; the monthly *Kúriai Döntések*, labour section whole |
| Alkotmánybíróság (`alkotmanybirosag.hu`) | New decisions whose subject is labour-related |

## Running it

```
python3 jogfigyelo.py              # check every source, report what is new
./deploy.sh --data                 # publish the updated data.json
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

What counts as labour-related is in `config.toml`: weighted patterns, the
score thresholds, which court-decision queries to run. Try a change on a
downloaded gazette issue before relying on it:

```
python3 jogfigyelo.py --pdf MK_26_138.pdf
```

Gazette acts are scored by pattern density rather than presence, so an
incidental mention in a long act counts for little; a title match counts
three times, and an amendment to a core labour act always counts in full.
