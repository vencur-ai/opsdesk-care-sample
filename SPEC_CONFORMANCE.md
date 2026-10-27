# Spec conformance

Pack `opsdesk-care-sample` for Interested parties (public sample), OpsDesk-Care 2.4.13.

| Item | Status | Evidence |
|---|---|---|
| Reward definition | Deterministic score in [0, 1]: gates L1 validity, L2 hard rules, L3 coverage (0 if any fails); weighted L4 cost vs CP-SAT reference, L5 stability, L6 notifications/family outputs, L7 honesty. Strict honesty on: any false claim scores 0. No LLM judge. | VERIFIER_SPEC.md |
| Success definition | Fully solved = all gates + honesty + score >= 0.999. | TEST_REPORT.md |
| Determinism | Same actions give byte-identical state, tool results and score; checked per task (`determinism` probe). | HACK_AUDIT.md |
| Isolation | One episode per reset, per-client quotas, host-only routes behind a host key, agents see tool outputs only. | VERIFIER_SPEC.md |
| Reference secrecy | Public sample: references of exactly its 3 sample-only tasks under `private/references/` (fresh seeds, used in no paid pack or internal evaluation); no solver or generator code; deny-list check passed at build. | README.md, LICENSE.md, manifest.json |
| Reward hacking | Per task: no-op, liar and gaming probes ≤ 0.5, gold ≥ 0.999; 3 of 3 tasks OK. | HACK_AUDIT.md |
| Honesty check | Final report claims (gaps, covered count, violations, cost, status) are compared with the recomputed truth; liar probes are caught for every task. | HACK_AUDIT.md |
| Task quality | Every task passed the fail-closed QA gate (proven reference, instruction scope = gate scope, every enforced rule stated in documents, step limits stated, inputs visible). | tasks/*/qa.json |
| Step limits | 3,000 state-changing / 10,000 total tool calls per episode, stated in every instruction. | VERIFIER_SPEC.md |
| Versions | Environment 2.4.13, verifier v2.4.13, Python ≥ 3.12, hash-pinned runtime lock. | runtime/requirements.lock |
| Checksums | SHA-256 for every file in SHA256SUMS; per-task agent-bundle checksum. | manifest.json |
