from .impact import selector
from .impact_stacked_maps import StackedMapsImpact


HEAVY_TESTS = {1106, 1224, 1490}


def _promote_subsets(plan, must, cpu):
    targets = {
        n for n in (plan.include | plan.deferred) & HEAVY_TESTS
        if n not in must and plan.subsets.get(n)
    }
    if targets:
        plan.include.difference_update(targets)
        plan.deferred.difference_update(targets)
        plan.run = sorted(set(plan.run) | targets, key=lambda n: (cpu[n], n))
        plan.notes.append(f"verify heavy subsets {sorted(targets)}")
    return targets


class VerifyHeavyImpact(StackedMapsImpact):
    def select(self, commits):
        orig_choose, orig_run_all = selector.choose, selector.run_all
        promoted = {}

        def choose(plan, cand, must, never, ms, cpu, ids, deferrable):
            orig_choose(plan, cand, must, never, ms, cpu, ids, deferrable)
            targets = _promote_subsets(plan, must, cpu)
            if targets:
                promoted[plan] = targets

        def run_all(plans, ids, limits, deadline):
            temporary = []
            for plan in promoted:
                if plan.diagnostics is None:
                    plan.diagnostics = {"timed": [], "incomplete_children": 0, "checked": []}
                    temporary.append(plan)
            try:
                orig_run_all(plans, ids, limits, deadline)
            finally:
                for plan, targets in promoted.items():
                    completed = {n for n, _ in plan.diagnostics["timed"]}
                    plan.include.update(targets - completed)
                for plan in temporary:
                    plan.diagnostics = None

        selector.choose, selector.run_all = choose, run_all
        try:
            return super().select(commits)
        finally:
            selector.choose, selector.run_all = orig_choose, orig_run_all
