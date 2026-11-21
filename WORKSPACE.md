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
  `ImpactSelector` uses coverage-mapped impact analysis and patched-tree
  verification and is opt-in via `Config(selector_cls=ImpactSelector)` (import
  it from `strategies.selectors.impact`). `AllTestsSelector` is the default.
- `strategies/schedulers/` defines `Scheduler`, constructed once with
  `(durations, config)`. Its `schedule(selected)` method returns worker lists
  containing each selected number exactly once, or `[]` for no tests, dropping
  empty workers. `LptScheduler` uses at most `config.workers` workers.
- The container entrypoint, `main.py`, selects and schedules tests for each
  patch, caching identical consecutive selections, and writes one
  `{"commit": "...", "workers": [...]}` JSON line. Keep selection
  fixed when measuring scheduling changes and vice versa.

## Reference result

The default `Config()` preserves the official starter's
behavior: run all 1,620 tests on three workers.
The recorded all-tests reference on the 30 public patches is **1,210.470
simulated seconds, zero misses**. The local sample scorer does not measure container
CPU; do not compare this score directly with the published leaderboard
baseline, which is not the local sample score.

## Impact selector and image

`strategies/selectors/impact/` ports Don's PR #5 at
`d8e4a935eb4261fa6dd32080b0614afbb3fc8b45`. The committed maps under its
`maps/` directory were built from the pinned SQLGlot revision. The image
installs `requirements.lock`, fetches that revision into `/opt/sg`, builds
`maps/static.pkl.gz`, and precompiles Python modules. Offline tools live in
`tools/`; tools, tests, sample data, and experiment output are excluded from
the Docker build context.

`ImpactSelector` starts its wall budget at construction, before loading its
index. It falls back to every catalog test if index loading or patch analysis
raises an error. The original environment-variable knobs (`BR_SG`, `BR_MAPS`,
`BR_WORK`, `BR_WALL_BUDGET`, and the other `BR_*` tuning settings) remain in
the impact implementation, not `Config`. `BR_MAPS` contains `index.pkl.gz`,
`cpu.json`, and the build-generated `static.pkl.gz`.

The selector's internal `pack.py` estimates verification costs; it does not
replace the final `LptScheduler`, which uses `config.workers` (three by
default). Patched-tree verification also probes changed Python imports in its
existing child process, with a 2-second bound per import. An exception, timeout,
or missing child result conservatively retains that module's catalog-filtered
importers; only an explicit successful probe clears this fallback. Pure Git
renames are recognized from `rename from`/`rename to` metadata. Python renames
probe the old import path and also consider old-path importers as candidates
even when the probe succeeds. Public sample results do not guarantee zero
misses on hidden patches.

Treat `sample/answers.json` as scorer-only data. The sample runner mounts
only the patches and the test list, and the Docker build excludes the
sample directory. Keep the [scorer restrictions](SCORING.md) in place:
no network at runtime, a read-only filesystem, and the unprivileged
`nobody` user.

The official starter files are from the
[track download](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz).
