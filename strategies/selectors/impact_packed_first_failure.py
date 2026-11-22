"""Stop after a failure only when keeping every check cannot increase the schedule."""

from .impact import runner, selector
from .impact.pack import MAX_WORKERS, WORKER_MS
from .impact_first_failure import _run_until_failure
from .impact_optimizer_transcript import OptimizerTranscriptImpact


def _no_extra_schedule(plan, ms):
    mandatory = plan.include | plan.deferred
    if not mandatory:
        return False
    replay = mandatory.union(plan.run)
    possible = replay | {n for mask in plan.pending_imports.values() for n in selector.bits(mask)}
    total = sum(ms[n] for n in possible)
    longest = max(ms[n] for n in possible)
    upper = WORKER_MS + total
    if len(replay) >= 2:
        # Two-worker LPT either leaves the longest job alone, or gives the
        # other worker at least that much load. The bound also covers subsets.
        upper = min(upper, 2 * WORKER_MS + max(longest, total - longest))
    required_ms = sum(ms[n] for n in mandatory)
    required_longest = max(ms[n] for n in mandatory)
    lower = min(
        k * WORKER_MS + max(required_longest, (required_ms + k - 1) // k)
        for k in range(1, MAX_WORKERS + 1)
    )
    return upper <= lower


class PackedFirstFailureImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        run_all = selector.run_all

        def run_gated(plans, ids, limits, deadline):
            gated = [
                plan for plan in plans
                if plan.root and plan.run and not plan.error and plan.cid not in self.proven
                and _no_extra_schedule(plan, self.ms)
            ]
            if not gated:
                return run_all(plans, ids, limits, deadline)
            roots = {plan.root for plan in gated}
            child_main = runner.child_main

            def run_child(root, *args, **kwargs):
                verify = _run_until_failure if root in roots else child_main
                return verify(root, *args, **kwargs)

            runner.child_main = run_child
            try:
                run_all(plans, ids, limits, deadline)
            finally:
                runner.child_main = child_main
            for plan in gated:
                if plan.failed:
                    plan.include.update(plan.run)
                    selector.log(f"{plan.cid}: retained all {len(plan.run)} checks after failure at no extra schedule cost")

        selector.run_all = run_gated
        try:
            return super().select(commits)
        finally:
            selector.run_all = run_all
