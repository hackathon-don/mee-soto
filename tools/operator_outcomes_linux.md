# Operator-outcome pilot (native Linux/amd64)

### Regenerate the patch-independent committed map

Run from the repository root on native Linux/amd64. This one-off record
uses the pinned image, catalog and eight cores. No patch, label, or ordered
patch-batch input is mounted. Check the reported completion and file size
against the track's repository cap before staging the ordinary gzip file.

```bash
set -euo pipefail
test "$(uname -s)/$(uname -m)" = Linux/x86_64
img=blast-radius-outcomes
maps="$PWD/strategies/selectors/impact/maps"
docker build --platform linux/amd64 -t "$img" .
docker run --rm --network none --cpus 8 --memory 8g --memory-swap 8g \
  --read-only --tmpfs /tmp:rw,exec,size=1g,mode=1777 --env HOME=/tmp \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/sample/tests.json:/data/tests.json:ro" -v "$maps:/out" \
  "$img" python -m strategies.build_operator_outcomes \
    --catalog /data/tests.json --output /out/operator-outcomes.json.gz \
    --committed-map --all-sites --workers-mode isolated --jobs 8 \
    --budget-seconds 2400 --encounter-budget 1000000 --compact-records --cache-rewrites
wc -c "$maps/operator-outcomes.json.gz"
```

The Dockerfile records the exact archive revision in the image. The map
header includes that revision, the canonical catalog SHA-256, a digest of
the pinned SQLGlot source and test tree, the format and tracer versions,
and the recorder's Python/platform identity. A missing or mismatched
header disables the map. Regenerate on revision, catalog, test, runtime
identity or tracer-version changes; pre-header maps must also be regenerated.
The output has no patch answers or replay proofs. The original
`impact-first-failure-tables-outcomes` still requires an ordered replay proof
to skip a check.
Each test's isolated recording includes its pytest fixture setup and teardown.
Sites observed during module import or collection are listed separately. A test
that hits the encounter cap in its parent or a spawned recorder is unknown at
all sites, and its test number is listed under `capped`.

### Run the opt-in isolated selector

With the isolated map at the committed path, run:

```bash
make e2e EXPERIMENT=impact-first-failure-tables-free-cpu-outcomes \
  DIAGNOSTIC_FLAGS='--env PYTHONHASHSEED=0 --env BR_OUTCOME_DIAGNOSTICS=1'
```

To try an external map, mount it read-only and set `BR_OUTCOME_MAP` to its
container path. The new experiment narrows candidates only for pure supported
operator changes, and leaves import probes intact. Missing or incomplete test
records, capped tests, sites reached at import or collection, and inconclusive
observations retain normal verification or inclusion. A missing or stale map
uses free-CPU selection unchanged. This opt-in experiment accepts the same
shared-verifier order risk as table-key narrowing; this is not an ordered proof.

### Public two-site pilot

Run from this scratch branch, on the same pinned image for tracing and scoring.
Only patches and the catalog reach the selector; labels stay with the scorer.
The submitted image command is unchanged.

```bash
set -euo pipefail
test "$(uname -s)/$(uname -m)" = Linux/x86_64
img=blast-radius-outcomes
base=impact-first-failure-tables
candidate=impact-first-failure-tables-outcomes
out="$PWD/out/operator-outcomes"
mkdir -p "$out"
docker build --platform linux/amd64 -t "$img" .

docker_trace() {
  docker run --rm --network none --cpus 4 --memory 8g --memory-swap 8g \
    --read-only --tmpfs /tmp:rw,exec,size=1g,mode=1777 --env HOME=/tmp --env BR_OUTCOME_METRICS \
    --user "$(id -u):$(id -g)" \
    -v "$PWD/sample/tests.json:/data/tests.json:ro" -v "$out:/out" \
    "$img" python -m strategies.build_operator_outcomes \
    --catalog /data/tests.json --jobs 4 "$@"
}
seed='--env PYTHONHASHSEED=0'
make e2e-amd64 NO_BUILD=1 IMAGE="$img" EXPERIMENT="$base" CPU_DIAGNOSTICS=1 \
  DIAGNOSTIC_FLAGS="$seed --env BR_CPU_DIAGNOSTICS=1" OUT="$out/baseline-ledger"
mapfile -t numbers < <(python3 - "$out/baseline-ledger/e2e.log" <<'PY'
import json
import sys
from pathlib import Path

ids = set()
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("RWX_CPU_V1 "):
        row = json.loads(line.removeprefix("RWX_CPU_V1 "))
        if row["commit"] in ("2haz", "66ks"):
            ids.update(row["checked"])
for number in sorted(ids):
    print(number)
PY
)
test "${#numbers[@]}" -gt 0
pilot=()
for number in "${numbers[@]}"; do pilot+=(--test-number "$number"); done
docker_trace --output /out/pilot.json.gz \
  --site sqlglot/dialects/dialect.py:1280 --site sqlglot/executor/context.py:53 \
  "${pilot[@]}" | tee "$out/pilot-build.log"

mount="--volume $out/pilot.json.gz:/opt/maps/operator-outcomes.json.gz:ro"
make e2e-amd64 NO_BUILD=1 IMAGE="$img" EXPERIMENT="$candidate" CPU_DIAGNOSTICS=1 \
  DIAGNOSTIC_FLAGS="$seed $mount --env BR_OUTCOME_SHADOW=1 --env BR_CPU_DIAGNOSTICS=1" \
  OUT="$out/shadow"
python3 tools/operator_outcome_report.py "$out/shadow/e2e.log" 2haz 66ks

# Isolated check rows alone cannot certify a shared-cache verifier batch.
# Replay its exact order and import probes on the base tree before active use.
python3 - "$out/shadow/e2e.log" "$out/batches.json" <<'PY'
import json
import sys
from pathlib import Path

prefix = "RWX_OUTCOMES_V1 "
rows = (json.loads(line.removeprefix(prefix))
        for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith(prefix))
batches = [dict(row["batch"], commit=row["commit"])
           for row in rows if row["commit"] in ("2haz", "66ks") and row["batch"]]
assert batches, "no replayable verifier batches"
Path(sys.argv[2]).write_text(json.dumps(batches))
PY
docker_trace --output /out/pilot-verified.json.gz --batch-file /out/batches.json \
  --site sqlglot/dialects/dialect.py:1280 --site sqlglot/executor/context.py:53 \
  "${pilot[@]}" 2>&1 | tee "$out/pilot-verified-build.log"

# Batch replay allows 10× each manifest check limit for instrumentation.
# The original manifest still keys the proof, and the build and process limits
# still apply. The log keeps batch stderr and distinguishes these timeouts.
# Only the four debug-timing reads in the pinned executor.execute are exempt.
# The pinned UNIXTOTIME conversion passes an explicit UTC timezone; local
# timestamp conversions and other clock reads are still rejected.

# Separate, bounded full-site trace. Review coverage as well as elapsed time.
docker_trace --output /out/full.json.gz --all-sites --batch-file /out/batches.json \
  --compact-records --cache-rewrites --budget-seconds 540 \
  2>&1 | tee "$out/full-build.log"
```

### Fixed Linux timing subset

Run the following separately from the full trace. All six runs use the same
48 catalog tests spread across the recorded duration range, including the
shortest and longest. The profile modes cannot write a map. Compare plain
test/interpreter cost, rewrite and hook cost with the observer passing through,
and total recording cost with and without the fast paths.

```bash
profile_tests=()
while read -r number; do profile_tests+=(--test-number "$number"); done < <(
  python3 - <<'PY'
import json
from pathlib import Path

tests = sorted(json.loads(Path("sample/tests.json").read_text())["tests"],
               key=lambda test: (test["seconds"], test["number"]))
for i in range(48):
    print(tests[i * (len(tests) - 1) // 47]["number"])
PY
)
test "${#profile_tests[@]}" -eq 96
docker_trace --all-sites "${profile_tests[@]}" --profile-mode plain | tee "$out/profile-plain.log"
docker_trace --all-sites "${profile_tests[@]}" --profile-mode rewrite | tee "$out/profile-rewrite.log"
docker_trace --all-sites "${profile_tests[@]}" --output /out/profile-default.json.gz \
  | tee "$out/profile-default.log"
docker_trace --all-sites "${profile_tests[@]}" --compact-records --cache-rewrites \
  --output /out/profile-fast.json.gz | tee "$out/profile-fast.log"
docker_trace --all-sites "${profile_tests[@]}" --workers-mode persistent --profile-mode plain \
  | tee "$out/profile-plain-persistent.log"
docker_trace --all-sites "${profile_tests[@]}" --workers-mode persistent --compact-records --cache-rewrites \
  --output /out/profile-persistent.json.gz 2>&1 | tee "$out/profile-persistent.log"
python3 -m tools.compare_operator_outcomes "$out/profile-fast.json.gz" "$out/profile-persistent.json.gz"
```

To find a slow test, set `BR_OUTCOME_METRICS=timing` for a persistent run
and capture stderr. Use `BR_OUTCOME_METRICS=encounters` to also count
comparison hook calls per site in the test process (not its spawned workers).
The start markers identify the in-flight test if a shard times out.
Use identical metrics settings for before/after timings.

For an opt-in comparison with `--compact-records`, repeat the persistent
trace with `--fast-builtins`. The fast path computes identity, equality,
and applicable membership from the live operands before reusing an outcome
row. Enum classes, subclasses, bytes and other operands take the ordinary
path; no result is inferred from types alone.

`--fast-profile` prefilters exact builtin call objects that the safety profiler
does not reject. The other profile events, clock checks and audit hooks still run.

`--encounter-budget 1000000` is a separate persistent-recording experiment.
It counts selected-site comparisons in each test's parent process; on the
first excess encounter it logs `RWX_OUTCOME_BUDGET <test> <limit>` and omits
all local trace evidence for that test. The test still runs, the safety hooks
stay installed, and spawn, collection and ordered batch replay are uncapped.
Do not treat a budget-limited map as full coverage, even if it lists every site.

Compare attempts and completions as well as elapsed time; a quicker run
with more rejected traces is not a coverage win. The builder reports elapsed
wall time and, on cgroup v2, average CPU cores used including subprocesses.
The job pool launches at most four fresh interpreter processes, one per test
or ordered batch. Tests can also spawn their own workers. The private bytecode
cache is reused by subprocesses and verified against source bytes, but test
state is not shared in isolated mode. Compact records deduplicate outcome vectors, not merely
operand types. Every encounter is still checked.

With `--workers-mode persistent`, each of the four default jobs instead runs
one deterministic pytest shard. The shard uses the remaining build budget,
not the 120 s per-process timeout; standalone ordered batch replay retains
that timeout. On a crash the attempted prefix is unknown; the unstarted
suffix can restart in a clean process. Collection and spawned-worker
observations are pooled globally and can veto a test's agreement. No local
observation means unknown, including comparisons hidden by a prior test's
cache. Persistent maps are shadow-only, and any replay results are stored
separately from active batch proofs. Active omission still needs isolated
evidence and the exact ordered batch replay.

The comparator lists persistent agreements not backed by isolated evidence,
even though the active selector cannot omit from the persistent map. Require
zero findings before considering shared-process evidence for a future proof
gate. Review both maps' completion and observation counts too: zero findings
from an empty persistent map is not a validation.

After reviewing shadow coverage and batch completeness, choose one map.
Active mode needs a complete replay of the exact ordered batch and import
probes, plus complete isolated traces (with workers) for the batch's checks,
with agreement at every observed encounter. This includes a comparison
whose result is cached by one check and reused by another. Mixed, unrecorded,
and incomplete batches stay intact. Pending import probes still run.

```bash
map="$out/pilot-verified.json.gz"  # or "$out/full.json.gz"
mount="--volume $map:/opt/maps/operator-outcomes.json.gz:ro"
for i in 1 2 3; do
  make e2e-amd64 NO_BUILD=1 IMAGE="$img" EXPERIMENT="$base" \
    DIAGNOSTIC_FLAGS="$seed" OUT="$out/base-$i"
  make e2e-amd64 NO_BUILD=1 IMAGE="$img" EXPERIMENT="$candidate" \
    DIAGNOSTIC_FLAGS="$seed $mount --env BR_OUTCOME_SHADOW=0" OUT="$out/candidate-$i"
done
```

Use a fresh labeled holdout directory (not one with cached evaluator outputs).
The following runs the main-branch evaluator against both experiments with the
same sandbox and input mounts; its labels stay on the host. If the target is
unlabeled, generate and label it first using main's `make holdout` workflow.

```bash
export DEV_IMAGE="$img"
export OUTCOME_MAP="$map"
holdout=/absolute/path/to/fresh-labeled-holdout
tool=/absolute/path/to/main/tools/holdout_eval.py
python3 - "$tool" "$holdout" "$base" "$candidate" <<'PY'
import os
import runpy
import sys

path, holdout, base, candidate = sys.argv[1:]
module = runpy.run_path(path)
main = module["main"]
main.__globals__["RUN_FLAGS"].extend([
    "--env", "PYTHONHASHSEED=0", "--env", "BR_OUTCOME_SHADOW=0",
    "--volume", f'{os.environ["OUTCOME_MAP"]}:/opt/maps/operator-outcomes.json.gz:ro',
])
sys.argv = [path, holdout, "30", base, candidate]
raise SystemExit(main())
PY
```

Do not treat an agreeing shadow row as held-out or active-mode evidence.
The recorder rejects incomplete tests and workers, unsupported operands,
application clock/thread observations, and unsupported comprehension-chain
rewrites. It does not prove that native extensions cannot introduce
nondeterminism. A timed-out trace emits a partial map, never an all-test claim.
