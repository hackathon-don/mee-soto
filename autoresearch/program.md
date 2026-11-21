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
| Don's tuned selector (closed PRs) | 30 public | 105.896 | 0 |
| `map` experiment | 30 public | 851.938 | 0 |
| `baseline` (all tests) | 30 public | 1,210.470 | 0 |

These are full-sample numbers from earlier research, not search-split scores.
`ImpactSelector` is on main but has no `EXPERIMENTS` entry yet.

## Idea queue

Ideas from the earlier research plan. Andrew adds the rest here, one bullet
per idea, in priority order.

- Table and config tracing at build time, including lookups that missed.
- Edit-kind rules: comment-only, fixture-only, and code edits handled
  differently.
- Parser replay: re-run only the parse/generate paths a patch touches.
- SQL-input features: map changed SQL fixtures to the tests that read them.
- Held-out zero-miss validation on mutated patches before keeping an idea.
- (Andrew: add the remaining ideas here.)

## Run log

Every iteration appends one row to `autoresearch/results.tsv` (search split)
or `autoresearch/validation.tsv` (validation split). Both are untracked so a
reset never erases them. Columns: time, branch, commit, experiment, split,
status, simulated seconds, miss count, missed commits, local selector CPU
seconds, and the commit subject.
