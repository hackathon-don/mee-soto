# Autoresearch program

Driver: bandrew

This file is owned by Andrew. An agent running the loop reads it and never
edits it. It adapts [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
to test selection: the agent proposes one idea at a time, each idea is scored
on a frozen benchmark, and only ideas that improve the score are kept.

## Goal

Lower the simulated total on the search split with zero added misses. The
simulated total is what `score.py` reports as exact. Selector CPU is printed
and logged next to it but is not part of the keep decision.

## One driver

Only one person or agent runs the loop: the driver named above. Everyone else
adds ideas to the queue below by committing to `program.md` on the
`autoresearch` branch, and does not run `make autoresearch`. The driver never
edits this file. Everything happens on the `autoresearch` branch; there are no
per-person or per-run branches.

## Setup (once, by the driver)

1. Check out the `autoresearch` branch.
2. Seed the kept run with the current best experiment, for example
   `make autoresearch EXPERIMENT=impact`. The first iteration is always
   recorded as `seed`.

## One iteration

1. Run `git pull --no-rebase` to pick up new ideas, then take the next idea
   from the queue below. Never rebase: the kept commit must stay an ancestor
   of HEAD.
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
5. After a `KEEP`, push. Never push a commit before its decision, so a reset
   never needs a force push.
6. Read the result, note what it teaches, and pick the next idea. Do not stop
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
  The one exception is `program.md` commits from others, pulled in with new
  ideas; the driver itself still never edits it.
- Never read `sample/answers.json` or `autoresearch/validation.tsv`, and
  never name a sample commit id in code.
  The loop rejects added lines that name one.
- Never run `make autoresearch-validate`. It is for Andrew.
- Keep the scheduler and worker count fixed when testing a selector idea, and
  the selector fixed when testing a scheduler idea.
- Follow [AGENTS.md](../AGENTS.md): native amd64 builds only, keep the runtime
  sandbox intact, and never submit to the leaderboard.
- Each experiment name is evaluated once per branch, and a candidate's name
  must not already exist in `config.py` at the kept commit. Rename to retry a
  variant.
- Merge main into `autoresearch` only before seeding. A merge during a run
  touches files outside the editable surface, so the loop rejects it and
  resets.

## Benchmark

- The loop tunes on all 30 public patches (the search split in
  `splits.json`).
- The validation split is empty for now, so nothing scores a kept idea on
  patches it was not tuned on, and `make autoresearch-validate` stops with an
  error. The leaderboard appears to run 40 patches, so a public-sample gain is
  only a candidate.
- Next: freeze a held-out set of mutated and historical SQLGlot patches as the
  validation split (idea 7). `tools/mutants.py` and `tools/groundtruth.py` can
  generate one. Andrew then runs `make autoresearch-validate EXPERIMENT=<name>`
  on a kept idea before promoting it, and the result goes to
  `validation.tsv`.

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

Ideas 1 to 7 come from Andrew's first research notes, in their recommended
order; 8 to 11 and the safety harness come from his second notes. Take the
first idea not yet tried. Anyone but the driver adds or
reorders ideas here; Andrew has the final say on order.

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

8. **SQL-input feature inventory.** At build time, record what each test's
   SQL inputs exercise: read and write dialect, parsed node types such as
   `exp.Coalesce`, function names, identifiers, and tokenizer or parser error
   categories (keep raw tokens for malformed inputs). A patch to a parser,
   generator, optimizer or dialect rule then includes the tests whose inputs
   use that feature. Use it only to include tests: a missing record is not
   evidence that a test is safe.
9. **Dense integer bitsets.** Store each test set as a Python `int` over the
   1,620 catalog numbers and combine sets with `|`. This changes no
   selection, only selector CPU. Try it once a selector has real indexes, and
   measure the whole selector including conversion back to test numbers.
10. **Reuse pure analysis across the batch.** Every patch targets the same
    pinned base, so load indexes and parse baseline sources once per run.
    Reuse a result only on an exact match of patched content and config, never
    carry one patch's source tree into the next, and never ship a
    patch-to-tests table.
11. **Assertion-aware checked dependencies (exploratory, last).** A build-time
    dynamic slice from each test's assertions separates "reached this code"
    from "checked a value this code produced". Absence from the slice must not
    drop a test on its own: new branches, exceptions before the assertion and
    module-level effects are not in a baseline slice. Expensive to build.

### Safety harness

These are not loop experiments: they produce evidence about an index or a
selector. Put them under `tools/` and run them by hand, outside
`make autoresearch`.

- **Cold, warm and reordered profiling audit.** Build the dynamic index in a
  fresh process, in reversed and shuffled test orders, and in a warm process,
  then compare each test's recorded dependencies. A difference points to lazy
  imports, caches or global registrations the index misses.
- **Multi-edit interaction checks.** On clean copies of the pinned tree,
  compare failing tests for edit A, edit B and A+B. When A+B fails a test
  outside the union selected for A and B, add a general rule or fallback.
  Public patch `rszg` (three files) is a starting case.
- **Delta-debug a missed selection.** When a candidate misses a failing test
  on a public patch, shrink the patch until it is 1-minimal while it still
  applies, the same test fails the same way, and the selector still omits it.
  The small case shows which general rule is missing.
- **Selector metamorphic checks.** Encodings of the same change (reordered
  file blocks or hunks, extra context lines) must give the same selected
  set once their applied trees are verified identical, and so must batch
  order and cold versus warm caches. Never assume comment or whitespace edits
  are harmless, or that selection is monotone across combined patches.

Ideas 2 to 6, 8, 10 and 11 add selector CPU. It is logged separately and not
part of the keep decision, so note it in the description when it grows.

## Run log

Every iteration appends one row to `autoresearch/results.tsv` (search split)
or `autoresearch/validation.tsv` (validation split). Both are untracked so a
reset never erases them. Columns: time, branch, commit, experiment, split,
status, simulated seconds, miss count, missed commits, local selector CPU
seconds, and the commit subject.

## Additional queue ideas

12. **Recover interrupted verification.** If an Impact child dies outside a test,
    try one bounded same-module cohort of tests it left unrun in a fresh process.
    Require a clean full test lifecycle for each member. Keep the entire cohort
    on uncertainty. Leave scheduled workers and existing failure handling fixed.
13. **Prove literal registry reads unaffected.** Apply the distinction between
    observed first-match actions and unchanged operation results from APKeep and
    Rust red-green queries to one literal registry. Require complete consumer
    evidence and preserve unknowns, missing keys, dynamic bindings, precedence,
    and whole-table access.
14. **Amortize verification setup across a module cohort.** For groups included
    by Impact's initial cost plan, estimate the fixed schedule cost of removing
    an entire same-module cohort and charge its setup once. Compare this with a
    bounded fresh verification run. Keep the cohort unless every member completes
    an unmodified full test lifecycle successfully.

15. **Prioritize verification before the failure cutoff.** Use regression-test
    prioritization to rank already planned module groups by the fixed schedule
    cost avoided per unit of verification CPU. Preserve order within a module,
    the original tests, deadline, failure ceiling, and fallback behavior.
16. **Budget a deferred test against the actual schedule.** Consider full
    independently verified execution of a deferred test only when the fixed
    schedule savings cover the measured CPU and setup estimate. Keep it on
    timeout, partial verification, or failed lifecycle checks.
