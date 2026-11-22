from .impact_stacked_maps import StackedMapsImpact


class HeavyDropImpact(StackedMapsImpact):
    def select(self, commits):
        self._must_fail = {}
        return super().select(commits)

    def _analyze_commit(self, ctx, patch):
        analysis = super()._analyze_commit(ctx, patch)
        self._must_fail[patch.commit] = set(analysis.must)
        return analysis

    def _selected(self, plans):
        heavy = {1106, 1224, 1490}
        for plan, (cid, selected) in zip(plans, super()._selected(plans), strict=True):
            unsupported = heavy.difference(plan.failed, self._must_fail.get(cid, ()))
            yield cid, [number for number in selected if number not in unsupported]
