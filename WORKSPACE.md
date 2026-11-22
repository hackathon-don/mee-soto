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
| `make build` | Builds the Docker image, including the impact name index | `blast-radius-entry` image (override with `IMAGE`) |
| `make e2e` | Selector and scheduler in the pinned linux/amd64 image, under the scorer's limits, then the sample scorer | `out/e2e.jsonl` |
| `make e2e-amd64` | Alias of `make e2e`, explicitly naming the judge's linux/amd64 architecture | `out/e2e.jsonl` |
| `make e2e-dev` | Same limits and scorer, native architecture with a separate `blast-radius-entry-dev` image and unpinned Python base | `out/e2e-dev.jsonl` |
| `make check` | Same run, format check only | `out/e2e.jsonl` |
| `make select` | Selector only, in the image | `out/picks.jsonl`, one `{"commit", "selected"}` line a patch |
| `make schedule` | Scheduler only, on the host, over `out/picks.jsonl`, then the sample scorer | `out/schedule-saved.jsonl` |
| `make schedule SOURCE=ideal` | Scheduler only, over the sample's known failing tests | `out/schedule-ideal.jsonl` |
| `make test` | Unit tests on the host | |
| `make holdout` | Generates the [held-out patch set](#held-out-patch-set) once and labels it by running the full catalog in the dev image; resumes | `out/holdout/commits/`, `out/holdout/labels.jsonl` |
| `make e2e-holdout` | `EXPERIMENT` on the labeled held-out set, in sample-sized batches, native dev image; `DEFERRAL=off` disables heavy-test deferral | `out/holdout/eval/<experiment>/` |
| `make e2e-full` | The sample plus the held-out set on one dev image, one report | `out/holdout/eval/<experiment>/` |

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
  `Dockerfile` `CMD` passes `--experiment impact-variable-workers-matched`; the
  Makefile targets override `CMD` and pass `EXPERIMENT` instead.
- `strategies/selectors/` defines `Selector`, `CatalogTest`, and the `Patch`
  record (commit id and diff text). A selector is constructed once with
  `(catalog, config)`. Its `select(commits)` method processes the batch and
  returns a mapping from commit id to unique catalog test numbers.
  `AllTestsSelector` currently selects every test for every commit.
- `ImpactSelector` is opt-in via the `impact` experiment. It maps an AST diff
  of each patch onto committed line-coverage maps, then runs cheap candidates
  against the patched tree and selects the ones that fail. See
  [Impact selector](#impact-selector).
- `TableKeySelector` (the `impact-tables` and
  `impact-tables-variable-workers` experiments) is an `ImpactSelector` that
  drops candidates which never read the table entry a patch changed. See
  [Table-entry narrowing](#table-entry-narrowing).
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

`make e2e` and `make e2e-dev` measure the entry process's user and system CPU,
including reaped descendants, from launch to exit. Build time, host scoring,
and wall-clock waits are excluded. This local measurement is added only to
the estimated total; compare strategies using the exact simulated total.
The leaderboard measures CPU on its own amd64 host, so local estimates are
only a rough guide. Unreaped background processes are not captured by the
local process accounting. `ImpactSelector` has
no judge-compatible reference run in this repository yet.

Selectors on the 30 public patches. The first column is a native arm64
`make e2e-dev` run, one image for all rows (not judge-compatible); the second
replays those same picks on the host through `VariableWorkerScheduler`:

| Selector (experiment) | `LptScheduler`, 3 workers | `VariableWorkerScheduler` | Misses |
| --- | ---: | ---: | ---: |
| `ImpactSelector` (`impact`) | 120.919 | 105.896 | 0 |
| `OptimizerTranscriptImpact` (`impact-full-optimizer-proof`, `impact-variable-workers-matched`) | 116.574 | 105.480 | 0 |
| `TableKeySelector` (`impact-tables`, `impact-tables-variable-workers`) | 111.533 | 96.817 | 0 |

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

For an opt-in selector decision log, run
`make e2e EXPERIMENT=<name> DECISION_LOG=1`. Independently of CPU diagnostics,
stderr contains `RWX_DECISION_V1 {json}` lines for each pre-narrowing candidate
and a per-patch summary. Records describe the final kept/dropped verdict, rule,
and recorded verification CPU (or null). It is off in default and scored runs.

## Benchmark release

**Benchmark release** runs on every push to main and benchmarks every
registered experiment three times at the pushed commit, without CPU
diagnostics so logging overhead stays out of the ranked totals. One job builds
the image, so every experiment uses the same image. Each experiment
runs in its own job on its own machine, and a report job combines their trials;
an experiment whose job fails counts as failed runs. The workflow saves the human report and structured results as artifacts, even when nothing
is released. The
baseline is a reference; eligible candidates finish all three runs without a
miss and are ranked by exact median simulated-plus-CPU time. An incomplete batch
or no eligible candidate cannot release. A batch is incomplete when an experiment
job failed in the latest attempt or the experiments do not all report the
same Docker image ID.

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

Run it with `make e2e EXPERIMENT=impact`.

## Table-entry narrowing

SQLGlot keeps most dialect rules in dict literals assigned in class bodies.
`ImpactSelector` treats an edit to one entry as an edit to the whole
statement, so every test that touches the class and the table's name is a
candidate. `TableKeySelector` in `strategies/selectors/impact_tables.py`
subclasses it and narrows one kind of edit: a patch that changes the value of
an existing entry of a dict literal assigned directly in a module or class
body. For that edit it keeps only the tests that read that entry's value.

Everything else gets `ImpactSelector`'s answer unchanged: entries added,
removed or renamed, a `**` spread, a value that calls something, a table the
trace did not wrap, and a missing map or one built for another catalog.

The committed map, `strategies/selectors/impact_tables.json.gz`, comes from a
build-time trace. `tools/trace_tables/sitecustomize.py` rewrites each SQLGlot
module as it is imported so those dict literals record value reads. Membership
tests, `len()` and key iteration read no value and are not recorded; anything
that reads every value at once (`items()`, `copy()`, comparison, a `**`
spread into another table) counts as reading the whole table. A test that
fails under the tracer or leaves no dump is always kept. The tracer cannot see
a read made by C code that bypasses the dict's Python methods.

Rebuild the map after changing the tracer or the catalog:

```sh
OUT=$(mktemp -d) && cp sample/tests.json "$OUT/"
docker run --rm --network none --memory 6g \
  -v "$PWD/tools/trace_tables:/trace:ro" \
  -v "$PWD/tools/table_trace_driver.py:/tools/table_trace_driver.py:ro" \
  -v "$OUT:/out" blast-radius-entry-dev \
  sh -c 'cp -a /opt/sg /src && python /tools/table_trace_driver.py /out/tests.json /out 6'
python3 tools/build_table_index.py "$OUT" sample/tests.json strategies/selectors/impact_tables.json.gz
```

The committed map traced all 1,620 catalog tests with none failing. On the
sample it changes one commit, `4d63`, whose candidates go from 275 to 0.
`tools/table_mutants.py` and `tools/check_table_narrowing.py` check the
narrowing on held-out edits: over 80 single-entry mutants across 59 tables,
narrowing dropped 14,166 of 22,020 candidates, the base candidates produced
1,406 failures, and none of the dropped tests failed. The checker flagged
`test_max_nodes` on three mutants when several copies ran at once; it passes
under pytest on each of those patched trees. These results do not guarantee
zero misses on hidden patches.

## Held-out patch set

The 30 public patches are tuning data. To check a selector on patches nothing
was tuned on, generate a held-out set from the pinned commit, label it by
running the full catalog, and score experiments on it. `make e2e` and the
other existing targets, `make test`, CI and the image never touch it: the
tools sit in `tools/` and the data in `out/holdout/`, both outside the Docker
build context and Git.

```sh
make holdout                                   # once: generate 150 patches and label them (hours; resumes)
make e2e-holdout EXPERIMENT=impact-tables      # score one experiment on the held-out set
make e2e-holdout EXPERIMENT=impact-tables DEFERRAL=off
make e2e-full EXPERIMENT=impact-tables         # the sample plus the held-out set, one report
```

`HOLDOUT_COUNT` (150), `HOLDOUT_BATCH` (30 patches per run, like the sample)
and `HOLDOUT_JOBS` (6 parallel pytest processes while labeling) override the
defaults; `NO_BUILD=1` reuses the dev image. All three targets use the native
dev image, so like `make e2e-dev` they are not judge-compatible measurements.

`tools/holdout_mutants.py` mirrors the sample's mix and edit shapes (code 16,
fixture 8, multi 3, comment 3 of 30) on uniformly drawn lines.
`tools/holdout_label/label.py` runs every catalog test file under pytest on
each patched tree; a test is failing when any of its reports fails, subtests
included, and it passes unpatched. On 8 sample commits (`2haz`, `2jrq`,
`2vdj`, `3utz`, `535z`, `986c`, `9u5k`, `ary6`) its labels matched
`sample/answers.json` exactly. `tools/holdout_eval.py` runs each batch under
the scorer's flags with only that batch's patches mounted, then lists every
missed test. `DEFERRAL=off` sets `BR_MIN_EVIDENCE` so high that
`ImpactSelector` never defers its two heavy tests.
