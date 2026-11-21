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
   and `python3 --version`, then run `make e2e`.
3. Start changes on separate branches. Include the sample score and miss
   count in each pull request. Use `make check` for output validation, not
   as a substitute for checking the miss count.

## Commands

| Command | Runs | Output |
| --- | --- | --- |
| `make e2e` (alias `make sample`) | Selector and scheduler in the image, under the scorer's limits, then the sample scorer | `out/e2e.jsonl` |
| `make check` | Same run, format check only | `out/e2e.jsonl` |
| `make select` | Selector only, in the image | `out/picks.jsonl`, one `{"commit", "selected"}` line a patch |
| `make schedule` | Scheduler only, on the host, over `out/picks.jsonl`, then the sample scorer | `out/schedule-saved.jsonl` |
| `make schedule SOURCE=ideal` | Scheduler only, over the sample's known failing tests | `out/schedule-ideal.jsonl` |
| `make test` | Unit tests, on the host | |

Run `make select` once, then `make schedule` after each scheduler change: the
selection stays fixed, so score differences come from scheduling alone.
`SOURCE=ideal` reads `sample/answers.json` on the host, never in the image; it
shows the best score a scheduler could reach with a perfect selector. The same
modes run directly as `python3 main.py [--data DIR] [select | schedule
--picks FILE | schedule --ideal FILE]`.

## Selection and scheduling interface

- `config.py` defines the frozen experiment `Config`: selector and scheduler
  classes, worker count (default 3), and data directory (default `/data`).
  `DEFAULT_CONFIG` uses `AllTestsSelector` and `LptScheduler`.
- `strategies/selectors/` defines `Selector`, `CatalogTest`, and the `Patch`
  record (commit id and diff text). A selector is constructed once with
  `(catalog, config)`. Its `select(commits)` method processes the batch and
  returns a mapping from commit id to unique catalog test numbers.
  `AllTestsSelector` currently selects every test for every commit.
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
