Run `extension_live_acceptance.py` only as the enrolled muse-main maintenance user,
with Jobs explicitly paused and the installed extension's popup.html already
open in a tab. It reloads only the extension, creates disposable localhost tabs,
tests actual native messaging, protected grouping, rule synchronization,
collections import/export, capture and duplicate suggestions, then restores
rules/collections and removes its own fixtures. It never navigates or closes
pre-existing portal tabs. It intentionally exercises the actual installed Chrome,
not an alternate browser pretending to be the user's managed Chrome.

`live_acceptance.py` defaults to the two original new workspaces. With explicit
user authorization to close/save browser work, set
`MUSE_MONITOR_TEST_WORKSPACES=jobs,scholarships` for the two-profile runtime. It must not run
against workspaces holding unrelated tasks. It refuses to automatically clear a
human pause except when recovering its own controlled fixture.
