# Committed operator-outcome map (native Linux/amd64)

### Regenerate the patch-independent committed map

Run from the repository root on native Linux/amd64. This one-off record
uses the pinned image, catalog and eight cores. No patch, label, or ordered
patch-batch input is mounted. Check the reported completion and file size
against the track's repository cap before staging the ordinary gzip file.

```bash
set -euo pipefail
test "$(uname -s)/$(uname -m)" = Linux/x86_64
img=blast-radius-outcomes-resume
maps="$PWD/strategies/selectors/impact/maps"
recording="$HOME/outcome-recording"
mkdir -p "$recording"
docker build --platform linux/amd64 -t "$img" .
docker run --rm --network none --cpus 8 --memory 8g --memory-swap 8g \
  --read-only --tmpfs /tmp:rw,exec,size=1g,mode=1777 --env HOME=/tmp \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/sample/tests.json:/data/tests.json:ro" -v "$recording:/out" \
  "$img" python -m strategies.build_operator_outcomes \
    --catalog /data/tests.json --output /out/operator-outcomes.json.gz \
    --committed-map --all-sites --workers-mode isolated --jobs 8 \
    --checkpoint-dir /out/operator-outcomes.partial \
    --budget-seconds 0 --encounter-budget 1000000 --compact-records --cache-rewrites
wc -c "$recording/operator-outcomes.json.gz"
cp "$recording/operator-outcomes.json.gz" "$maps/operator-outcomes.json.gz"
```

Checkpointed recording has no overall cutoff with `--budget-seconds 0`.
The per-test timeout remains 120 seconds. With a positive overall budget,
the builder stops starting new tests at the cutoff and lets in-flight tests
finish or hit their individual timeouts. Repeat the command with the same
checkpoint directory to continue. Use only one writer per directory. The
selector never reads checkpoints; the final gzip is published atomically
only after the full catalog is accounted for.

#### Resume the original 5fa9aa5 Linux run

If raw reports were saved from the original container, set `raw` below to
the directory that directly contains its `test-*.json` files. Use the
**original recording image ID**, not a rebuilt or retagged image, and the
catalog and options actually used to record the files. Raw reports have no
embedded provenance; this step is only appropriate for files copied from
that known run.

```bash
raw="$HOME/outcome-partials/tmpzxr4dtk5"
original_image="$(docker image inspect blast-radius-outcomes --format '{{.Id}}')"
docker run --rm -i --network none --read-only \
  -v "$PWD/sample/tests.json:/data/tests.json:ro" \
  --entrypoint python "$original_image" - > "$raw/provenance.json" <<'PY'
import hashlib
import json
from pathlib import Path

from strategies.selectors.outcome_format import trace_header

files = ("strategies/build_operator_outcomes.py",
         "strategies/selectors/outcome_format.py",
         "strategies/trace_operator_outcomes.py")
catalog = json.loads(Path("/data/tests.json").read_text())["tests"]
settings = dict(workers_mode="isolated", all_sites=True, timeout_seconds=120,
                encounter_budget=1000000, compact_records=True, cache_rewrites=True,
                fast_builtins=False, fast_profile=False)
print(json.dumps(dict(header=trace_header(Path("/opt/sg"), catalog), settings=settings,
                      recorder={path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in files})))
PY
```

For the recording command above, add `-v "$raw:/resume:ro"` to `docker run`
and `--resume-from /resume` to the builder. The builder verifies the original
recorder code hashes, header and settings before using old reports. Missing,
invalid or unverifiable reports are re-recorded; a saved cap marker without a
final report is not enough to reuse that test. The existing checkpoints remain
usable on later runs even without the raw directory.

The Dockerfile records the exact archive revision in the image. The map
header includes that revision, the canonical catalog SHA-256, a digest of
the pinned SQLGlot source and test tree, the format and tracer versions,
and the recorder's Python/platform identity. A missing or mismatched
header disables the map. Regenerate on revision, catalog, test, runtime
identity or tracer-version changes; pre-header maps must also be regenerated.
The output has no patch answers or replay proofs.
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
