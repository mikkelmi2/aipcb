# Router literature survey: tightening theory, congestion lineage, modern methods (M18)

A research-only session in the toporouter-postmortem style — that format worked: findings in the note's own words, mapped to aipcb components, closed with scoped candidates. No code changes. The M17 report's stretcher profile is input: the survey must answer whatever algorithmic question the profiling left open (is the stretcher asymptotically right and constant-slow, or is there better asymptotics?).

**Deliverable: `docs/notes/routing-literature.md` + a drafted proposal for the next implementation milestone, left for the user's review — this session does not start implementation.**

## Session context (fresh session)

Read `docs/reports/m17.md` (especially M17c's profile findings and the updated scaling fit), `docs/notes/toporouter-postmortem.md` (the method template and the standing candidates), and the M18 roadmap entry. The license discipline from the toporouter study applies to any source code encountered: study, never copy; academic papers are the preferred sources throughout.

## Survey scope, in priority order

### 1. Tightening theory (where 83–94 % of runtime lives — the priority)

- **Hershberger–Snoeyink**, "Computing minimum-length paths of a given homotopy class" — the canonical funnel algorithm; compare its complexity and structure to the shipping stretcher
- **Chazelle's** linear-time shortest-path machinery and what of it applies to iterated tightening
- **TopoR/Eremex published papers** on arc-based tightening and force relaxation (they did publish — find what's findable; the toporouter note's sources are a starting point)
- **Dayan's thesis, attempt two**: try harder routes to full text (university library proxies, interlibrary references, contacting UCSC, archive services). If it stays closed, say so — the second-hand markers in the toporouter note then remain
- Output question, concretely: given M17c's profile, is the right next move (a) constant-factor engineering already exhausted → algorithm replacement candidate with named algorithm, (b) incremental/lazy re-tightening architecture, or (c) a compiled kernel (numba/Rust) of the current algorithm? Each with expected gain and risk

### 2. Negotiated-congestion lineage (quality research, low priority — negotiation is < 0.5 s)

- BoxRouter, NTHU-Route 2.0, FastRoute 4.x, NCTU-GR — the ISPD-contest refinements: history-cost variants, monotonic routing, adaptive expansion
- Mine for cost-function techniques that transfer to the PCB/topological setting; note explicitly which are FPGA/ASIC-specific and don't
- Frame against the baseline fact that 10 of 11 examples converge in one iteration: candidates here must justify themselves on the boards that *don't* converge trivially (the stress/dense future, not the current corpus)

### 3. Modern and ML-based routing (surveyed for completeness, standing hypothesis: rejected)

- DeepRoute-family, RL global routing, GPU-accelerated routing — what exists, what it claims, measured against the project's determinism requirement
- The expected outcome is documented rejection in the evolution-candidate style: what was considered, why non-determinism (or other grounds) disqualifies it from the core, and under what future conditions the question reopens. If something genuinely deterministic and applicable surfaces, flag it as the exception it would be

## Note discipline (the toporouter template)

Per technique: what it is (own words, cited) → which aipcb component it touches → expected gain (tied to baseline numbers where possible) → runtime/complexity risk → verdict: candidate (scoped) or rejected (reasoned). Gaps in the record marked as gaps, never filled with speculation. Diagrams where homotopy/funnel concepts need them.

## The drafted proposal (the session's second deliverable)

From the survey's candidates, draft the next implementation milestone as a proposal file (`docs/milestones/m19-DRAFT.md` or similar, clearly marked DRAFT): the selected candidates in implementation order, each with its budget against the current baseline, acceptance criteria, and the rejection rule. **Do not begin implementation** — the draft is for the user's review; the orchestrator (if one is running) halts here by design.

## Delivery

The literature note, the roadmap updated (candidates in, survey entry closed), the DRAFT milestone proposal, and `docs/reports/m18.md` summarizing what was learned, what changed in the plan, and what the user needs to decide. Commit and push. No code.
