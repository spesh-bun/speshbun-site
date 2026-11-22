# Scout

The collector that looks for unknown, finished releases on YouTube.

- `collect.py` runs on GitHub Actions. It needs the `YOUTUBE_API_KEY`
  repository secret and nothing else.
- Add a file to `requests/` on the `scout` branch to start a run. What it
  found is written to `results/` under the same name.
- Each search in a request costs one of the 100 daily YouTube searches.
