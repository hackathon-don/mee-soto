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
| `make e2e` (alias `make sample`) | Selector and scheduler in the pinned linux/amd64 image, under the scorer's limits, then the sample scorer | `out/e2e.jsonl` |
| `make e2e-amd64` | Alias of `make e2e`, explicitly naming the judge's linux/amd64 architecture | `out/e2e.jsonl` |
| `make e2e-dev` | Same limits and scorer, native architecture with a separate `blast-radius-entry-dev` image and unpinned Python base | `out/e2e-dev.jsonl` |
| `make check` | Same run, format check only | `out/e2e.jsonl` |
| `make select` | Selector only, in the image | `out/picks.jsonl`, one `{"commit", "selected"}` line a patch |
| `make schedule` | Scheduler only, on the host, over `out/picks.jsonl`, then the sample scorer | `out/schedule-saved.jsonl` |
| `make schedule SOURCE=ideal` | Scheduler only, over the sample's known failing tests | `out/schedule-ideal.jsonl` |
| `make test` | Unit tests on the host ([collector test requirements](AGENTS.md#evaluation-and-runtime)) | |

Container stderr is printed to the terminal and saved to `out/e2e.log`,
`out/e2e-dev.log`, `out/check.log`, or `out/select.log`, respectively.

To replay picks in separate checkouts, run `make select` once and copy its
`out/picks.jsonl` to the other checkout's `out/` before running `make schedule`
in each. `SOURCE=ideal` shows what the scheduler would score if given exactly
the known failing tests. The same modes run directly as
`python3 main.py [--data DIR] [select | schedule --picks FILE | schedule --ideal FILE]`.

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
  `{"commit": "...", "workers": [...]}` JSON line.

## Reference results

On the 30 public patches with three workers:

| Selector | Simulated seconds | Misses |
| --- | ---: | ---: |
| All tests (default; `make sample`) | 1,210.470 | 0 |
| [MapSelector](#run-the-map-selector) ([reference run](https://github.com/hackathon-don/rwx-blast-radius/pull/10)) | 851.9 | 0 |

The local sample scorer does not measure container CPU.

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
runtime gets only the patches and test catalog from the host.

### Run the map selector

From the repository root, build the image and run the opt-in selector with
the same mounts and constraints as `make e2e`:

```sh
make build IMAGE=blast-radius-entry
mkdir -p out
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$PWD/sample/commits:/data/commits:ro" \
  --volume "$PWD/sample/tests.json:/data/tests.json:ro" \
  --workdir /app blast-radius-entry \
  python -c 'from config import Config; from main import main; from strategies.selectors.map_only import MapSelector; main(Config(selector_cls=MapSelector))' \
  > out/e2e-map.jsonl &&
python3 score.py sample sample/answers.json out/e2e-map.jsonl
```
