# RWX Blast Radius workspace

The [RWX track](https://www.joinbuilders.org/buildathon/tracks/blast-radius)
asks you to pick the SQLGlot tests that might fail for each patch, then split
them across at most eight workers. The score is the slowest worker's recorded
test time, plus scheduling overhead, a penalty for each patch with a missed
failure, and the selector's own CPU time. Lower is better.

## Get both teammates running the same setup

1. Use a Linux x86-64 Devin environment scoped to this repository. Use the
   same environment for both teammates. Docker must be running and accessible
   to your user; the host also needs GNU Make and Python 3.9 or newer.
   A local machine with those tools works too: the Makefile targets
   `linux/amd64` explicitly, even on an ARM host.
2. Clone the repository into a path without spaces. In the repo root, run
   `docker version` (check for both Client and Server), `make --version`,
   and `python3 --version`, then run `make sample`.
3. Start changes on separate branches. Include the sample score and miss
   count in each pull request. Use `make check` for output validation, not
   as a substitute for checking the miss count.

## Selection and scheduling interface

- `config.py` defines the frozen experiment `Config`: selector and scheduler
  classes, worker count (default 3), and data directory (default `/data`).
  `DEFAULT_CONFIG` uses `AllTestsSelector` and `LptScheduler`.
- `strategies/selectors/` defines `Selector`, `CatalogTest`, and the `Patch`
  record (commit id and diff text). A selector is constructed once with
  `(catalog, config)`. Its `select(commits)` method processes the batch and
  returns a mapping from commit id to unique catalog test numbers.
  `AllTestsSelector` currently selects every test for every commit.
- `MapSelector` is opt-in via `Config(selector_cls=MapSelector)`. It reads a
  precomputed file-to-test map only; it does not import the patched source,
  collect tests, launch subprocesses, or probe imports. Unknown, empty,
  malformed, quoted, binary, or unsupported changes select all tests.
- `strategies/schedulers/` defines `Scheduler`, constructed once with
  `(durations, config)`. Its `schedule(selected)` method returns worker lists
  containing each selected number exactly once, or `[]` for no tests, dropping
  empty workers. `LptScheduler` uses at most `config.workers` workers.
- The container entrypoint, `main.py`, selects and schedules tests for each
  patch, caching identical consecutive selections, and writes one
  `{"commit": "...", "workers": [...]}` JSON line. Keep selection
  fixed when measuring scheduling changes and vice versa.

## Reference result

The interface split preserves the official starter's behavior: run
all 1,620 tests on three workers.
On the 30 public patches, `make sample` reports **1,210.470 simulated
seconds, zero misses**. The local sample scorer does not measure container
CPU; do not compare this score directly with the published leaderboard
baseline, which is not the local sample score.

Treat `sample/answers.json` as scorer-only data. The sample runner mounts
only the patches and the test list, and the Docker build excludes the
sample directory. Keep the [scorer restrictions](SCORING.md) in place:
no network at runtime, a read-only filesystem, and the unprivileged
`nobody` user.

The official starter files are from the
[track download](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz).

## Map-only selector

`strategies/selectors/map_only.py` reads
`strategies/selectors/file_map.json.gz`, an offline export of the pinned
Don coverage index. The exporter accepts only the index SHA-256
`9bb245fa41d26ddfbb5866d0791c969033d130ea93272d3742fe7491f3188107`, checks
it before unpickling, and verifies the catalog identity
`f3bdf0f41f64d10ff0d9edfa93b4c9bd0c296a8e770678d0a40d372c9882fc34`.
The donor source is
`hackathon-don/rwx-blast-radius@d8e4a935eb4261fa6dd32080b0614afbb3fc8b45`,
path `maps/index.pkl.gz`; its SQLGlot base is
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

Re-export from a trusted checkout:

```sh
git clone https://github.com/hackathon-don/rwx-blast-radius.git "$HOME/rwx-blast-radius-don"
git -C "$HOME/rwx-blast-radius-don" show d8e4a935eb4261fa6dd32080b0614afbb3fc8b45:maps/index.pkl.gz > "$HOME/don-index.pkl.gz"
shasum -a 256 "$HOME/don-index.pkl.gz"
python3 -m tools.export_file_map --index "$HOME/don-index.pkl.gz" --catalog sample/tests.json --output strategies/selectors/file_map.json.gz
```

The donor Makefile uses `KIT ?= ../blast-radius-sample` and consumes
`$KIT/sample/tests.json`. Its catalog and this repo's `sample/tests.json`
were compared across all `(number, id, file)` tuples:
1,620 entries match exactly (identity SHA-256
`f3bdf0f41f64d10ff0d9edfa93b4c9bd0c296a8e770678d0a40d372c9882fc34`; the
raw catalog SHA-256 is `21257a93c58f4b0646c5122586d2141b4b340ad85b2f0bb251af3746de3f0dc2`).
The exporter unions Python `run` and import-time `deft` masks with non-Python
`files` masks, maps TPC-DS query/setup pseudo IDs to their real catalog test,
and carries baseline failures and missing traces as always-selected IDs.
It writes sorted JSON with deterministic gzip metadata.

At runtime the selector validates schema, source pins, exact catalog
`(number, id, file)` identity (durations may differ), paths, and all test
numbers. A changed catalog or invalid map falls back to all tests. Renames
consider both paths; any unmapped/new path, empty map entry, or unsupported diff
falls back to all tests. A mapped file can still contain changed control flow
not exercised by the pinned coverage run; file coverage is not a guarantee of
failure detection on hidden patches.

Opt in explicitly; `DEFAULT_CONFIG` remains all-tests:

```sh
python3 -c 'from config import Config; from main import main; from strategies.selectors.map_only import MapSelector; main(Config(selector_cls=MapSelector))'
```
