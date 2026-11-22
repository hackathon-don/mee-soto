# RWX Blast Radius workspace

The [RWX track](https://www.joinbuilders.org/buildathon/tracks/blast-radius)
asks you to pick the SQLGlot tests that might fail for each patch, then split
them across at most eight workers. The score is the slowest worker's recorded
test time, plus scheduling overhead, a penalty for each patch with a missed
failure, and the selector's own CPU time. Lower is better.

## Get both teammates running the same setup

1. Use the repository's Devin environment, or set up a local machine that
   meets the [build requirements](AGENTS.md#hard-rules). Docker must be
   running and accessible to your user; the host also needs GNU Make and
   Python 3.9 or newer.
2. Clone the repository into a path without spaces. In the repo root, run
   `docker version` (check for both Client and Server), `make --version`,
   and `python3 --version`, then run `make e2e`.
3. Start changes on separate branches. See the [PR evidence rules](AGENTS.md#evidence-for-prs)
   before running a comparison.

## Commands

| Command | Runs | Output |
| --- | --- | --- |
| `make build` | Builds the Docker image, including the selector map | `blast-radius-entry` image (override with `IMAGE`) |
| `make e2e` | Selector and scheduler in the pinned linux/amd64 image, under the scorer's limits, then the sample scorer | `out/e2e.jsonl` |
| `make e2e-amd64` | Alias of `make e2e`, explicitly naming the judge's linux/amd64 architecture | `out/e2e.jsonl` |
| `make e2e-dev` | Same limits and scorer, native architecture with a separate `blast-radius-entry-dev` image and unpinned Python base | `out/e2e-dev.jsonl` |
| `make check` | Same run, format check only | `out/e2e.jsonl` |
| `make select` | Selector only, in the image | `out/picks.jsonl`, one `{"commit", "selected"}` line a patch |
| `make schedule` | Scheduler only, on the host, over `out/picks.jsonl`, then the sample scorer | `out/schedule-saved.jsonl` |
| `make schedule SOURCE=ideal` | Scheduler only, over the sample's known failing tests | `out/schedule-ideal.jsonl` |
| `make test` | Unit tests on the host ([collector test requirements](AGENTS.md#evaluation-and-runtime)) | |

Container stderr is printed to the terminal and saved to `out/e2e.log`,
`out/e2e-dev.log`, `out/check.log`, or `out/select.log`, respectively.
Run targets use `EXPERIMENT=baseline` by default; override it with a name
from `config.py`'s `EXPERIMENTS`.

To replay picks in separate checkouts, run `make select` once and copy its
`out/picks.jsonl` to the other checkout's `out/` before running `make schedule`
in each. `SOURCE=ideal` shows what the scheduler would score if given exactly
the known failing tests. The same modes run directly as
`python3 main.py [--experiment NAME] [--data DIR] [select | schedule --picks FILE | schedule --ideal FILE]`.

## Selection and scheduling interface

- `config.py` defines the frozen experiment `Config`: selector and scheduler
  classes, worker count (default 3), and data directory (default `/data`).
  `EXPERIMENTS` holds named configs; `DEFAULT_CONFIG` is the baseline entry,
  using `AllTestsSelector` and `LptScheduler`. No flag preserves the supplied
  config (or `DEFAULT_CONFIG`); `--experiment` replaces it, then `--data`
  overrides its data directory. The judge runs the image without flags, so the
  `Dockerfile` `CMD` passes `--experiment impact-full-optimizer-proof`; the
  Makefile targets override `CMD` and pass `EXPERIMENT` instead.
- `strategies/selectors/` defines `Selector`, `CatalogTest`, and the `Patch`
  record (commit id and diff text). A selector is constructed once with
  `(catalog, config)`. Its `select(commits)` method processes the batch and
  returns a mapping from commit id to unique catalog test numbers.
  `AllTestsSelector` currently selects every test for every commit.
- `MapSelector` is opt-in via the `map` experiment. It reads a
  precomputed file-to-test map only; it does not import the patched source,
  collect tests, launch subprocesses, or probe imports. Unknown, empty,
  malformed, quoted, binary, or unsupported changes select all tests.
- `ImpactSelector` is opt-in via `Config(selector_cls=ImpactSelector)` (import
  it from `strategies.selectors.impact`). It maps an AST diff of each patch
  onto committed line-coverage maps, then runs cheap candidates against the
  patched tree and selects the ones that fail. See
  [Impact selector](#impact-selector).
- `strategies/schedulers/` defines `Scheduler`, constructed once with
  `(durations, config)`. Its `schedule(selected)` method returns worker lists
  containing each selected number exactly once, or `[]` for no tests, dropping
  empty workers. `LptScheduler` uses at most `config.workers` workers.
- The container entrypoint, `main.py`, selects and schedules tests for each
  patch, caching identical consecutive selections, and writes one
  `{"commit": "...", "workers": [...]}` JSON line.

## Try a new strategy

Keep each idea separate so it can be compared against the unchanged baseline.

1. Add the class in its own file, such as
   `strategies/schedulers/my_scheduler.py`, following the
   [interface](#selection-and-scheduling-interface).
2. Import it in `config.py` and add an `EXPERIMENTS` entry holding a `Config`,
   e.g. `"my": Config(scheduler_cls=MyScheduler)`. Keep the other stage and
   worker count fixed. Don't edit existing strategies or the baseline for a trial.
3. Benchmark as below. Promote the winner to the baseline/default only in
   the PR that makes it the submission, with the comparison as evidence.

For selectors, compare `make e2e` with `make e2e EXPERIMENT=<name>`.
These targets rebuild the image after code changes and use the same run limits.

To compare schedulers, select once and replay the same picks on the host,
which needs no image rebuild:

```sh
make select                # once; writes out/picks.jsonl
make schedule              # baseline on those picks
make schedule EXPERIMENT=my # new scheduler on the same picks; no rebuild
```

Use `SOURCE=ideal` on both schedule runs to compare on the known failing tests.
Save output files before the next run if you need to retain both schedules.

## Reference results

On the 30 public patches with three workers:

| Selector | Simulated seconds | Misses |
| --- | ---: | ---: |
| All tests (default; `make e2e`) | 1,210.470 | 0 |
| [MapSelector](#run-the-map-selector) ([reference run](https://github.com/hackathon-don/rwx-blast-radius/pull/10)) | 851.9 | 0 |

`make e2e` and `make e2e-dev` measure the entry process's user and system CPU,
including reaped descendants, from launch to exit. Build time, host scoring,
and wall-clock waits are excluded. This local measurement is added only to
the estimated total; compare strategies using the exact simulated total.
The leaderboard measures CPU on its own amd64 host, so local estimates are
only a rough guide. Unreaped background processes are not captured by the
local process accounting. `ImpactSelector` has
no judge-compatible reference run in this repository yet.

The official starter files are from the
[track download](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz).

## PR selection errors

The score comment reports selection errors for each experiment's median-total run.
It uses the emitted worker schedule and public labels on the scoring host only; labels
do not reach the selector container.

## PR selector CPU

The separate CPU comment uses the median-total run. It joins completed verifier
timings to the final worker schedule; shared and incomplete work is not assigned
to individual tests. A fixture-subset check is not a full test.

For a local diagnostic run on native Linux amd64, use
`make e2e EXPERIMENT=impact CPU_DIAGNOSTICS=1`. Detailed records go only
to stderr and the timed CI run includes logging overhead. The default and judge
entrypoints do not enable this instrumentation.

## Benchmark release

**Benchmark release** runs on every push to main and benchmarks every
registered experiment three times at the pushed commit, without CPU
diagnostics so logging overhead stays out of the ranked totals. One job builds
the image, so every experiment uses the same file map. Each experiment
runs in its own job on its own machine, and a report job combines their trials;
an experiment whose job fails counts as failed runs. The workflow saves the human report and structured results as artifacts, even when nothing
is released. The
baseline is a reference; eligible candidates finish all three runs without a
miss and are ranked by exact median simulated-plus-CPU time. An incomplete batch
or no eligible candidate cannot release. A batch is incomplete when an experiment
job failed in the latest attempt or the experiments were measured with different
file maps.

When the batch has an eligible winner, a separate job validates the saved
result and attaches both reports to a release tagged at the measured commit.
The release notes are the full report. A pull request that changes the workflow
or its report tools runs the same benchmark and release validation as a dry run
that stops before creating a tag or release. It does not change the default experiment or submit anything to the
leaderboard.

## Impact selector

`strategies/selectors/impact/` holds `ImpactSelector`. The maps committed
under its `maps/` directory were built offline from the pinned SQLGlot
revision: per-line test coverage, class instantiations, fixture reads, and
measured per-test CPU. The image installs `requirements.lock`, fetches that
revision into `/opt/sg`, builds `maps/static.pkl.gz` (a name index), and
precompiles Python modules. The offline map and tuning tools live in `tools/`
and stay out of the Docker build context.

For each patch the selector diffs the old and new ASTs, so comment-only edits
drop out, and maps each difference to the tests that can reach it. A cost
model then splits those candidates into tests to include unrun and tests to
run. Chosen tests run in forked children against a symlink overlay of the
patched tree under `/tmp`, and only failures are selected. Running tests uses
container CPU, which counts toward the board score and the local CPU estimate.

`ImpactSelector` starts its wall budget at construction, before loading its
index. It falls back to every catalog test if index loading or patch analysis
raises an error. Its tuning knobs are environment variables (`BR_SG`,
`BR_MAPS`, `BR_WORK`, `BR_WALL_BUDGET`, and the other `BR_*` settings) read in
`selector.py`, not `Config` fields.

The selector's internal `pack.py` estimates costs when deciding what to run;
it does not replace the final `LptScheduler`, which uses `config.workers`.
Verification also probes changed Python imports in its child process, with a
2-second bound per import. An exception, timeout, or missing child result
keeps that module's importers selected; only a successful probe clears them.
Pure Git renames are recognized from `rename from`/`rename to` metadata, and
old-path importers stay candidates even when the probe succeeds. Public sample
results do not guarantee zero misses on hidden patches.

To run it, use the [MapSelector recipe](#run-the-map-selector) with
`from strategies.selectors.impact import ImpactSelector` and
`Config(selector_cls=ImpactSelector)` in the `python -c` argument.

## Map-only selector

The Docker builder runs `tools/build_file_map.py` against SQLGlot
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
does not receive sample patches or answers. The final stage copies the
generated map and runtime application files, not the collector tooling; it
keeps the pinned dependencies and SQLGlot source for `ImpactSelector`, which
`MapSelector` does not read. Runtime validates schema, source pin, catalog identity,
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
runtime gets only the patches and test catalog from the host.

### Run the map selector

From the repository root, build and score with the standard mounts and limits:

```sh
make e2e EXPERIMENT=map
```
