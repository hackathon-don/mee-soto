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

- `selector.select(diff, catalog)` takes unified diff text and the entries
  from `tests.json`. It returns a list of unique test numbers from the catalog,
  currently all of them. Selection changes belong here.
- `scheduler.schedule(selected, durations)` takes the selected test numbers
  and a mapping from test number to recorded seconds. It returns worker lists
  containing each selected number exactly once, or `[]` for no tests.
  Scheduling changes belong here.
- The container entrypoint selects and schedules tests for each patch and
  writes one `{"commit": "...", "workers": [...]}` JSON line. Keep selection
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
