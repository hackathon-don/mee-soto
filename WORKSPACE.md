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
   `linux/amd64` explicitly for judge-compatible runs, even on an ARM host.
   Use `make e2e-dev` for a faster native-architecture local loop instead.
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
| `make e2e-amd64` | Alias of `make e2e`, explicitly naming the judge's linux/amd64 architecture | `out/e2e.jsonl` |
| `make e2e-dev` | Same limits and scorer, native architecture with a separate `blast-radius-entry-dev` image and unpinned Python base | `out/e2e-dev.jsonl` |
| `make check` | Same run, format check only | `out/e2e.jsonl` |
| `make select` | Selector only, in the image | `out/picks.jsonl`, one `{"commit", "selected"}` line a patch |
| `make schedule` | Scheduler only, on the host, over `out/picks.jsonl`, then the sample scorer | `out/schedule-saved.jsonl` |
| `make schedule SOURCE=ideal` | Scheduler only, over the sample's known failing tests | `out/schedule-ideal.jsonl` |
| `make test` | Unit tests, on the host | |

Container stderr is printed to the terminal and saved to `out/e2e.log`,
`out/e2e-dev.log`, `out/check.log`, or `out/select.log`, respectively.

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

`MapSelector` is opt-in; `Config()` still selects all tests. The Docker
builder runs `tools/build_file_map.py` against SQLGlot
`tobymao/sqlglot@eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and
`sample/tests.json`. It starts one Python 3.13 child per catalog test, enables
`sys.monitoring.PY_START` before importing pytest, and audits file opens and
directory enumeration. The map records repository-relative source/fixture
files and directory dependencies; runtime adds dependents of only the
immediate parent directory of each changed file.

The deterministic gzip JSON uses schema 2 and embeds the exact
`(number, id, file)` catalog identity and its digest. It is generated in the
builder stage and is not committed. The builder copies the pinned
`requirements.lock`, collector, schema helper, and `sample/tests.json`; it
does not receive sample patches or answers. The final stage copies only the
generated map and runtime application files, not pytest, test source, or
collector tooling. Runtime validates schema, source pin, catalog identity,
status partition, paths, and test numbers; malformed or missing maps fall back
to all tests.

Baseline failures, missing tests, and incomplete traces are always selected.
Incomplete includes subprocess/fork use (descendants are not traced),
timeouts, invalid results, and tests whose trace omitted their catalog file.
Tests not started before the collector budget expires are recorded as missing,
not silently discarded. At runtime, changed files must have exact file entries;
an unmapped/new path, empty entry, or unsupported diff falls back to all tests.
For directory reads, only tests that enumerated the changed file's immediate
parent directory are added; a file-content edit does not affect ancestor
directory listings. As with any pinned test-to-file map, changed control flow
or hidden tests may not be represented by this catalog's measurements.

The collector uses four workers, a 360-second test-collection budget, and a
90-second per-test timeout by default. The collection budget does not include
base-image acquisition, SQLGlot source download, dependency installation, or
final image assembly; those steps add to a clean Docker build's total time.
Build logs report the full catalog partition (passed, baseline failures,
incomplete, and missing); missing/incomplete tests are always selected. The
final image has no tests or answers mounted at runtime.

Opt in explicitly; `DEFAULT_CONFIG` remains all-tests:

```sh
python3 -c 'from config import Config; from main import main; from strategies.selectors.map_only import MapSelector; main(Config(selector_cls=MapSelector))'
```
