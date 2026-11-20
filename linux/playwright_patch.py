#!/usr/bin/python3
"""Apply a narrow, reversible guard to the pinned Playwright CLI core bundle.

Upstream report: https://github.com/microsoft/playwright-cli/issues/489
Run again after an intentional CLI upgrade only if the exact source still matches.
"""
from pathlib import Path
import os

BUNDLE = Path('/opt/muse/node/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/coreBundle.js')
OLD = '''            title = await this.page.title();'''
NEW = '''            // Muse local guard: one background tab must not block every CLI command.
            const alreadyPending = !!this._musePendingTitle;
            if (!this._musePendingTitle) {
              this._musePendingTitle = this.page.title().catch(() => "").finally(() => {
                this._musePendingTitle = void 0;
              });
            }
            let titleTimer;
            try {
              title = await Promise.race([
                this._musePendingTitle,
                new Promise(resolve => { titleTimer = setTimeout(() => resolve(""), alreadyPending ? 100 : 1000); })
              ]);
            } finally {
              clearTimeout(titleTimer);
            }'''
SNAPSHOT_OLD = 'root ? await root.ariaSnapshot({ mode: "ai", depth, boxes }) : await this.page.ariaSnapshot({ mode: "ai", depth, boxes })'
SNAPSHOT_NEW = 'root ? await root.ariaSnapshot({ mode: "ai", depth, boxes, timeout: this.context.config.timeouts?.action ?? 10000 }) : await this.page.ariaSnapshot({ mode: "ai", depth, boxes, timeout: this.context.config.timeouts?.action ?? 10000 })'
JSON_OLD = 'root ? await root.ariaSnapshotJSON({ mode: "ai", depth, boxes }) : await this.page.ariaSnapshotJSON({ mode: "ai", depth, boxes })'
JSON_NEW = 'root ? await root.ariaSnapshotJSON({ mode: "ai", depth, boxes, timeout: this.context.config.timeouts?.action ?? 10000 }) : await this.page.ariaSnapshotJSON({ mode: "ai", depth, boxes, timeout: this.context.config.timeouts?.action ?? 10000 })'
UPLOAD_OLD = 'files: stringArrayArg.describe("The absolute paths to the files to upload")'
UPLOAD_NEW = 'files: stringArrayArg.optional().describe("The absolute paths to the files to upload; omit to cancel the chooser")'

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with the maintenance administrator.')
    content = BUNDLE.read_text()
    changed = content
    for old,new in [(OLD,NEW),(SNAPSHOT_OLD,SNAPSHOT_NEW),(JSON_OLD,JSON_NEW),(UPLOAD_OLD,UPLOAD_NEW)]:
        if new in changed:
            continue
        if changed.count(old) != 1:
            raise SystemExit('Pinned source changed; refusing an ambiguous library edit.')
        changed = changed.replace(old,new,1)
    if changed == content:
        print('Title/snapshot bounds and optional file-chooser cancellation already installed.')
        return
    backup = BUNDLE.with_suffix('.js.before-muse-title-timeout')
    if not backup.exists():
        backup.write_bytes(BUNDLE.read_bytes())
        backup.chmod(0o644)
    BUNDLE.write_text(changed)
    print('Installed title/snapshot bounds and optional chooser cancellation for new daemons. The handler compatibility path cancels choosers in existing daemons without restarting Chrome.')

if __name__ == '__main__':
    main()
