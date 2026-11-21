# Agent instructions

Use [WORKSPACE.md](WORKSPACE.md) for setup, commands, strategy interfaces,
and reference scores. Follow the [PR skill](.agents/skills/pr/SKILL.md) when
opening or updating a PR. These are this repository's instructions; rules in
the upstream SQLGlot checkout do not apply here.

## Hard rules

- Build the judge-compatible image on native Linux amd64. An emulated amd64
  build can exhaust the map collector's budget and silently weaken coverage.
- Keep `sample/answers.json` on the host for sample scoring and ideal replay
  only. Never copy it into the image, mount it, or feed it to selection.
- Do not submit to the leaderboard.
- Keep the [runtime sandbox](README.md#how-your-image-runs) intact. Write
  only result JSONL to stdout; send diagnostics to stderr.

## Task routing

New idea = new class in its own file + new `EXPERIMENTS` entry; don't edit existing strategies or the baseline entry/`DEFAULT_CONFIG` for a trial. See [Try a new strategy](WORKSPACE.md#try-a-new-strategy).

### Selection

- Start in [strategies/selectors/](strategies/selectors/) and [config.py](config.py).
  For map collection, see [tools/build_file_map.py](tools/build_file_map.py)
  and [Dockerfile](Dockerfile).
- Run `make select` to inspect picks and `make e2e` to check misses. Keep the
  scheduler and worker count fixed when evaluating a selector change.
- The [MapSelector recipe](WORKSPACE.md#run-the-map-selector) runs inside
  the built image. Check the collector's result counts
  before trusting a new map.

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
- `make test` skips three collector tests unless the host has Python 3.12+
  and pytest.

## Evidence for PRs

1. Compare base and head with the same sample inputs, platform, and run limits.
   Report validity, simulated seconds, and misses for performance changes.
2. For scheduler comparisons, replay the exact same saved selections on both
   sides. Do not regenerate picks between runs.
3. Limit claims to what the run measured. Public sample results do not
   establish hidden-patch performance or leaderboard scores.
4. When reporting a score to your owner or in a PR description, use this
   format. It does not change what `make e2e` prints.

   ```
   Experiment: "map"
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
