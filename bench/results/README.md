# Benchmark results

What `aipcb bench` writes, and which of it is committed.

## `baseline.json` — the reference

The one file here that is under version control. It is what CI's smoke run diffs
every pull request against, and what routing quality work has to show its numbers
against. It was measured at M16 on the whole bundled corpus; the run's commit,
timestamp, Python, Shapely and GEOS versions are recorded inside it, because wall
clock without a machine attached is not a measurement.

The `commit` field carries a `-dirty` suffix when the run was taken on a working
tree rather than on a commit, which is what a mid-milestone measurement always is.
A committed baseline should name a clean commit: measure again once the change is
committed, and commit the refreshed file on top.

**It moves deliberately, never incidentally.** A change that alters copper length,
via count or completion should update it in the same commit that makes the change,
and the delivery report or ADR should say why the new numbers are better. A
baseline quietly refreshed to make a red build go green is worse than no baseline.

## Everything else

`aipcb bench` writes `bench/results/<commit>.json` by default. Those are working
measurements — `.gitignore` keeps them out of the repository. Compare two of them
directly:

```
aipcb bench --out bench/results/before.json
# ... change something ...
aipcb bench --compare bench/results/before.json
```

## Reading the numbers

The two thresholds are asymmetric and the asymmetry is the point.

* **Copper length, via count, completion, and the board hash are deterministic.**
  The same input produces the same output, so any movement is a real change in
  what the router decided. `--length-threshold` defaults to 2%.
* **Wall clock is not.** `--runtime-threshold` defaults to 50% for two runs on one
  machine, and CI passes 400% because it is comparing against a file measured
  somewhere else entirely. That is enough to catch an accidental quadratic and
  useless for anything finer. Timing work worth trusting happens on one machine,
  by hand.

`over` in the human table counts over-subscribed cuts on the *routed* board. It is
pressure, not a defect: the congestion field is shared and approximate while the
stretcher is per-net and exact, so a cut over capacity on a board that is DRC-clean
and crossing-free means the coarse model was pessimistic there. A rising count
means the board got tighter, which is worth knowing when quality work claims to
have made room.
