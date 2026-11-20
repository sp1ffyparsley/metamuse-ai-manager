# Muse tasks — prepare, ask, verify

Use this workflow for authorized work of **any topic**: applications, research,
school, planning, writing, coding or administration. A topic is a label; it does
not create another Chrome profile, account, scheduler or agent. Main and Secondary
remain browser resources. Existing permissions, factual-source rules, website
rules, human pauses and authentication challenges still apply. A ready packet
does not authorize a purchase, submission, message, invented fact or other action.

## Main's routine

1. At the start of a run, read `work-brief` through the existing helper. One
   response includes tasks, Needs you/receipts, saved site profiles and unresolved
   challenges; follow the task cursor with `task list` when relevant.
   Read `tasks-help` on first use or when the controller revision changes, rather
   than fetching the full guide on every run. `needs-you` is an optional filtered
   shortcut, not a second mandatory read. Check pending work before creating
   duplicates and reuse the existing tracker/canonical ID. No helper replacement
   is needed.
2. Before execution, save one concise packet with the goal, intended person or
   subject, source links, prepared notes/answers, exact required files, next safe
   action and explicit success criteria. Link large material. Use `artifacts`
   for file names and hashes; use `admit DRIVE_LINK` to prepare missing documents.
   Preparation and task reads work while browsers are human-paused.
3. For resumed work, read the packet before acting: it includes current
   file/readiness problems. For a freshly saved packet, use the returned record.
   Claim the existing item (`claim-item`, with `item` and the prepared/read
   `revision`); the controller checks the exact packet revision, readiness and
   file versions again. Use the revision returned by the claim, without another
   read unless a conflict or uncertain response requires it. Acquire browser
   control separately only when permitted. This workflow never
   resumes a human pause. Keep factual provenance and applicant/contact identity
   in the packet notes or linked approved sources. For applications, resolve
   eligibility, identity, writing rules and required materials before submission.
4. Save the existing `checkpoint` after meaningful progress. It records the exact
   stopping point and pending/uncertain action; use `checkpoint-read` after a
   disconnect. A checkpoint is not completion evidence. Check the current page
   before continuing; never replay an uncertain submit or payment.
5. If a person is needed, `block` the item with the reason, exact question/action,
   page link and next safe action. This releases the item claim, not browser
   ownership. Release browser control separately. Preserve its form and tabs;
   continue other independently authorized ready tasks. Do not wait in a retry
   loop or mark it done. The on-demand **Muse Tasks** desktop view shows this list.
6. After a response, verify the change. Update the packet to remove answered
   questions and prepare missing files; an existing blocker remains until
   `unblock` records the observed resolution. Read the new revision before acting.
   A resolved task blocker does not dismiss Google challenges or resume Chrome.
7. Record `receipt` for **each** declared success criterion. Then `finish` with
   a precise result summary. Report the evidence's actual scope. Never write
   "applied", "sent", "uploaded" or "completed" from a successful click alone.
   Use verified local receipts as evidence when updating the existing tracker
   through Main's separately authorized connector. VM canonical bridge writes
   remain governed by their existing setting; no new sync writer was enabled.

## Transport and schema

Simple reads: `guest.sh work-brief`, `guest.sh tasks-help`, `guest.sh tasks`, `guest.sh needs-you`.
For updates, pass JSON below as base64url UTF-8 to the existing
`guest.sh monitor-encoded BASE64URL_JSON`. This avoids quote/space loss in SSH.
Use the actual existing helper location. Do not install the held replacement.

```json
{
  "op": "task", "action": "save", "id": "EXISTING-TRACKER-ID", "revision": 0,
  "packet": {
    "title": "Compare community garden options",
    "topic": "Local research",
    "goal": "Prepare a sourced comparison for the user",
    "workspace": "jobs",
    "subject": "The user's garden project",
    "sources": ["https://example.com/gardens"],
    "files": [],
    "draft": "Approved facts, source versions and prepared notes go here.",
    "questions": [],
    "nextAction": "Prepare the comparison PDF",
    "successCriteria": [
      {"id": "report", "description": "The comparison PDF exists", "evidence": "file"}
    ]
  }
}
```

`workspace` is optional/null for work without a browser; `jobs` means the existing
Main resource, not a restriction to job topics. Other current resource ID:
`scholarships` (Secondary). Topic can be any short subject label.
`files` entries are `{"name":"Exact_Name.pdf","sha256":"64 lowercase hex chars"}`.
Current admitted document formats remain PDF/DOCX, including Google Docs→PDF.
Other outcomes can use a visible page receipt or explicit human confirmation.

Required packet fields: `title`, `topic`, `goal`, `nextAction`, `successCriteria`.
Sources, subject, draft notes, files and questions are optional. A packet can be
saved before it is ready; unanswered questions put it on Needs you, and missing
or changed required files make it unavailable for execution. Readiness describes
preparation completeness, not independent verification of every factual claim.

Existing items use their current item revision, not 0. Read the task or the
existing queue/item claim response first. Every mutation after creation requires
the last returned revision. On stale/replayed response errors, **read**; never
automatically replay a write. Changing packet content invalidates prior receipts.
Completed records are immutable. Create a separate follow-up for new work.

Operations (all include `op: "task"`):

| action | Additional fields |
| --- | --- |
| `list` | Optional `state`: all / ready / draft / needs-you / done; `cursor` for another page |
| `read` | `id` |
| `save` | `id`, `revision`, `packet` |
| `block` | `id`, `revision`, `reason`, `question`, `nextAction`, optional `url` |
| `unblock` | `id`, `revision`, `resolution` describing what was checked |
| `receipt` | `id`, `revision`, `criterion` plus evidence fields below |
| `finish` | `id`, `revision`, `summary` |

Each list is bounded to 50 records. Follow `nextCursor` until null when scanning
the whole queue. Input packets are at most 8 KiB so they fit the existing encoded
SSH request; large reports and drafts should be linked. Records including receipts
are at most 32 KiB, with at most 12 files, source links,
questions and success criteria each. No passwords, tokens, codes or cookies.

## Evidence that the controller checks

Choose a criterion's evidence type before execution:

- **`page`**: requires `page`, `pageGeneration`, exact current `url`, and specific
  visible `text` (8–500 characters). Get exact page identities from
  `guest.sh monitor status`, not a tab index or stale element ref. The controller
  reads the current page without clicking, checks URL/document identity and
  confirms that text is visible. It stores only that narrow confirmation and
  server check time, not the full form. For submission, use the portal's actual
  confirmation/application ID. For an upload, use its accepted attachment label.
  A file receipt alone cannot satisfy a criterion declared as `page`.
- **`file`**: requires `name` and `sha256`. The controller checks the bytes and
  permitted format inside Muse Files and rechecks at completion. It proves the
  local document exists at that version; it does **not** prove delivery, an
  employer accepted it, the report's quality or that code tests passed.
- **`human`**: requires `confirmation` from an authenticated human principal.
  Main cannot self-certify this criterion. Its receipt is labelled human
  attestation, not an independent website check. Useful for offline/physical
  work or a decision only the user can confirm. On the Muse desktop, open
  **Muse Tasks → Confirm a result**, choose the task/result, and describe what
  you personally checked. Cancelling any dialog records nothing.

Example page receipt after an authorized submission:

```json
{"op":"task","action":"receipt","id":"EXISTING-TRACKER-ID","revision":3,"criterion":"submitted","page":"p7","pageGeneration":"CURRENT_DOCUMENT_ID","url":"https://example.com/confirmation","text":"Your application has been received"}
```

The packet must already declare that `submitted` criterion with `evidence: page`.
Evidence is tied to that packet version and attributed to its checker. The
controller verifies the narrow observation; Main must still interpret whether
the wording proves the intended result. Do not use unrelated or generic text as
proof of a stronger claim. Respect later authentication challenges.

## Lightweight by design

Packet, blocker and receipts live inside the **existing SQLite item record**.
The existing checkpoint table stores save points. No new database, cloud folder
mirror, daemon, dependency, extension, polling loop, scheduler or duplicate tracker.
Readiness/file hashing happens on explicit preparation/read/claim/completion,
not each HUD refresh. Muse Tasks is a native text window opened only on request;
Refresh performs one bounded read and does not touch the browser.

The replacement cloud helper remains on hold. This task protocol uses the
existing approved connection and neither changes that hold nor bypasses a
tool-result safety rejection. If Main's own checks reject retrieved content,
respect that decision and report the exact blocked step to the user.

## Site continuity and fewer browser visits — handler 10

The controller never clears cookies/history, changes browser identity, solves a
CAPTCHA, rotates networks, or copies sessions between profiles for this workflow.
CAPTCHAs may recur even with persistent cookies. Main/Secondary retain separate
site sessions; a saved routing preference is not proof that a login is current.

Use `op: site` over the same existing `monitor-encoded` transport:

- `list` returns up to 200 saved exact hosts and context labels, scoped to your
  resources. These are in the existing settings table, with no polling/mirroring.
- `read`, with `url` and optional `context` (default `default`), returns the current
  revision or revision 0 for a new host/context. Paths, queries and fragments are
  not saved in this routing key. Exact hosts are used, not guessed parent domains.
- `remember` requires `url`, `workspace`, `revision`, `note`, and optional context.
  Record the existing profile after checking the correct website/account/session.
  No new profile is created. A deliberate binding correction needs `rebind: true`,
  a fresh revision and an observed reason. It cannot relocate an unresolved
  challenge. Do not automatically switch identities/profiles after a failure.
- `check`, with `url`, `workspace`, optional context, refuses a wrong saved
  workspace or unresolved challenge. Known navigation and writes are guarded in
  the existing remote handler/API. Snapshot/tab selection and recovery remain
  available; cancel upload, Escape and dialog-dismiss still work. A parked form
  cannot be replaced by navigating to unrelated work; create/select another tab.
  The usual
  legacy route reuses the already-read selected-tab URL, without an extra browser
  RPC. Only an unavailable/ambiguous inventory requires a bounded read-only probe.
- `resolve` requires `url`, fresh site `revision`, the exact `challengeId` and
  `resolution`. Call only after a person completes the challenge and the same
  intended form/session is inspected. There is no automatic cooldown expiry or
  challenge-solving service. A resolution never resumes a human browser pause.

For browser tasks, set packet `execution: browser`, `targetURL`, `workspace`, and
optional `siteContext`. Use the host/context's saved profile throughout. The older `run TOKEN` browser route controls Main only. If memory selects
Secondary, use the EXISTING workspace-aware worker API through monitor-encoded,
with `workspace: scholarships`; do not sign in again in Main as a fallback. Get
exact page/document identities from monitor status and preserve its existing tabs.
For example, claim with `{op: claim, workspace: scholarships, task: TASK_ID}`, then
send `{op: browser, workspace: scholarships, token: RETURNED_LEASE, generation:
RETURNED_GENERATION, requestId: UNIQUE_ID, action: {kind: snapshot, page: PAGE_ID,
document: CURRENT_DOCUMENT_GENERATION}}`, and release the same lease afterward.
These are JSON object shapes; use quoted JSON keys/strings in actual requests.
Human pauses and the existing role permissions still apply.

Shared identity-provider hosts with genuinely different accounts need distinct context
labels; the legacy browser route guards the default context. The workspace-aware
worker API can pass `siteContext` for deliberate separate contexts. Do not guess
an identity, synchronize cookies or rebind to evade a challenge.

When the site requires a CAPTCHA, task `block` accepts `category: captcha` and
`siteRevision` from the briefing/read. The packet must identify the target site
and workspace. The task and site blocker are saved atomically. Related browser
work becomes unavailable, while unrelated sites and connector/offline preparation
remain available. The first report returns `notificationRecommended: true`;
unchanged reports return false and do not write another heartbeat. Use only an
already authorized notification route. Saved/recommended is not delivered: never
claim a notification was sent without its actual result. Inspect on an uncertain
send before retrying. Summarize unchanged blockers without repeatedly pushing the
same alert; preserve separately authorized urgent/deadline escalation rules.

After the user has released the human pause, a recovery snapshot can use the
existing browser claim without associating the blocked task ID. Read/snapshot and
recovery remain allowed, while site writes stay gated. Inspect the preserved tab,
resolve it explicitly, release that inspection lease, then claim the canonical
item/browser task normally. This is not permission to clear a human pause.

After the human responds, inspect the preserved page, then task `unblock` with
`challengeId` and `resolution` clears its matching site challenge atomically. If
other packet questions/files remain unresolved the whole operation fails without
clearing the site gate. Re-read/reprepare tasks that were created as drafts while
waiting. Keep existing checkpoints and never repeat an uncertain submission.
A CAPTCHA iframe's presence alone does not prove that a human challenge is active;
report actual visible/observed blocking requirements, not every embedded widget.

## Connector-first preparation

`{"op":"task","action":"plan","kind":"research"}` suggests a connector-first
route. Kinds: research, records, documents, website, offline. It does not call or
claim access to another chat's tools, schedule work or authorize an external action.
Verify the worker's actual connected-tool permissions. Use official/authorized
search and source tools for research, existing writers for records, and
`admit`/document tools for file preparation. Use Chrome only for a step that needs
the website. Packet `execution` is browser / connector / offline. Connector or
offline tasks do not require a browser lease; a task-bound browser claim rejects
such preparation work. For an application, prepare its notes/files before claiming
Chrome; change the packet's route only when its actual phase requires it.

`work-brief` is the normal single startup read. Read the full guide once per
controller revision; reuse successful responses and returned revisions. Keep
browser steps sequential, reusing the current page until the website genuinely
changes. Do not add fixed waits to imitate a human; honor actual page readiness,
site rate limits and human challenges. No new scheduler or parallel browser actor.

## Timing that separates the delays

Small metadata stays inside the existing rotating 3 MiB diagnostic logs. API
browser operations record guest adapter/RPC, browser-operation and navigation
wait durations. The legacy route records daemon connect/RPC timings. No form
text, cookies, URLs or credentials are added to diagnostic logs; internal timing
fields are removed before normal page results are returned. No extra DOM probe
or background sample is added to each action.

On a bounded diagnostic run, call `work-brief` with `measure: true` in JSON. The
response's `timing` separates controller time and guest Unix API round trip; it
EXCLUDES Main's cloud proxy, SSH, helper startup and tool time. Main can measure
its own total call wall time, then submit `op: timing-sample` with `workerTotalMs`
and the returned `guestRoundTripMs`. Diagnostics labels these as worker-reported;
the difference is unattributed outside-guest overhead, not pure network latency.
Use a few samples when diagnosing drift, not a probe on every form command.

For a specific slow page, `op: performance`, `workspace`, exact `page` and
`pageGeneration` reads last-document navigation timings (response wait, download,
DOM ready, load). These concern that document's navigation, not each SPA step.
Read-only probes do not reload or navigate the page. `monitor diagnostics` groups
available timing layers; missing layers stay unknown. Local timing improvement
is not evidence of a faster cloud connection. The held replacement helper stays
on hold and is not required for any operation above.
