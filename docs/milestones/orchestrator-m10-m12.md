# Orchestrator: run milestones M10 → M11 → M12 sequentially with subagents

You are orchestrating three prepared milestone prompts, to be executed strictly in order. The goal is unattended sequential execution — the user does not want to wait and paste each prompt manually. Your job is delegation, gate-keeping, and honest stopping; the milestone prompts themselves contain all technical content.

This session is started simply by pointing you at this file — everything else it needs lives in the repo.

Read the three milestone files from the repository:

1. `docs/milestones/m10-pours.md` — copper pours, stitching vias & plane integrity
2. `docs/milestones/m11-highspeed.md` — high-speed: controlled impedance, edge connector & via transitions
3. `docs/milestones/m12-simulation.md` — SI simulation integration (openEMS/gerber2ems)

## Execution model

- Run each milestone as a **fresh subagent** (Task) that receives: the **full content of the milestone file, read from disk** — the whole text verbatim, never a summary or a paraphrase of it — plus that file's path so the subagent can re-read it, plus the instruction to begin by reading `docs/reports/` and `docs/decisions/` per its own session-context section. Fresh context per milestone is the point — do not run milestones in your own context, and do not pass conversational history between them; the repo (reports, ADRs, code, tests) is the only inter-milestone memory
- One optional early task: the M12 prompt's **Phase 0 (environment verification)** touches no repo code and may be run as a separate subagent at any time — including before or during M10/M11 — writing its findings to `docs/decisions/` as specified. If it fails, that does not block M10/M11; it only means the chain stops before M12 proper

## Gates between milestones (hard, self-enforced)

After each milestone subagent finishes, YOU verify before starting the next — do not take the subagent's word for it:

1. Run the full test suite, ruff, and mypy yourself: all green
2. Run the milestone's own acceptance checks on the examples (build/check/route as applicable): confirm 0 DRC/ERC violations and byte-stable rebuilds where the milestone requires them
3. Confirm the delivery report exists at its required path (`docs/reports/m10.md` etc.) and actually contains measured numbers, the required performance table, and the empirical findings the prompt demanded — a report of assertions without measurements fails the gate
4. Confirm a git commit (or commits) exists for the milestone so a later problem can be rolled back to a clean boundary — then **push to the remote** (`git push`). Each milestone's work must be on the remote before the next one starts, so an interrupted session never strands completed work locally. If the push fails (auth, network, no remote configured), log it in the orchestrator log and continue the chain — a failed push is not a gate failure, but it must be visible in the final report
5. Write a short gate summary into `docs/reports/orchestrator-log.md`: milestone, verdict, anything notable

Only when all five pass does the next milestone start. If a gate fails, make at most one focused remediation pass (a subagent with the specific failure), re-run the gate, and if it still fails: STOP the chain and report.

## Stopping rules (these override completion pressure)

The milestone prompts contain explicit "stop and ask" conditions — headless zone-fill unavailable (M10), stretcher changes beyond the three M11d rules (M11), toolchain/compatibility failure (M12 phase 0), and any schema-breaking need. In an unattended run these become **stop-the-chain** conditions:

- When a subagent hits one, do NOT resolve it on its behalf and do NOT let it improvise around it. Halt everything, write the situation to the orchestrator log, and end with a clear report of: what completed, what is blocked, exactly what decision or information is needed from the user
- A partially completed chain that stopped honestly is success. A fully "completed" chain that quietly worked around a stop-condition is failure — the user will trust none of it
- Uncertainty counts: if a subagent's result is ambiguous enough that you cannot honestly pass gate 2 or 3, that is a stop, not a judgment call to wave through

## Deviations and judgment calls

Milestone prompts grant implementation judgment recorded as ADRs — that stays with the subagents. But anything that would surprise the user reading the reports later (a deferred acceptance item, a weakened test, a scope trim) must be flagged prominently at the top of that milestone's delivery report AND in the orchestrator log, not buried. When in doubt whether something is a permissible judgment call or a stop-condition: it is a stop-condition.

## Final report

When the chain ends — completed or stopped — write `docs/reports/chain-m10-m12.md`: per milestone, one paragraph of status and headline numbers; the combined performance picture (pipeline times across milestones, from the reports' tables); every flagged deviation in one list; and what the recommended next action for the user is. Keep it short — the detailed reports exist; this is the executive view the user reads first when they come back. Commit and **push** this final report (and anything else uncommitted) as the last action, so the remote reflects the complete end state of the run — including any pushes that failed earlier and were retried here.
