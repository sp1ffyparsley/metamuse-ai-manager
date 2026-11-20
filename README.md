# MetaMuse AI Manager

MetaMuse adds an AI Work Board to Muse Monitor. The Windows HUD is the operator interface. An Ubuntu controller manages task state and runs a scoped Hermes worker for research and draft review. Crosswalk carries review requests to a verified Muse identity. Google adapters support read verification and an optional research handoff.

This repository is a public source snapshot for independent review. Tests use local fixtures. Deployment values in the sample source are synthetic, and the snapshot carries no live access to the original installation.

| Start here | Contents |
| --- | --- |
| [AI agent handoff](AI_MASTER_PROMPT.md) | Architecture, scope, current capability, and review priorities |
| [Setup and verification](MANAGER-SETUP.md) | Operator flow, runtime requirements, and local test commands |
| [Acceptance](ACCEPTANCE.md) | Observed results, fixture coverage, and open qualification |
| [Protocol](PROTOCOL.md) | Controller, worker, and Windows client interfaces |
| [Third-party notices](THIRD-PARTY-NOTICES.md) | Attributions for upstream components and adapted patterns |

`windows/` and `client/` contain the HUD and transport. `linux/muse_monitor/` contains the controller, browser and integration adapters, and journal logic. `extension/` contains the existing Chrome bridge. `tests/` and `linux/tests/` contain verification code.

The snapshot includes the API and integration implementations so reviewers can follow real call paths. It uses synthetic Google record IDs, network addresses, repository references, account labels, and public key pins. Live configuration, original operational evidence, prior history, and build artifacts are held separately.
