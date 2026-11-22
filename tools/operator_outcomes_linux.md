# Operator-outcome pilot (native Linux/amd64)

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
    --read-only --tmpfs /tmp:rw,exec,size=1g,mode=1777 --env HOME=/tmp \
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

# Separate, bounded full-site trace. Review coverage as well as elapsed time.
docker_trace --output /out/full.json.gz --all-sites --batch-file /out/batches.json \
  --compact-records --cache-rewrites --budget-seconds 540 \
  2>&1 | tee "$out/full-build.log"
```

### Fixed Linux timing subset

Run the following separately from the full trace. All four runs use the same
48 catalog tests spread across the recorded duration range, including the
shortest and longest. The first two modes cannot write a map. Compare plain
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
```

Compare attempts and completions as well as elapsed time; a quicker run
with more rejected traces is not a coverage win. The builder reports elapsed
wall time and, on cgroup v2, average CPU cores used including subprocesses.
The job pool launches at most four fresh interpreter processes, one per test
or ordered batch. Tests can also spawn their own workers. The private bytecode
cache is reused by subprocesses and verified against source bytes, but test
state is not shared. Compact records deduplicate outcome vectors, not merely
operand types. Every encounter is still checked.

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
