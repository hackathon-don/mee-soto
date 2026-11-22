"""Prioritize first-failure checks by their coverage of changed lines."""

from collections import Counter, defaultdict
from itertools import groupby

from .impact import selector
from .impact.diffparse import parse_patch
from .impact.impact import _changed_lines
from .impact_first_failure import FirstFailureImpact


def _line_masks(index, diff):
    masks = Counter()
    for patch in parse_patch(diff):
        if patch.old_path is None:
            continue
        old_lines, _ = _changed_lines(patch)
        for line in old_lines:
            mask = index.run_mask(patch.old_path, line, line) | index.def_mask(patch.old_path, line, line)
            if mask:
                masks[mask] += 1
    return masks


def _test_mask(number, subsets, pseudo):
    queries = subsets.get(number)
    if queries is not None and pseudo and number == pseudo["test"]:
        return (1 << pseudo["setup"]) | sum(1 << pseudo["queries"][q] for q in queries)
    return 1 << number


def _ordered(tests, ids, masks, subsets, pseudo):
    ranks = {}
    modules = defaultdict(list)
    for position, number in enumerate(tests):
        candidates = _test_mask(number, subsets, pseudo)
        overlap = sum(count for mask, count in masks.items() if mask & candidates)
        ranks[number] = (-overlap, position)
        modules[ids[number].split("::", 1)[0]].append(number)
    if not any(overlap for overlap, _ in ranks.values()):
        return tests

    # The runner batches by module and uses contiguous class fixtures.
    def rank(number):
        return ranks[number]

    def test_class(number):
        return ids[number].rsplit("::", 1)[0]

    ordered_modules = []
    for group in modules.values():
        blocks = [sorted(block, key=rank) for _, block in groupby(group, test_class)]
        blocks.sort(key=lambda block: rank(block[0]))
        ordered_modules.append([number for block in blocks for number in block])
    ordered_modules.sort(key=lambda group: rank(group[0]))
    return [number for group in ordered_modules for number in group]


class OrderedFirstFailureImpact(FirstFailureImpact):
    def select(self, commits):
        if self.ctx is None:
            return super().select(commits)
        diffs = {patch.commit: patch.diff for patch in commits}
        index = self.ctx.ix
        spawn = selector.spawn_child

        def spawn_ordered(plan, tests, ids, limits):
            if len(tests) > 1:
                masks = _line_masks(index, diffs[plan.cid])
                tests = _ordered(tests, ids, masks, plan.subsets, index.pseudo.get("tpcds"))
            return spawn(plan, tests, ids, limits)

        selector.spawn_child = spawn_ordered
        try:
            return super().select(commits)
        finally:
            selector.spawn_child = spawn
