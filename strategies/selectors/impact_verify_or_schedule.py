"""Schedule a whole verification cohort when its packed cost is lower."""

from .impact import selector
from .impact.pack import pack
from .impact_optimizer_transcript import OptimizerTranscriptImpact

LINUX_CPU_SCALE = 2.4


class VerifyOrScheduleImpact(OptimizerTranscriptImpact):
    def select(self, commits):
        verify = selector.run_all

        def verify_when_cheaper(plans, ids, limits, deadline):
            for plan in plans:
                if not plan.run:
                    continue
                scheduled = plan.include.union(plan.run, plan.deferred)
                before, _ = pack(sorted(plan.include), self.ms)
                after, _ = pack(sorted(scheduled), self.ms)
                verify_ms = plan.est_run_ms * LINUX_CPU_SCALE
                if after - before < verify_ms:
                    selector.log(
                        f"{plan.cid}: scheduling {len(plan.run)} checks and {len(plan.deferred)} "
                        f"deferred tests: {(after - before) / 1000:.3f}s vs "
                        f"{verify_ms / 1000:.3f}s estimated Linux verification"
                    )
                    plan.include = scheduled
                    plan.run = []
                    self.proven.discard(plan.cid)
            verify(plans, ids, limits, deadline)

        selector.run_all = verify_when_cheaper
        try:
            return super().select(commits)
        finally:
            selector.run_all = verify
