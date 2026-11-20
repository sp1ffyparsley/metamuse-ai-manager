# AI agent handoff

Review the exact commit requested by the operator. Start with [README](README.md), [Acceptance](ACCEPTANCE.md), [Setup](MANAGER-SETUP.md), and [Protocol](PROTOCOL.md). Use source behavior to resolve conflicting descriptions. Separate implemented code, fixture verification, live observations, and open work in your conclusions.

## Architecture

- Windows WPF is the operator interface. The Work Board creates, ranks, and displays tasks and receipts. The transport and the Ubuntu controller enforce the authority for each operation.
- The Ubuntu controller owns the existing SQLite journal, task revisions, scope, leases, and outbox. Keep it authoritative. The browser bridge uses existing workspaces and profiles and separates read-only viewing from human takeover.
- Hermes runs in a scoped Ubuntu inference process using a hosted OpenAI Codex model through attended OAuth. It performs bounded planning, synthesis and review. The model does not receive controller credentials, provider secrets, or browser authority.
- Crosswalk is a private transport between verified role identities. A public research task may preauthorize one automatic request to an exact selected recipient. The controller checks the board and identities and only posts its verified public citations. Delivery, agent pickup, and receipt verification are distinct.
- Google APIs serve reads and the optional Drive Research Relay. A saved Drive packet is distinct from a verified scheduled consumer receipt. Canonical production writes require guarded cutover.
- The Windows app can look up approved excerpts from the user's existing local AgentVault. The vault and its content are external to this repo.

## Maintainership

- Extend the existing journal, worker trigger, and adapters before introducing new services.
- Keep each role distinct. Respect native consent, browser holds, and scope. Reviewers should use synthetic data and local fixtures.
- Prefer low-dependency, incremental improvements with bounded resource use. Preserve compatibility with old task records and mixed Windows and Ubuntu versions.
- Keep DeepAI, Bitwarden redesign, and personalized writing-style storage outside this review.
- Public source contains synthetic deployment values. Never infer a provider account, permission, or acceptance result from the examples.

## Review targets

For product research, prioritize a useful end-to-end review visible in the Work Board, supported unattended pickup, useful permission holds, and measurable creative workflow gains. For correctness review, trace revision fencing, restart and cancellation, exact Crosswalk identity and receipts, compatibility, and journal recovery. See [Acceptance](ACCEPTANCE.md) for the boundary between demonstrated and planned behavior.
