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

### Autoresearch

- When asked to run the autoresearch loop, follow
  [autoresearch/program.md](autoresearch/program.md) and do not edit it.

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
