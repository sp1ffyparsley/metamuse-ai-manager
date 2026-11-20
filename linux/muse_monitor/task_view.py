"""On-demand native task list. No polling, browser tab, or additional daemon."""
import argparse
import json
import subprocess
from pathlib import Path

from .cli import request


def confirm(config, tasks):
    """Only an attended native dialog can issue a human attestation here."""
    candidates = [t for t in tasks if t['state'] != 'done']
    if not candidates:
        return
    choice = subprocess.run(['zenity', '--list', '--title=Confirm a result', '--text=Choose work you personally checked',
                             '--column=Task ID', '--column=Task', '--print-column=1', '--width=760', '--height=420', '--',
                             *[v for t in candidates for v in (t['id'], t['title'])]], capture_output=True, text=True, check=False)
    if choice.returncode or choice.stdout.strip() not in {t['id'] for t in candidates}:
        return
    item = choice.stdout.strip()
    data = request(config, {'op': 'task', 'action': 'read', 'id': item}, principal='windows-human')
    criteria = [c for c in data['packet']['successCriteria'] if c['evidence'] == 'human' and c['id'] not in data['receipts']]
    if not criteria:
        subprocess.run(['zenity', '--info', '--text=This task has no outstanding result that requires your personal confirmation.'], check=False)
        return
    choice = subprocess.run(['zenity', '--list', '--title=Confirm a result', '--text=Which result did you personally verify?',
                             '--column=Result ID', '--column=Result', '--print-column=1', '--width=760', '--height=420', '--',
                             *[v for c in criteria for v in (c['id'], c['description'])]], capture_output=True, text=True, check=False)
    if choice.returncode or choice.stdout.strip() not in {c['id'] for c in criteria}:
        return
    criterion = choice.stdout.strip()
    answer = subprocess.run(['zenity', '--entry', '--title=Record your confirmation',
                             '--text=Describe the result you personally checked. Do not enter passwords or verification codes.'],
                            capture_output=True, text=True, check=False)
    if answer.returncode or not answer.stdout.strip():
        return
    request(config, {'op': 'task', 'action': 'receipt', 'id': item, 'revision': data['revision'],
                     'criterion': criterion, 'confirmation': answer.stdout.strip()}, principal='windows-human')


def render(tasks, truncated=False, sites=None, workspace_names=None):
    lines = ['MUSE TASKS', 'Ready packets, Needs you, and verified completion receipts.',
             'This view does not pause, resume, or control Chrome.',
             'Needs you: complete the stated action, then tell Muse what changed.',
             'A checked file means bytes exist; a page receipt means the stated text was observed.', '']
    if sites:
        lines.extend(['WEBSITE PROFILES', '-'*45])
        for site in sites:
            name=(workspace_names or {}).get(site['workspace'],site['workspace'])
            lines.append(site['host']+' → '+name+' ['+site['context']+']')
            if site.get('challenge'):
                lines.extend(['Waiting for you: '+site['challenge']['question'],'Next: '+site['challenge']['nextAction']])
        lines.extend(['Saved routing is separate from whether a website is currently signed in.',''])
    for state, label in (('needs-you', 'NEEDS YOU'), ('ready', 'READY'), ('draft', 'STILL PREPARING'), ('done', 'DONE — WITH RECEIPTS')):
        rows = [t for t in tasks if t['state'] == state]
        lines.extend([label + ' (' + str(len(rows)) + ')', '-' * 45])
        if not rows:
            lines.append('Nothing here.')
        for t in rows:
            lines.extend([t['title'] + ' [' + t['topic'] + ']', 'Task: ' + t['id'], 'Next: ' + t['nextAction']])
            if t.get('waitingOnSite'):lines.append('Waiting on website: '+t['waitingOnSite'])
            blocker = t.get('blocker')
            if blocker:
                lines.extend(['Why: ' + blocker['reason'], 'Question/action for you: ' + blocker['question'], 'Continue with: ' + blocker['nextAction']])
                if blocker.get('url'):
                    lines.append('Page: ' + blocker['url'])
            lines.extend(['Checked criteria: ' + str(t['criteriaMet']) + '/' + str(t['criteriaTotal']), ''])
            if t.get('completionSummary'):
                lines.append('Result: ' + t['completionSummary'])
            for criterion, proof in t.get('receipts', {}).items():
                label = proof.get('url') or proof.get('name') or proof.get('confirmation', '')
                lines.append('Receipt [' + criterion + '/' + proof['method'] + ']: ' + label)
                if proof.get('text'):
                    lines.append('Observed: ' + proof['text'])
                if proof.get('sha256'):
                    lines.append('Exact local file version checked.')
            lines.append('')
        lines.append('')
    if truncated:
        lines.append('View limited to 200 records. Main can read further pages through the task API.')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(Path.home() / '.config/muse-monitor/config.json'))
    parser.add_argument('--text', action='store_true')
    args = parser.parse_args()
    while True:
        tasks, cursor, sites, names = [], 0, [], {}
        while len(tasks) < 200:
            if cursor==0:
                brief=request(args.config,{'op':'work-brief'},principal='windows-human')
                result=brief['tasks'];sites=brief['sites']['sites'];names=brief['workspaceNames']
            else:result = request(args.config, {'op': 'task', 'action': 'list', 'cursor': cursor}, principal='windows-human')
            tasks.extend(result['tasks'])
            cursor = result['nextCursor']
            if cursor is None:
                break
        report = render(tasks, cursor is not None, sites, names)
        if args.text:
            print(report)
            return
        result = subprocess.run(['zenity', '--text-info', '--title=Muse Tasks', '--width=820', '--height=640',
                                 '--ok-label=Refresh', '--cancel-label=Close', '--extra-button=Confirm a result'], input=report, capture_output=True, text=True, check=False)
        if result.stdout.strip() == 'Confirm a result':
            confirm(args.config, tasks)
        elif result.returncode != 0:
            return


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError):
        subprocess.run(['zenity', '--error', '--text=Could not read Muse Tasks. Existing work is preserved; inspect Muse Monitor.'], check=False)
        raise SystemExit(1)
