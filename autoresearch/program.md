# Autoresearch program

This file is owned by Andrew. An agent running the loop reads it and never
edits it. It adapts [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
to test selection: the agent proposes one idea at a time, each idea is scored
on a frozen benchmark, and only ideas that improve the score are kept.

## Goal

Lower the simulated total on the search split with zero added misses. The
simulated total is what `score.py` reports as exact. Selector CPU is printed
and logged next to it but is not part of the keep decision.

## Setup (once per research run)

1. Start from the PR branch that added this directory, or from main once it
   has it. Create a run branch: `git checkout -b autoresearch/<tag>`, where
   `<tag>` names the run, such as `autoresearch/nov22`.
2. Seed the kept run with the current best experiment, for example
   `make autoresearch EXPERIMENT=map`. The first iteration on a branch is
   always recorded as `seed`.

## One iteration

1. Pick the next idea from the queue below.
2. Implement it as a new strategy class in its own file under
   `strategies/selectors/` or `strategies/schedulers/`, plus a new
   `EXPERIMENTS` entry in `config.py` named for the idea. Optional build-time
   indexing goes in a new file under `tools/`, wired in through `Dockerfile`.
3. Commit it with a one-line subject that says what the idea is.
4. Run `make autoresearch EXPERIMENT=<name>`. It builds the image, runs the
   experiment under the scorer's limits on the search split only, and prints
   a decision:
   - `KEEP`: lower simulated total and no commit missed that the kept run
     caught. The commit becomes the new kept run.
   - `DISCARD`: anything else. The branch is reset to the kept commit; the
     printed SHA can restore the idea for inspection.
   - `CRASH`: the run failed or its output was rejected. Treated like discard.
   - `REJECT`: the commit breaks a rule below, so it is not run. Treated like
     discard.
5. Read the result, note what it teaches, and pick the next idea. Do not stop
   to ask whether to continue.

## Rules

- Never edit an existing strategy class or an existing `EXPERIMENTS` entry,
  and never change `DEFAULT_CONFIG` or the `baseline` entry. A new idea is a
  new class plus a new named experiment. To build on a kept idea, subclass or
  copy it into a new file.
- The editable surface is new files under `strategies/` and `tools/`,
  additions to `config.py`, and `Dockerfile` edits for build-time indexing.
  `make autoresearch` rejects a commit that touches anything else, including
  `score.py`, `main.py`, `Makefile`, `sample/`, `tests/`, and this directory.
- Never read `sample/answers.json`, the validation patches, or
  `autoresearch/validation.tsv`, and never name a sample commit id in code.
  The loop rejects added lines that name one.
- Never run `make autoresearch-validate`. It is for Andrew.
- Keep the scheduler and worker count fixed when testing a selector idea, and
  the selector fixed when testing a scheduler idea.
- Follow [AGENTS.md](../AGENTS.md): native amd64 builds only, keep the runtime
  sandbox intact, and never submit to the leaderboard.
- Each experiment name is evaluated once per branch. Rename to retry a variant.

## Benchmark

- `splits.json` freezes the 30 public patches into a 21-patch search split
  and a 9-patch validation split, stratified by answer kind. The loop only
  ever mounts the search split's patches.
- The validation split stays outside the loop so ideas are not tuned to it.
  Andrew runs `make autoresearch-validate EXPERIMENT=<name>` on a kept idea
  before promoting it, and the result goes to `validation.tsv`.
- The leaderboard appears to run 40 patches, so a search-split gain is only a
  candidate until validation agrees.
- Not yet frozen here: a held-out set of mutated and historical SQLGlot
  patches. `tools/mutants.py` and `tools/groundtruth.py` can generate one.

## Reference points

| What | Patches | Simulated s | Misses |
| --- | ---: | ---: | ---: |
| Answer-key floor | 30 public | 94.351 | 0 |
| Don's tuned selector (reported from his PRs) | 30 public | 105.896 | 0 |
| `map` experiment | 30 public | 851.938 | 0 |
| `baseline` (all tests) | 30 public | 1,210.470 | 0 |

These are full-sample numbers from earlier research, not search-split scores.
`ImpactSelector` (#15) is on main but has no `EXPERIMENTS` entry yet.

## Idea queue

From Andrew's research notes (first-principles research plus a paper search), in the
recommended order. Take the first idea that has not been tried on this branch.
Andrew adds or reorders ideas here.

1. **Worker sweep (scheduler idea, selector fixed).** A new scheduler that
   runs integer-ms LPT for every worker count `k = 1..8` and picks the lowest
   `makespan + 2000k`. Stop early when it equals the floor
   `LB_k = max(longest_test, total / k) + 2000k`. On the full catalog this
   picks `k = 4` at 36.817 s per patch against 40.349 s for the fixed three
   workers.
2. **AST edit-kind rules.** Parse old and new files with `ast`, classify each
   edit (new branch, deleted or renamed name, fixture file, other), and apply
   a rule per kind. Keep three states: affected, unaffected, unknown. Unknown
   widens the selection. New lines have no coverage, so anchor them to the
   enclosing function or class and to names they reference. Deleted names go
   through static references and imports. Map fixture files such as
   `tests/fixtures/identity.sql` to the tests that open them, recorded at
   build time. Never skip comment-only or formatting edits: tests can read
   `__doc__`, source text, or line numbers. Treat `getattr`, `importlib`,
   `exec`, decorators, class bodies and module-level code as unknown.
3. **Table and config entry tracing.** At build time, record which keys each
   test looks up in SQLGlot's tables (`KEYWORDS`, `FUNCTIONS`, `TOKENS`,
   dialect settings), including lookups that missed. A patch that edits one
   entry then selects only tests that read that key, or that would now find
   it. Example: Snowflake escape patch `4d63` drags in 275 tests (9.079 s)
   with 0 failures under class-level coverage. A test that iterates, copies
   or mutates a whole table depends on all of it.
4. **Slack-aware group inclusion and full-suite fallback.** Price an
   uncertain test by how much it raises the packed score, not by its own
   duration: a test that fits under the busiest worker costs 0 s. Decide on
   correlated tests as a group, since catching some but not all failing tests
   still costs the full 300 s. Fall back to the full suite when its extra
   cost is below `300 * P(miss)`. Do not tune drop thresholds on the public
   patches: 0 misses on 30 patches still allows a 9.5% miss rate at 95%.
5. **Pure-expression state checks (only if 1 to 4 leave expensive false
   positives).** For an edit to a side-effect-free expression, select only
   tests whose recorded inputs make the old and new expression disagree, such
   as `n == 0` for `n > 0` to `n >= 0`. Count raised exceptions as a
   difference. No SMT solvers or symbolic execution at selection time.
6. **Parser-call replay (deferred).** Record the parser inputs each slow test
   sends at build time and, at selection time, replay only the calls that
   touch the patch. Skip the test when every output matches. Selector CPU
   counts one to one against worker time, so this only pays off for slow,
   multi-case tests such as `TestTranspile.test_identity`.
7. **Held-out zero-miss validation.** Freeze a larger held-out set of mutated
   and historical patches (see Benchmark) and require zero misses on it
   before an idea is promoted.

Ideas 2 to 6 add selector CPU. It is logged separately and not part of the
keep decision, so note it in the description when it grows.

## Run log

Every iteration appends one row to `autoresearch/results.tsv` (search split)
or `autoresearch/validation.tsv` (validation split). Both are untracked so a
reset never erases them. Columns: time, branch, commit, experiment, split,
status, simulated seconds, miss count, missed commits, local selector CPU
seconds, and the commit subject.
