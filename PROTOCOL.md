# MetaMuse operator and worker routes

The paired `windows-human` and `local-human` identities can read `GET /v1/connections` and
`GET /v1/manager`. `POST /v1/connections/verify` accepts only `google` or `gmail` and runs the
corresponding bounded read probe. `POST /v1/manager` supports revisioned goal updates, idempotent
task creation, read, retry and cancel, revisioned Relay destination selection and publication,
and Crosswalk identity checks, public-source review dispatch and reconciliation. A browser
extension principal, even with a human role, cannot access these routes.

An optional, separate Unix socket on the same controller accepts only
`POST /v1/manager/worker`. It requires the dedicated manager role and key. Its operations
report verified inference readiness, claim a ranked packet, renew its lease, submit or record an
uncertain result. Every mutation of a claimed packet checks task revision, run ID, attempt ID and
fence. The worker identity is denied on all existing browser routes and general HTTP listeners.
When eligible work is waiting and the inference proof has expired, a claim may return
`probeRequired` without reserving a task. The same worker runs one probe and retries the claim.
Idle work and a recent failed probe do not trigger additional model calls.

Manager state is part of the existing SQLite journal. An expired worker lease becomes unknown
and needs explicit retry. Transport acknowledgements and the existing canonical Sheets writer are
independent. See [setup](MANAGER-SETUP.md) for verified and pending provider behaviors.

# Windows client protocol 1

This contract is implemented by the Windows client only. It must be reconciled with the actual Ubuntu controller source before deployment. No proxy may simply relay interactive viewer input around controller authorization.

Transport: private HTTPS with normal certificate validation, or HTTP loopback through a separately authenticated private tunnel. Every request supplies `Authorization: Bearer <paired-client-token>` and `X-Muse-Protocol: 1`. No redirects, query tokens, cookies or public raw CDP endpoint. Server maps the token to an authenticated human actor with explicitly scoped authority; actor names in bodies convey no authority. Origin/CSRF validation and authenticated extension identity must be enforced by the future bridge.

`GET v1/status`: JSON `StatusEnvelope` (see client/ControllerClient.cs). Required protocol, instance identity, monotonic response sequence, observation timestamp, installed revision, and workspace rows. The sequence increases for each response, including control responses. Workspace includes ID/name/generation/state, observed actor/role/task/phase/blocker, observedAt, optional actionStartedAt, queue and pendingSync counts, optional canonical record URL, and exact page IDs/title/document generations. State values include working/waiting/paused/human/offline/needs-you. No inference of cloud reasoning from tool gaps. Protocol mismatch or controller-instance change locks input until explicit reconnect. Per-workspace generation cannot decrease.

`POST v1/workspaces/{id}/pause|takeover|resume`: JSON with requestId, expected workspace generation, selected page/document generation, optional human grant. Reply is `{status: StatusEnvelope, grant: ControlGrant|null}`. Pause closes agent admission durably, settles/reconciles in-flight work, and reports paused only after it is safe. Takeover is separately acknowledged after reconciliation and returns a short-lived fenced human grant bound to exact actor/workspace/page/document. Resume requires explicit current human authority; a restart or expired agent lease cannot clear a human pause. Server persists the pause independently of the client lifetime. A response lost in transit must not cause automatic replay. Request IDs support server-side intent lookup and reconciliation.

`GET v1/workspaces/{id}/pages/{page}/frame?generation=N&document=ENCODED_GENERATION`: bounded PNG of that exact current page. This read-only route must not acquire input/lifecycle authority. It must refuse stale identities. The client consumes frames only while viewing and clears them on disconnect. The client limits responses to 2 MiB and verifies PNG identity; server must also bound dimensions and capture frequency.

`POST v1/workspaces/{id}/input`: requestId, workspace generation, page, pageGeneration, grant, action. Actions: `{kind: "click", x:0..1, y:0..1}` in normalized page coordinates; `{kind:"text",text:"..."}` limited to 4096 characters; `{kind:"key",key:"Tab|Enter|Escape|Backspace|ArrowUp|ArrowDown"}`. Server independently validates all authority, grant expiry, fencing and page references for every mutation. It rejects raw/replayed input without a grant even when the UI is bypassed. Return bounded JSON acknowledgment. Do not record raw text, codes or grant values in logs.

401 means pairing rejected; 403 is authorization denial; 409 means stale identity/ownership requiring reconciliation. Errors returned to the UI use fixed safe text rather than untrusted response bodies. The client never retries a POST. Transport continuity never implies browser ownership. Every independent workspace requires its own admission/ownership state; page-targeted actions must not be serialized behind unrelated desktop focus.
