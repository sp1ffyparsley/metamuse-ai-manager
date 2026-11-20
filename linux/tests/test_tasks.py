import copy
import hashlib
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

import test_authority
from muse_monitor.store import Store, Conflict, Denied
from muse_monitor.tasks import Tasks, KEY
from muse_monitor.task_view import render, confirm
from muse_monitor import task_view


class TaskTests(unittest.TestCase):
    setUp = test_authority.AuthorityTests.setUp
    tearDown = test_authority.AuthorityTests.tearDown

    def packet(self, **changes):
        return {'title': 'Compare community garden options', 'topic': 'Gardening / local research',
                'goal': 'Prepare a sourced recommendation', 'workspace': 'jobs',
                'sources': ['https://example.com/gardens'], 'draft': 'Prepared notes.',
                'nextAction': 'Review the report',
                'successCriteria': [{'id': 'report', 'description': 'Report PDF exists', 'evidence': 'file'}], **changes}

    def api(self, action, actor=None, **body):
        return self.m.worker(actor or self.main, {'op': 'task', 'action': action, **body})

    def save(self, item='garden-1', **changes):
        return self.api('save', id=item, revision=0, packet=self.packet(**changes))

    def file(self, name='Report.pdf', content=b'%PDF-1.4\nDiagnostic report\n%%EOF'):
        folder = Path(self.tmp.name) / 'artifacts'
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(content)
        return {'name': name, 'sha256': hashlib.sha256(content).hexdigest()}

    def test_generic_packet_reuses_item_journal_without_browser_or_new_tables(self):
        tables = [r[0] for r in self.m.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        task = self.save()
        self.assertEqual(task['state'], 'ready')
        self.assertEqual(task['packet']['topic'], 'Gardening / local research')
        self.assertEqual(self.m.browser.calls, [])
        self.assertEqual(tables, [r[0] for r in self.m.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")])
        self.assertEqual(self.m.worker(self.main, {'op': 'next-work'})[0]['id'], 'garden-1')
        caps = self.m.worker(self.main, {'op': 'capabilities'})
        self.assertIn('task', caps['ops'])

    def test_missing_and_changed_files_block_readiness_and_claim(self):
        f = {'name': 'Missing.pdf', 'sha256': '0' * 64}
        task = self.save(files=[f])
        self.assertEqual(task['state'], 'draft')
        with self.assertRaises(Conflict):
            self.m.worker(self.main, {'op': 'claim-item', 'item': task['id']})
        f = self.file()
        task = self.save('garden-2', files=[f])
        self.assertEqual(task['state'], 'ready')
        self.file(content=b'%PDF-1.4\nChanged report\n%%EOF')
        self.assertTrue(self.api('read', id=task['id'])['readinessProblems'])
        with self.assertRaises(Conflict):
            self.m.worker(self.main, {'op': 'claim-item', 'item': task['id']})

    def test_needs_you_releases_item_not_browser_or_human_pause(self):
        task = self.save()
        self.m.worker(self.main, {'op': 'claim-item', 'item': task['id'], 'revision': task['revision']})
        self.m.store.pause('jobs', 1)
        task = self.api('block', id=task['id'], revision=task['revision'] + 1,
                        reason='Human verification required', question='Please complete the site challenge.',
                        nextAction='Check the saved page and continue', url='https://example.com/challenge')
        self.assertEqual(task['state'], 'needs-you')
        row = self.m.store.db.execute('SELECT * FROM item WHERE id=?', (task['id'],)).fetchone()
        self.assertIsNone(row['owner'])
        self.assertEqual(self.api('list', state='needs-you')['tasks'][0]['id'], task['id'])
        self.assertEqual(self.m.worker(self.main, {'op': 'next-work'}), [])
        task = self.api('unblock', id=task['id'], revision=task['revision'], resolution='Challenge completed and site page checked')
        self.assertEqual(task['state'], 'ready')
        self.assertTrue(self.m.store.workspace('jobs')['paused'])
        self.assertEqual(self.m.browser.calls, [])

    def test_questions_require_packet_update_then_explicit_resolution(self):
        task = self.save(questions=['Which garden is approved?'])
        with self.assertRaises(Conflict):
            self.api('unblock', id=task['id'], revision=task['revision'], resolution='Answered')
        task = self.api('save', id=task['id'], revision=task['revision'], packet=self.packet(questions=[]))
        self.assertEqual(task['state'], 'needs-you')
        task = self.api('unblock', id=task['id'], revision=task['revision'], resolution='Approved source and answer recorded')
        self.assertEqual(task['state'], 'ready')

    def test_completion_requires_all_actual_evidence_and_survives_restart(self):
        task = self.save()
        with self.assertRaises(Conflict):
            self.api('finish', id=task['id'], revision=task['revision'], summary='Clicked save')
        f = self.file()
        task = self.api('receipt', id=task['id'], revision=task['revision'], criterion='report', **f)
        self.assertIn('delivery or content quality is not implied', task['receipts']['report']['scope'])
        task = self.api('finish', id=task['id'], revision=task['revision'], summary='Local report file verified')
        self.assertEqual(task['state'], 'done')
        self.assertEqual(self.m.worker(self.main, {'op': 'next-work'}), [])
        for op in ('save', 'block', 'finish'):
            with self.assertRaises(Conflict):
                self.api(op, id=task['id'], revision=task['revision'], packet=self.packet(), summary='Replay')
        with self.assertRaises(Conflict):
            self.m.worker(self.main, {'op': 'claim-item', 'item': task['id']})
        self.m.store.db.close()
        self.m.store = Store(Path(self.tmp.name) / 'journal.sqlite3')
        self.m.tasks = Tasks(self.m)
        self.assertEqual(self.api('read', id=task['id'])['receipts'], task['receipts'])

    def test_packet_revision_invalidates_receipts_and_rejects_stale_writes(self):
        task = self.save()
        old = task['revision']
        task = self.api('receipt', id=task['id'], revision=old, criterion='report', **self.file())
        with self.assertRaises(Conflict):
            self.api('finish', id=task['id'], revision=old, summary='Stale')
        task = self.api('save', id=task['id'], revision=task['revision'], packet=self.packet(goal='Prepare a different report'))
        self.assertEqual(task['receipts'], {})

    def test_file_evidence_rechecks_at_finish_and_refuses_paths_links_and_wrong_hash(self):
        task = self.save()
        f = self.file()
        for bad in ({'name': '../Report.pdf', 'sha256': f['sha256']}, {'name': f['name'], 'sha256': '0' * 64}):
            with self.assertRaises(Conflict):
                self.api('receipt', id=task['id'], revision=task['revision'], criterion='report', **bad)
        link = Path(self.tmp.name) / 'artifacts' / 'Link.pdf'
        link.symlink_to(Path(self.tmp.name) / 'artifacts' / f['name'])
        with self.assertRaises(Conflict):
            self.api('receipt', id=task['id'], revision=task['revision'], criterion='report', name='Link.pdf', sha256=f['sha256'])
        task = self.api('receipt', id=task['id'], revision=task['revision'], criterion='report', **f)
        self.file(content=b'%PDF-1.4\nReplaced after receipt\n%%EOF')
        with self.assertRaises(Conflict):
            self.api('finish', id=task['id'], revision=task['revision'], summary='Do not accept changed file')

    def test_human_confirmation_cannot_be_forged_by_main(self):
        task = self.save(successCriteria=[{'id': 'call', 'description': 'Human confirms phone appointment', 'evidence': 'human'}])
        with self.assertRaises(Denied):
            self.api('receipt', id=task['id'], revision=task['revision'], criterion='call', confirmation='Confirmed', actor=self.main)
        task = self.api('receipt', id=task['id'], revision=task['revision'], criterion='call', confirmation='I confirmed the appointment by phone', actor=self.human)
        self.assertEqual(task['receipts']['call']['checkedBy'], 'human')

    def test_page_evidence_requires_exact_url_document_and_matched_text(self):
        task = self.save(successCriteria=[{'id': 'sent', 'description': 'Portal confirmation', 'evidence': 'page'}])
        body = {'id': task['id'], 'revision': task['revision'], 'criterion': 'sent', 'page': 'p1', 'pageGeneration': 'doc1',
                'url': 'https://example.com/confirmation', 'text': 'Your request was received'}
        with self.assertRaises(Conflict):
            self.api('receipt', **body)
        self.m.browser.action = lambda wid, request: {'matched': True, 'page': {'id': 'p1', 'generation': 'wrong', 'url': body['url']}}
        with self.assertRaises(Conflict):
            self.api('receipt', **body)
        self.m.browser.action = lambda wid, request: {'matched': True, 'page': {'id': 'p1', 'generation': 'doc1', 'url': body['url']}}
        task = self.api('receipt', **body)
        self.assertEqual(task['receipts']['sent']['text'], body['text'])

    def test_permissions_scopes_credentials_and_reserved_capture_cannot_bypass(self):
        task = self.save()
        alt = self.m.principal(self.tokens['alt'])
        with self.assertRaises(Denied):
            self.api('list', actor=alt)
        human = dict(self.human, workspaces=['research'])
        self.assertEqual(self.api('list', actor=human)['tasks'], [])
        self.assertEqual(self.m.worker(human, {'op': 'next-work'}), [])
        with self.assertRaises(Denied):
            self.api('read', id=task['id'], actor=human)
        with self.assertRaises(Conflict):
            self.save('bad', draft='large', password='not allowed')
        with self.assertRaises(Denied):
            self.m.worker(self.main, {'op': 'capture', 'item': {'url': 'https://example.com', KEY: {'state': 'done'}}})

    def test_concurrent_revisions_have_one_winner_and_no_lost_update(self):
        task = self.save()
        barrier = threading.Barrier(2)
        results = []
        def write(question):
            barrier.wait()
            try:
                self.api('block', id=task['id'], revision=task['revision'], reason='Need decision', question=question, nextAction='Continue after answer')
                results.append('saved')
            except Conflict:
                results.append('stale')
        threads = [threading.Thread(target=write, args=(x,)) for x in ('Option A?', 'Option B?')]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertEqual(sorted(results), ['saved', 'stale'])

    def test_bounded_pagination_and_native_report(self):
        for i in range(51):self.save('topic-' + str(i))
        first = self.api('list')
        second = self.api('list', cursor=first['nextCursor'])
        self.assertEqual(len(first['tasks']), 50)
        self.assertEqual(len(second['tasks']), 1)
        self.assertIsNone(second['nextCursor'])
        self.assertIn('Gardening / local research', render(first['tasks']))
        self.assertEqual(self.m.browser.calls, [])

    def test_attended_confirmation_cancel_cannot_write_and_requires_three_choices(self):
        task = self.save(successCriteria=[{'id': 'call', 'description': 'Phone appointment confirmed', 'evidence': 'human'}])
        rows = self.api('list')['tasks']
        with patch('muse_monitor.task_view.subprocess.run', return_value=CompletedProcess([], 1, '')), patch('muse_monitor.task_view.request') as api:
            confirm('unused', rows)
            api.assert_not_called()
        replies = [CompletedProcess([], 0, task['id'] + '\n'), CompletedProcess([], 0, 'call\n'), CompletedProcess([], 0, 'I confirmed it by phone\n')]
        with patch('muse_monitor.task_view.subprocess.run', side_effect=replies), patch('muse_monitor.task_view.request', side_effect=[task, {}]) as api:
            confirm('unused', rows)
            self.assertEqual(api.call_args.kwargs['principal'], 'windows-human')
            self.assertEqual(api.call_args.args[1]['confirmation'], 'I confirmed it by phone')
            self.assertEqual(api.call_count, 2)

    def test_existing_guide_and_aliases_are_discoverable_without_helper_install(self):
        from test_repairs import legacy
        self.assertIn('any topic', self.api('guide')['instructions'])
        self.assertEqual(legacy.preparation_request('tasks-help', []), {'op': 'task', 'action': 'guide'})
        self.assertEqual(legacy.preparation_request('needs-you', []), {'op': 'task', 'action': 'list', 'state': 'needs-you'})

    def test_packet_fits_existing_encoded_transport_and_large_material_must_be_linked(self):
        import base64
        from test_repairs import legacy
        packet = self.packet(draft='x' * 6000)
        body = {'op': 'task', 'action': 'save', 'id': 'transport-bound', 'revision': 0, 'packet': packet}
        encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
        self.assertEqual(legacy.decode_json(encoded), body)
        self.api('save', **{k: v for k, v in body.items() if k not in ('op', 'action')})
        with self.assertRaises(Conflict):
            self.save('too-large', draft='x' * 6000, sources=['https://example.com/' + 'a' * 3000])

    def test_claim_rejects_a_different_packet_revision_without_an_extra_read(self):
        task = self.save()
        updated = self.api('save', id=task['id'], revision=task['revision'], packet=self.packet(goal='Changed task goal'))
        for revision in (None, task['revision']):
            with self.assertRaises(Conflict):
                self.m.worker(self.main, {'op': 'claim-item', 'item': task['id'], 'revision': revision})
        claim = self.m.worker(self.main, {'op': 'claim-item', 'item': task['id'], 'revision': updated['revision']})
        self.assertEqual(claim['revision'], updated['revision'] + 1)

    def test_zenity_extra_button_with_exit_one_still_enters_attended_confirmation(self):
        self.save()
        rows = self.m.worker(self.main,{'op':'work-brief'})
        replies = [CompletedProcess([], 1, 'Confirm a result\n'), CompletedProcess([], 1, '')]
        with patch('sys.argv', ['task-view']), patch.object(task_view, 'request', return_value=rows), patch.object(task_view.subprocess, 'run', side_effect=replies), patch.object(task_view, 'confirm') as attended:
            task_view.main()
            attended.assert_called_once()

    def test_attended_resume_can_cover_both_resources_but_main_cannot_resume(self):
        for wid in ('jobs', 'scholarships'):
            self.m.store.pause(wid, 1)
        with self.assertRaises(Denied):
            self.m.worker(self.main, {'op': 'local-resume', 'workspace': 'jobs'})
        local = {'id': 'local-human', 'role': 'human', 'workspaces': ['jobs', 'scholarships']}
        for wid in local['workspaces']:
            self.m.worker(local, {'op': 'local-resume', 'workspace': wid})
            self.assertFalse(self.m.store.workspace(wid)['paused'])


if __name__ == '__main__':
    unittest.main()
