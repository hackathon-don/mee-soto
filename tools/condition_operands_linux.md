# Condition operand map (Linux)

`impact-first-failure-tables-free-cpu-conditions` drops a candidate when the
patch only flips conditions (`and` <-> `or`, or adds or removes `not` on a
condition operand) and the committed map shows the candidate never evaluates
any flipped condition. A flip changes the result on every evaluation, so a test
that never reaches the site behaves the same; every other candidate is checked
as before.

## What is recorded

`strategies/trace_condition_operands.py` wraps every condition operand in
SQLGlot (`if`/`while`/conditional-expression/`assert` tests, comprehension
filters, each `and`/`or` operand, and the operand of `not`) in a hook that
records `bool(value)` and returns the value unchanged, so short-circuit order
and effects are kept. It reuses the isolated operator outcome recorder: one
test per process, the same import guard, nondeterminism profiler (active while
the test runs), thread and process audit, and encounter budget. A test is
complete only when setup, call and teardown pass, no worker process starts and
the recorder saw nothing unsupported. Conditions evaluated at import or
collection time are marked and never back a drop. Class and module setup runs
inside each test's own process, so it counts against every test under it.

The scored verifier runs with hash randomization, so every test is recorded
under hash seeds 0 and 1 and a site counts as reached if either seed reached
it. The builder prints how many test/site pairs differ between seeds.

No patch, label, or edit kind is used to choose sites or tests: every
condition site and every catalog test is recorded.

## Regenerate

Run on Linux/amd64 from the repository root (about 30 minutes on 8 cores):

```bash
set -euo pipefail
test "$(uname -s)/$(uname -m)" = Linux/x86_64
img=blast-radius-conditions
maps="$PWD/strategies/selectors/impact/maps"
docker build --platform linux/amd64 -t "$img" .
docker run --rm --network none --cpus 8 --memory 12g --memory-swap 12g \
  --read-only --tmpfs /tmp:rw,exec,size=2g,mode=1777 --env HOME=/tmp \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/sample/tests.json:/data/tests.json:ro" -v "$maps:/out" \
  "$img" python -m strategies.build_condition_operands \
    --catalog /data/tests.json --output /out/condition-operands.json.gz --jobs 8
wc -c "$maps/condition-operands.json.gz"
```

## Staleness

The map header holds the interpreter identity, the SQLGlot revision, the
catalog SHA-256 and a digest of the `sqlglot/` and `tests/` trees, plus the
condition tracer version. The selector checks all of it against the image the
first time a patch has a supported condition edit; any mismatch, a missing or
unreadable map, an incomplete or capped test, or an import-time site falls
back to the normal check. Regenerate after any change to the SQLGlot revision,
catalog, tests, Python runtime, or the condition tracer.
