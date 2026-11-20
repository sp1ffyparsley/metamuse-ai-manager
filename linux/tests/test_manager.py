import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from muse_monitor import manager_worker
from muse_monitor.manager import ACTOR
from muse_monitor.research import extract, source_url
from muse_monitor.server import Monitor
from muse_monitor.store import Conflict, Denied

HUMAN = {'id': 'windows-human', 'role': 'human', 'workspaces': []}
WORKER = {'id': ACTOR, 'role': 'manager-worker', 'workspaces': []}
CRITERION = 'Cite the exact verified source.'
SOURCE = 'https://api.github.com/repos/NousResearch/hermes-agent/releases/latest'


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = Monitor({'state': self.temp.name, 'workspaces': {}, 'principals': {}})
        self.addCleanup(self.monitor.store.db.close)
        self.manager = self.monitor.manager
        self.manager.worker(WORKER, {'action': 'provider-ready', 'model': 'gpt-6-sol',
                                     'elapsedMs': 150, 'providerAccountId': 'test-identity'})
        self.manager.research.get = lambda url: {'url': url, 'availability': 'available', 'sha256': 'a' * 64,
                                                 'excerpt': 'Example bounded public source', 'observedAt': time.time()}

    def create(self, name, goal='creative', **changes):
        return self.manager.operator(HUMAN, {'action': 'create', 'requestId': name, 'goal': goal,
                                             'capability': 'research', 'objective': 'Inspect a release for editing tools.',
                                             'criteria': [CRITERION], 'sources': [SOURCE], **changes})

    def claim(self):
        return self.manager.worker(WORKER, {'action': 'claim'})['task']

    def submit(self, lease, **result):
        return self.manager.worker(WORKER, {'action': 'result', **lease, 'result': result})

    def test_rank_revision_fencing_and_quality_correction(self):
        first = self.create('fixture-a', 'jobs')
        second = self.create('fixture-b')
        self.assertEqual(second, {k: v for k, v in self.create('fixture-b').items() if k != 'leaseUntil'})
        with self.assertRaises(Conflict):
            self.create('fixture-c', canonicalItemId=first['canonicalItemId'])
        plan = self.claim()
        self.assertEqual(plan['taskId'], second['taskId'])
        self.assertIsNone(self.claim())
        forged = dict(plan, run={**plan['run'], 'fencingId': 'forged'})
        with self.assertRaises(Denied):
            self.submit(forged, approach='Use the source', checks=[])
        self.submit(plan, approach='Use the source', checks=[CRITERION])
        with self.assertRaises(Conflict):
            self.submit(plan, approach='Replay', checks=[])
        work = self.claim()
        self.assertEqual(work['run']['phase'], 'work')
        with self.assertRaises(Conflict):
            self.submit(work, summary='Weak output', citations=['https://example.com/invented'])
        self.submit(work, summary='Weak uncited output', citations=[])
        review = self.claim()
        value = self.submit(review, verdict='accept', feedback='Looks fine', criteriaMet=[CRITERION])
        self.assertEqual(value['state'], 'working')
        correction = self.claim()
        self.assertEqual(correction['corrections'], 1)
        self.assertIn('Add at least one exact citation', correction['review']['feedback'])
        self.submit(correction, summary='Corrected output with exact evidence', citations=[SOURCE])
        reviewed = self.submit(self.claim(), verdict='accept', feedback='Exact collected content is cited.', criteriaMet=[CRITERION])
        self.assertEqual(reviewed['state'], 'done')
        stored = self.manager.operator(HUMAN, {'action': 'read', 'taskId': second['taskId']})
        self.assertEqual(stored['result']['citations'], [{'url': SOURCE, 'sha256': 'a' * 64}])
        self.assertEqual([], self.monitor.store.next_work(HUMAN))
        self.assertEqual(5, stored['calls'])

    def test_expired_lease_is_unknown_after_restart_and_cannot_replay(self):
        queued = self.create('fixture-expiry')
        lease = self.claim()
        with self.monitor.store.tx() as db:
            db.execute('UPDATE item SET until=? WHERE id=?', (time.time() - 1, queued['taskId']))
        restarted = Monitor({'state': self.temp.name, 'workspaces': {}, 'principals': {}})
        self.addCleanup(restarted.store.db.close)
        self.assertIsNone(restarted.manager.worker(WORKER, {'action': 'claim'})['task'])
        unknown = restarted.manager.operator(HUMAN, {'action': 'read', 'taskId': queued['taskId']})
        self.assertEqual(unknown['state'], 'unknown')
        with self.assertRaises(Conflict):
            restarted.manager.worker(WORKER, {'action': 'result', **lease, 'result': {'approach': 'old'}})
        self.assertEqual('planning', restarted.manager.operator(HUMAN, {'action': 'retry', 'taskId': queued['taskId'],
                                                                       'revision': unknown['revision']})['state'])

    def test_connections_and_manager_require_exact_roles(self):
        with self.assertRaises(Denied):
            self.manager.operator({'id': 'extension-jobs', 'role': 'human'}, None)
        with self.assertRaises(Denied):
            self.manager.worker({'id': ACTOR, 'role': 'main'}, {'action': 'claim'})
        with self.assertRaises(Denied):
            self.monitor.connections.list(WORKER)
        result = self.monitor.connections.list(HUMAN)
        entries = {row['id']: row for row in result['entries']}
        self.assertEqual(entries['hermes']['status'], 'Operational')
        self.assertEqual(entries['crosswalk-main']['status'], 'Needs human consent')
        self.assertEqual(entries['google']['status'], 'Not configured')
        self.assertFalse(entries['hermes']['stale'])

    def test_goal_write_is_revisioned(self):
        body = {'action': 'goal', 'goal': 'creative', 'weight': 0, 'enabled': False, 'revision': 0, 'constraints': ['No outreach']}
        current = self.manager.operator(HUMAN, body)
        self.assertFalse(current['enabled'])
        with self.assertRaises(Conflict):
            self.manager.operator(HUMAN, body)
        self.create('fixture-disabled')
        self.assertIsNone(self.claim())

    def test_expired_provider_is_verified_only_with_eligible_work_and_backs_off(self):
        proof = self.monitor.connections.saved('hermes')
        proof['verifiedAt'] = time.time() - 86401
        with self.monitor.store.tx() as db:
            db.execute('UPDATE setting SET data=? WHERE id=?', (json.dumps(proof), 'connection:hermes'))
        self.assertNotIn('probeRequired', self.manager.claim(WORKER))
        self.create('fixture-expired-provider')
        value = self.manager.claim(WORKER)
        self.assertEqual((None, True), (value['task'], value['probeRequired']))
        self.manager.worker(WORKER, {'action': 'provider-unavailable'})
        value = self.manager.claim(WORKER)
        self.assertNotIn('probeRequired', value)
        self.assertIn('backing off', value['reason'])

    def test_adapter_refreshes_then_advances_with_one_claim_retry(self):
        calls = []
        proofs = [{'task': None, 'probeRequired': True}, {'task': None}]
        receipt = {'model': 'gpt-6-sol', 'providerAccountId': 'fixture', 'elapsedMs': 200, 'result': True}

        def exchange(settings, body):
            calls.append(body['action'])
            return proofs.pop(0) if body['action'] == 'claim' else {}

        with patch.object(manager_worker, 'exchange', side_effect=exchange), patch.object(manager_worker, 'sandbox', return_value=receipt) as model:
            self.assertEqual(0, manager_worker.run({'socket': 'fixture'}, drain=True))
        self.assertEqual(['claim', 'provider-ready', 'claim'], calls)
        model.assert_called_once_with({'socket': 'fixture'}, {'phase': 'probe', 'forceRefresh': False})

    def test_relay_operator_saves_only_verified_private_target_and_fences_edits(self):
        folder = 'test-approved-folder'
        consumer = 'test-consumer-id'
        self.monitor.connections.record('google', 'Tested read', 'Fixture only')
        grants = {'permissions': [{'type': 'anyone'}]}

        class Drive:
            def call(self, service, path):
                if '/permissions?' in path:
                    return grants
                return {'id': folder, 'mimeType': 'application/vnd.google-apps.folder', 'capabilities': {'canAddChildren': True}}

        self.monitor.google = Drive()
        request = {'action': 'relay-configure', 'folderId': folder, 'consumerTaskId': consumer, 'revision': 0}
        with self.assertRaises(Denied):
            self.manager.operator(HUMAN, request)
        self.assertFalse(self.manager.dashboard()['relayTarget']['configured'])
        grants['permissions'] = [{'type': 'user'}]
        saved = self.manager.operator(HUMAN, request)
        self.assertEqual({'folderId': folder, 'consumerTaskId': consumer, 'revision': 1, 'configured': True}, saved)
        self.assertEqual(saved, self.manager.dashboard()['relayTarget'])
        with self.assertRaises(Conflict):
            self.manager.operator(HUMAN, request)

    def test_task_grant_sends_review_after_hermes_accepts_and_holds_if_identity_changes(self):
        board = self.manager.crosswalk
        owner, main, replacement = '1111-2222-3333-4444', '2222-3333-4444-5555', '3333-4444-5555-6666'
        board.settings.update(board='fixture-board', number=owner, workers={'main': main})

        def verify(number):
            self.monitor.connections.record('crosswalk-main', 'Tested write', 'Fixture only',
                                            providerAccountId=number, board='fixture-board', sender=owner)

        posts = []

        def call(tool, args):
            if tool == 'crosswalks':
                return [{'slug': 'fixture-board', 'private': True}]
            if tool == 'account' and args['action'] == 'numbers':
                return {'you': owner}
            if tool == 'account' and args['action'] == 'rules':
                return {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
            if tool == 'post':
                posts.append(args)
                return {'id': 'automatic-post'}
            raise AssertionError((tool, args))

        board.call = call
        verify(main)
        with self.assertRaises(Conflict):
            self.create('auto-unassigned', autoCrosswalkReview=True)
        for name, changed in (('auto-allowed', False), ('auto-held', True)):
            task = self.create(name, crosswalkReviewer='main', autoCrosswalkReview=True,
                               facts=['Do not share this private fact'])
            plan = self.claim()
            self.assertNotIn('autoCrosswalkReview', plan['packet'])
            self.submit(plan, approach='Read official release', checks=[CRITERION])
            self.submit(self.claim(), summary='Reviewed research', citations=[SOURCE])
            self.assertEqual(int(changed), len(posts))
            if changed:
                board.settings['workers']['main'] = replacement
                verify(replacement)
            self.submit(self.claim(), verdict='accept', feedback='Exact citation', criteriaMet=[CRITERION])
            result = self.manager.operator(HUMAN, {'action': 'read', 'taskId': task['taskId']})
            self.assertEqual('done', result['state'])
            if changed:
                self.assertNotIn('crosswalk', result)
                self.assertIn('held', result['nextSafeAction'])
            else:
                self.assertEqual(('posted', 'automatic-post'),
                                 (result['crosswalk']['state'], result['crosswalk']['postId']))
        self.assertEqual(1, len(posts))
        self.assertEqual(main, posts[0]['for'])
        self.assertIn(SOURCE, posts[0]['body'])
        self.assertNotIn('Do not share this private fact', posts[0]['body'])

    def test_automatic_review_recovers_after_result_commit(self):
        owner, reviewer = '1111-2222-3333-4444', '2222-3333-4444-5555'
        crosswalk = {'board': 'fixture-board', 'number': owner, 'workers': {'main': reviewer}}
        self.manager.crosswalk.settings.update(crosswalk)
        self.monitor.connections.record('crosswalk-main', 'Tested write', 'Fixture only', providerAccountId=reviewer,
                                        board='fixture-board', sender=owner)
        task = self.create('recover-automatic', crosswalkReviewer='main', autoCrosswalkReview=True)
        self.submit(self.claim(), approach='Read official release', checks=[CRITERION])
        self.submit(self.claim(), summary='Reviewed research', citations=[SOURCE])
        with patch.object(self.manager.crosswalk, 'deliver_automatic', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.submit(self.claim(), verdict='accept', feedback='Exact citation', criteriaMet=[CRITERION])
        restarted = Monitor({'state': self.temp.name, 'workspaces': {}, 'principals': {},
                             'manager': {'crosswalk': crosswalk}})
        self.addCleanup(restarted.store.db.close)
        posts = []

        def call(tool, args):
            if tool == 'crosswalks':
                return [{'slug': 'fixture-board', 'private': True}]
            if tool == 'account' and args['action'] == 'numbers':
                return {'you': owner}
            if tool == 'account' and args['action'] == 'rules':
                return {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
            if tool == 'post':
                posts.append(args)
                return {'id': 'recovered-post'}
            if tool == 'open':
                return {'replies': [{'id': 'recovered-reply', 'reply_to': 'recovered-post', 'via_number': reviewer,
                                     'brief': posts[0]['brief'].replace('METAMUSE REVIEW ', 'METAMUSE RESULT '),
                                     'body': 'First line\nSecond line' + 'é' * 2000}]}
            raise AssertionError((tool, args))

        manager = restarted.manager
        manager.crosswalk.call = call
        self.assertEqual(task['taskId'], manager.crosswalk.next_automatic())
        self.assertTrue(manager.worker(WORKER, {'action': 'claim'})['deliveryProcessed'])
        self.assertEqual((0, True), manager_worker.step({}, {'deliveryProcessed': True}))
        self.assertIsNone(manager.crosswalk.next_automatic())
        self.assertEqual(1, len(posts))
        receipt = manager.crosswalk.reconcile_dispatch(task['taskId'])
        self.assertEqual(('reply-verified', 'recovered-reply'), (receipt['state'], receipt['replyId']))
        self.assertTrue(receipt['replyText'].startswith('First line\nSecond line'))
        self.assertLessEqual(len(receipt['replyText'].encode('utf-8')), 3500)
        self.assertTrue(receipt['replyTruncated'])
        self.assertNotIn('replyText', manager.dashboard()['board'][0]['crosswalk'])

    def test_public_research_targets_and_text(self):
        self.assertEqual(SOURCE, source_url('https://github.com/NousResearch/hermes-agent/releases/latest'))
        reddit = 'https://www.reddit.com/r/Blender/comments/ab12/title'
        self.assertEqual(source_url(reddit), source_url(source_url(reddit)))
        for url in ('http://github.com', 'https://api.github.com.evil.invalid/repos/x/y/releases',
                    'https://api.github.com/repos/../y/releases', 'https://docs.github.com/path?auth=secret'):
            with self.assertRaises(Conflict):
                source_url(url)
        value = extract(json.dumps({'title': 'Release', 'body': 'A concrete public feature suitable for a test result.'}).encode(), 'application/json')
        self.assertIn('concrete public feature', value)

    def test_relay_recovers_lost_write_without_reposting(self):
        task = self.create('fixture-relay')
        self.submit(self.claim(), approach='Collect the source', checks=[CRITERION])
        self.submit(self.claim(), summary='Collected evidence', citations=[SOURCE])
        self.submit(self.claim(), verdict='accept', feedback='Exact URL', criteriaMet=[CRITERION])
        folder = 'test-folder-identifier'
        file_id = 'test-file-id-preallocated'
        self.manager.config['relay'] = {'folderId': folder, 'consumerTaskId': 'test-scheduled-consumer'}
        self.monitor.connections.record('google', 'Tested read', 'Fixture only')
        api = self

        class Drive:
            writes = 0
            content = None
            def call(self, service, path, raw=False):
                api.assertEqual(service, 'drive')
                if path.startswith(f'files/{folder}/permissions'):
                    return {'permissions': [{'type': 'user'}]}
                if path.startswith(f'files/{folder}?'):
                    return {'id': folder, 'mimeType': 'application/vnd.google-apps.folder', 'capabilities': {'canAddChildren': True}}
                if path.startswith('files/generateIds'):
                    return {'ids': [file_id]}
                if raw:
                    return self.content
                return {'id': file_id, 'parents': [folder]}
            def upload_text(self, id, parent, name, value):
                api.assertEqual((file_id, folder), (id, parent))
                self.writes += 1
                self.content = value
                raise TimeoutError('deliberate loss after the mutation')

        drive = Drive()
        self.monitor.google = drive
        stored = self.manager.operator(HUMAN, {'action': 'read', 'taskId': task['taskId']})
        request = {'action': 'relay-publish', 'taskId': task['taskId'], 'revision': stored['revision']}
        self.assertEqual('unknown', self.manager.operator(HUMAN, request)['state'])
        with self.assertRaises(Conflict):
            self.manager.operator(HUMAN, request)
        self.manager.config['relay'] = {'folderId': 'test-other-folder', 'consumerTaskId': 'other-consumer'}
        result = self.manager.operator(HUMAN, {**request, 'action': 'relay-reconcile'})
        self.assertEqual('stored', result['state'])
        self.assertIn('Awaiting scheduled consumer receipt', result['detail'])
        self.assertEqual(1, drive.writes)

    def test_crosswalk_requires_exact_provider_number_and_reply_thread(self):
        board = self.manager.crosswalk
        owner = '1111-2222-3333-4444'
        worker = '2222-3333-4444-5555'
        board.settings.update(board='fixture-board', number=owner, workers={'main': worker})
        state = {'number': owner, 'private': False, 'posts': 0}

        def call(tool, args):
            if tool == 'crosswalks':
                return {'boards': [{'slug': 'fixture-board', 'private': state['private']}]}
            if tool == 'account':
                if args['action'] == 'numbers':
                    return {'kind': 'numbers', 'you': owner, 'numbers': [{'agent': 'Hermes', 'number': owner}]}
                if args['action'] == 'rules':
                    return {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
                raise AssertionError(args)
            if tool == 'post':
                state['posts'] += 1
                state['correlation'] = args['brief'].split()[-1]
                return {'post_id': 'fixture-receipt'}
            if tool == 'open':
                return {'replies': [{'id': 'ack-fixture', 'reply_to': 'fixture-receipt', 'via_number': state['number'],
                                     'brief': 'METAMUSE ACK ' + state['correlation']}]}
            raise AssertionError(tool)

        board.call = call
        with self.assertRaises(Denied):
            board.begin('main')
        state['private'] = True
        self.assertEqual('posted', board.begin('main')['state'])
        self.assertEqual('posted', board.begin('main')['state'])
        self.assertEqual('posted', board.reconcile('main')['state'])
        state['number'] = worker
        self.assertEqual('verified', board.reconcile('main')['state'])
        self.assertEqual(1, state['posts'])
        self.assertEqual('Tested write', self.monitor.connections.saved('crosswalk-main')['status'])

    def test_crosswalk_uses_authenticated_you_not_another_agents_roster_number(self):
        board = self.manager.crosswalk
        owner, worker = '1111-2222-3333-4444', '2222-3333-4444-5555'
        board.settings.update(board='fixture-board', number=owner, workers={'main': worker})
        identity = {'kind': 'numbers', 'you': owner,
                    'numbers': [{'agent': 'Muse', 'number': worker}, {'agent': 'Hermes', 'number': owner}]}

        def call(tool, args):
            if tool == 'crosswalks':
                return {'mine': [{'slug': 'fixture-board', 'visibility': 'private'}]}
            if tool == 'account' and args['action'] == 'numbers':
                return identity
            if tool == 'account' and args['action'] == 'rules':
                return {'agent': 'Hermes', 'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
            raise AssertionError((tool, args))

        board.call = call
        self.assertEqual({'board': 'fixture-board', 'providerAccountId': owner}, board.check())
        for current in (worker, None, {'number': owner}):
            identity['you'] = current
            with self.subTest(current=current), self.assertRaises(Denied):
                board.begin('main')
        self.assertEqual(0, self.monitor.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_crosswalk_refuses_broad_wrong_or_read_only_key_before_a_post(self):
        board = self.manager.crosswalk
        owner, worker = '1111-2222-3333-4444', '2222-3333-4444-5555'
        board.settings.update(board='fixture-board', number=owner, workers={'main': worker})
        rules = {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}

        def call(tool, args):
            if tool == 'crosswalks':
                return [{'slug': 'fixture-board', 'private': True}]
            if tool == 'account' and args['action'] == 'numbers':
                return {'you': owner}
            if tool == 'account' and args['action'] == 'rules':
                return rules
            raise AssertionError((tool, args))

        board.call = call
        for scope in ({'scope': 'full', 'enters': 'all'}, {'scope': 'full', 'enters': '*'},
                      {'scope': 'full', 'enters': 'to/fixture-board, to/another'},
                      {'scope': 'full', 'enters': 'to/fixture-board-extra'},
                      {'scope': 'read', 'enters': 'to/fixture-board'}, None):
            rules['key'] = scope
            with self.subTest(scope=scope), self.assertRaises(Denied):
                board.begin('main')
        self.assertEqual(0, self.monitor.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_crosswalk_public_review_is_idempotent_and_reconciles_an_unknown_post(self):
        task = self.create('fixture-crosswalk', facts=['Private fixture context must stay local'])
        self.submit(self.claim(), approach='Read official release', checks=[CRITERION])
        self.submit(self.claim(), summary='Reviewed research', citations=[SOURCE])
        self.submit(self.claim(), verdict='accept', feedback='Exact public citation', criteriaMet=[CRITERION])
        board = self.manager.crosswalk
        owner, worker = '1111-2222-3333-4444', '2222-3333-4444-5555'
        board.settings.update(board='fixture-board', number=owner, workers={'main': worker})
        state = {'sender': owner, 'posts': 0}

        def call(tool, args):
            if tool == 'crosswalks':
                return [{'slug': 'fixture-board', 'private': True}]
            if tool == 'account':
                if args['action'] == 'numbers':
                    return {'kind': 'numbers', 'you': owner}
                if args['action'] == 'rules':
                    return {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
                raise AssertionError(args)
            if tool == 'post':
                state['posts'] += 1
                state['brief'] = args['brief']
                self.assertIn(SOURCE, args['body'])
                self.assertNotIn('Private fixture context', args['body'])
                raise Conflict('Response lost after provider accepted the post')
            if tool == 'read':
                return {'results': [{'id': 'review-post', 'via_number': owner, 'brief': state['brief']}]}
            if tool == 'open':
                return {'replies': [{
                    'id': 'review-reply', 'reply_to': 'review-post', 'via_number': state['sender'],
                    'brief': state['brief'].replace('METAMUSE REVIEW ', 'METAMUSE RESULT ')
                }]}
            raise AssertionError(tool)

        board.call = call
        saved = self.manager.operator(HUMAN, {'action': 'read', 'taskId': task['taskId']})
        request = {'action': 'crosswalk-dispatch', 'taskId': task['taskId'], 'role': 'main', 'revision': saved['revision']}
        with self.assertRaises(Denied):
            self.manager.operator(HUMAN, request)
        self.monitor.connections.record('crosswalk-main', 'Tested write', 'Fixture only', providerAccountId=worker,
                                        board='fixture-board', sender=owner)
        with self.assertRaises(Conflict):
            self.manager.operator(HUMAN, request)
        self.assertEqual('unknown', self.manager.operator(HUMAN, request)['state'])
        action = {'action': 'crosswalk-dispatch-reconcile', 'taskId': task['taskId']}
        self.assertEqual('posted', self.manager.operator(HUMAN, action)['state'])
        state['sender'] = worker
        receipt = self.manager.operator(HUMAN, action)
        self.assertEqual(('reply-verified', 'review-reply'), (receipt['state'], receipt['replyId']))
        self.assertEqual(1, state['posts'])

    def test_assigned_reviewer_must_be_verified_and_cannot_be_replaced_at_dispatch(self):
        board = self.manager.crosswalk
        owner, main, dad = '1111-2222-3333-4444', '2222-3333-4444-5555', '3333-4444-5555-6666'
        board.settings.update(board='fixture-board', number=owner, workers={'main': main, 'dad': dad})
        with self.assertRaises(Denied):
            self.create('assigned-review', crosswalkReviewer='dad')
        for role, number in board.settings['workers'].items():
            self.monitor.connections.record('crosswalk-' + role, 'Tested write', 'Fixture only',
                                            providerAccountId=number, board='fixture-board', sender=owner)

        def call(tool, args):
            if tool == 'crosswalks':
                return [{'slug': 'fixture-board', 'private': True}]
            if tool == 'account' and args['action'] == 'numbers':
                return {'you': owner}
            if tool == 'account' and args['action'] == 'rules':
                return {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
            if tool == 'post':
                self.assertEqual(dad, args['for'])
                return {'id': 'assigned-post'}
            raise AssertionError((tool, args))

        board.call = call
        task = self.create('assigned-review', crosswalkReviewer='dad')
        self.assertEqual('dad', self.manager.dashboard()['board'][0]['crosswalkReviewer'])
        self.submit(self.claim(), approach='Read official release', checks=[CRITERION])
        self.submit(self.claim(), summary='Reviewed research', citations=[SOURCE])
        self.submit(self.claim(), verdict='accept', feedback='Exact public citation', criteriaMet=[CRITERION])
        saved = self.manager.operator(HUMAN, {'action': 'read', 'taskId': task['taskId']})
        request = {'action': 'crosswalk-dispatch', 'taskId': task['taskId'], 'role': 'main', 'revision': saved['revision']}
        with self.assertRaises(Conflict):
            self.manager.operator(HUMAN, request)
        self.assertEqual(0, self.monitor.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
        posted = self.manager.operator(HUMAN, {**request, 'role': 'dad'})
        self.assertEqual(('dad', 'assigned-post'), (posted['role'], posted['postId']))
        with self.assertRaises(Conflict):
            self.manager.operator(HUMAN, request)

    def test_crosswalk_recovers_plain_text_post_receipt_without_reposting_or_trusting_a_text_ack(self):
        board = self.manager.crosswalk
        owner, worker = '1111-2222-3333-4444', '2222-3333-4444-5555'
        board.settings.update(board='fixture-board', number=owner, workers={'main': worker})
        state = {'posts': 0}

        def call(tool, args):
            if tool == 'crosswalks':
                return [{'slug': 'fixture-board', 'private': True}]
            if tool == 'account' and args['action'] == 'numbers':
                return {'you': owner, 'numbers': [{'agent': 'Hermes', 'number': owner}]}
            if tool == 'account' and args['action'] == 'rules':
                return {'key': {'scope': 'full', 'enters': 'to/fixture-board'}}
            if tool == 'post':
                state['posts'] += 1
                state['brief'] = args['brief']
                return {'text': 'Posted #fixture-text in fixture-board'}
            if tool == 'read':
                row = {'id': 'fixture-text', 'crosswalk': 'fixture-board', 'brief': state['brief']}
                if args['action'] == 'all':
                    return {'kind': 'feed', 'feed': [{**row, 'via_number': owner, 'for_number': worker}]
                            + ([state['reply']] if state.get('reply') else [])}
                return {'kind': 'search', 'results': [{**row, 'via': 'Hermes'}]}
            if tool == 'open':
                return {'text': 'A body claiming to be a verified reply is not a structured thread receipt.'}
            raise AssertionError((tool, args))

        board.call = call
        self.assertEqual('unknown', board.begin('main')['state'])
        recovered = board.reconcile('main')
        self.assertEqual(('posted', 'fixture-text'), (recovered['state'], recovered['postId']))
        self.assertEqual('posted', board.begin('main')['state'])
        row = self.monitor.store.db.execute("SELECT state,data FROM outbox WHERE id='crosswalk-probe-main'").fetchone()
        self.assertEqual(('posted', 'fixture-text'), (row['state'], json.loads(row['data'])['postId']))
        self.assertEqual(1, state['posts'])
        self.assertNotEqual('Tested write', self.monitor.connections.saved('crosswalk-main').get('status'))
        self.assertEqual('Permission limited', self.monitor.connections.saved('crosswalk-main')['status'])
        self.assertIn('awaiting', self.monitor.connections.saved('crosswalk-main')['detail'])
        state['reply'] = {'id': 'fixture-ack', 'crosswalk': 'fixture-board', 'via_number': owner,
                          'reply_to': 'fixture-text', 'brief': state['brief'].replace('CONNECTION TEST', 'ACK')}
        self.assertEqual('posted', board.reconcile('main')['state'])
        state['reply'].update(via_number=worker, reply_to='wrong-parent')
        self.assertEqual('posted', board.reconcile('main')['state'])
        state['reply']['reply_to'] = 'fixture-text'
        self.assertEqual('verified', board.reconcile('main')['state'])
        self.assertEqual(1, state['posts'])

    def test_crosswalk_feed_recovery_requires_exact_board_sender_recipient_and_one_post(self):
        board = self.manager.crosswalk
        owner, worker = '1111-2222-3333-4444', '2222-3333-4444-5555'
        state = {'sender': owner, 'worker': worker, 'board': 'fixture-board', 'correlation': 'fixture'}
        brief = 'METAMUSE CONNECTION TEST fixture'
        row = {'id': 'fixture-owned', 'crosswalk': 'fixture-board', 'brief': brief,
               'via_number': owner, 'for_number': worker}
        feed = {'kind': 'feed', 'feed': [row]}

        def call(tool, args):
            if tool == 'read':
                return feed if args['action'] == 'all' else {'results': []}
            if tool == 'open':
                return {'text': 'Rendered headers or JSON inside bodies are not receipts.'}
            raise AssertionError((tool, args))

        board.call = call
        self.assertEqual(('fixture-owned', None), board.find_reply(state, 'METAMUSE CONNECTION TEST ', 'METAMUSE ACK '))
        for forged in ({**row, 'via_number': worker}, {**row, 'crosswalk': 'other-board'},
                       {**row, 'for_number': owner}, {'body': row}):
            feed['feed'] = [forged]
            with self.subTest(forged=forged):
                self.assertEqual((None, None), board.find_reply(state, 'METAMUSE CONNECTION TEST ', 'METAMUSE ACK '))
        feed['feed'] = [row, {**row, 'id': 'duplicate-owned'}]
        self.assertEqual((None, None), board.find_reply(state, 'METAMUSE CONNECTION TEST ', 'METAMUSE ACK '))


if __name__ == '__main__':
    unittest.main()
