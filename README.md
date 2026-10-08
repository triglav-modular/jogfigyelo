# Jogfigyelő: the watcher's state

What the daily run (main branch, `.github/workflows/daily.yml`) has seen, found and summarised,
committed after every run. `main` checks this branch out as `data/`.

- `state.json`: what each source has listed so far
- `talalatok.jsonl`: every find, one per line
- `elemzesek.jsonl`: the summary of each find (elemzes.py)
- `runs.jsonl`: one line per run, with its errors
- `jelentesek/`: each run's report
- `njt/`: the followed statutes as njt.hu showed them, refreshed weekly
