# Agent instructions

Use [WORKSPACE.md](WORKSPACE.md) for setup, commands, strategy interfaces,
and reference scores. Follow the [PR skill](.agents/skills/pr/SKILL.md) when
opening or updating a PR. These are this repository's instructions; rules in
the upstream SQLGlot checkout do not apply here.

## Hard rules

- Build the judge-compatible image on native Linux amd64.
- Keep `sample/answers.json` on the host for sample scoring and ideal replay
  only. Never copy it into the image, mount it, or feed it to selection.
- Do not submit to the leaderboard.
- Keep the [runtime sandbox](README.md#how-your-image-runs) intact. Write
  only result JSONL to stdout; send diagnostics to stderr.

## Task routing

New idea = new class in its own file + new `EXPERIMENTS` entry; don't edit existing strategies or the baseline entry/`DEFAULT_CONFIG` for a trial. See [Try a new strategy](WORKSPACE.md#try-a-new-strategy).

### Autoresearch

- When asked to run the autoresearch loop, follow
  [autoresearch/program.md](autoresearch/program.md) and do not edit it.
- Only one driver runs the loop at a time. Otherwise, add ideas to the queue
  in `autoresearch/program.md` on the `autoresearch` branch instead of
  running it.

### Selection

- Start in [strategies/selectors/](strategies/selectors/) and [config.py](config.py).
- Run `make select` to inspect picks and `make e2e` to check misses. Keep the
  scheduler and worker count fixed when evaluating a selector change.

### Scheduling

- Start in [strategies/schedulers/](strategies/schedulers/) and [config.py](config.py).
  Use `make schedule` to replay picks without running the selector.
- Preserve the [scheduler contract](WORKSPACE.md#selection-and-scheduling-interface).

### Evaluation and runtime

- See [main.py](main.py), [Makefile](Makefile), and [score.py](score.py).
  [SCORING.md](SCORING.md) defines valid output and scoring.
- Use `make e2e` (or `make e2e-amd64` explicitly) for judge-compatible scores
  and misses. Use `make e2e-dev` for native-architecture iteration, not
  judge-compatible measurements. `make check` verifies output format only.
- Use `make e2e-holdout` or `make e2e-full` to check misses on the held-out
  set after `make holdout` has labeled it. They use the native dev image, so
  they are not judge-compatible, and the held-out labels stay on the host like
  `sample/answers.json`. Don't tune on the held-out results you report.

## Evidence for PRs

1. Compare base and head with the same sample inputs, platform, and run limits.
   Report validity, simulated seconds, and misses for performance changes.
2. For scheduler comparisons, replay the exact same saved selections on both
   sides. Do not regenerate picks between runs.
3. Limit claims to what the run measured. Public sample results do not
   establish hidden-patch performance or leaderboard scores.

## Reporting scores

Whenever you report a score, use this format. PRs do not need to include it,
and it does not change what `make e2e` prints.

```
Experiment: "<experiment-name>"
Score: 852.1 s (median)

| Run      | Simulated | CPU    | Missed | Total     |
|----------|-----------|--------|--------|-----------|
| 1 of 3   | 851.94 s  | 0.12 s | 0      | 852.06 s  |
| 2 of 3   | 851.94 s  | 0.11 s | 0      | 852.05 s  |
| 3 of 3   | 851.94 s  | 0.12 s | 0      | 852.06 s  |
| baseline | 1210.47 s | 0.07 s | 0      | 1210.54 s |

Machine: linux/amd64
Commit hash: <40 character hash>
```

- Run the experiment three times. Total is simulated plus CPU seconds;
  the score is the median of the three totals, to one decimal.
- Add a `baseline` row measured the same way for comparison.
- Give the full 40-character commit hash, or `Commit hash: NOT COMMITED`
  for uncommitted work.
