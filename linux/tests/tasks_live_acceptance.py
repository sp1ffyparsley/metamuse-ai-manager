"""Isolated real Chrome + authenticated HTTP acceptance, never user profiles."""
import hashlib
import http.client
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from muse_monitor.server import Monitor, Handler, UnixServer
from muse_monitor.store import Store, digest
from muse_monitor.tasks import Tasks
from muse_monitor.task_view import render
from muse_monitor.legacy_actions import guard_site
from muse_monitor.daemon import execute
from muse_monitor.cli import request as unix_request


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        html = b'<title>Private task receipt fixture</title><input id="draft" value="keep my diagnostic draft"><button onclick="document.querySelector(\'output\').textContent=\'Request accepted: DIAGNOSTIC-12345\'">Send diagnostic request</button><output>Draft only</output><p hidden>Hidden completion must not count</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *args):
        pass


def main():
    os.umask(0o077)
    checks = {}
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:' + str(fixture.server_port) + '/'
    with tempfile.TemporaryDirectory(prefix='muse-tasks-live-') as folder:
        root = Path(folder)
        tokens = {r: secrets.token_urlsafe(32) for r in ('main', 'human', 'alt')}
        config = {'state': str(root / 'state'), 'runtime': str(root / 'runtime'), 'nodeBin': '/opt/muse/node/bin',
                  'principals': {r: {'hash': digest(t), 'role': r, 'workspaces': ['jobs']} for r, t in tokens.items()},
                  'workspaces': {'jobs': {'name': 'Private diagnostic', 'session': 'taskfixture', 'base': str(root / 'base'), 'profile': str(root / 'profile'), 'preserveExisting': True}}}
        (root / 'runtime').mkdir(mode=0o700)
        monitor = Monitor(config)
        api = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        api.monitor = monitor
        threading.Thread(target=api.serve_forever, daemon=True).start()
        human_key = root / 'human.token'
        human_key.write_text(tokens['human'])
        main_key=root/'main.token';main_key.write_text(tokens['main'])
        config['principalFiles'] = {'windows-human': str(human_key),'main':str(main_key)}
        config_file = root / 'config.json'
        config_file.write_text(json.dumps(config))
        unix = UnixServer(str(root / 'runtime/muse-monitor.sock'), Handler)
        unix.monitor = monitor
        threading.Thread(target=unix.serve_forever, daemon=True).start()

        def request(body, actor='main', expected=200):
            conn = http.client.HTTPConnection('127.0.0.1', api.server_port, timeout=30)
            conn.request('POST', '/v1/worker', json.dumps(body), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tokens[actor], 'X-Muse-Protocol': '1'})
            response = conn.getresponse()
            value = json.loads(response.read())
            conn.close()
            assert response.status == expected, (response.status, value)
            return value

        def task(action, **data):
            return request({'op': 'task', 'action': action, **data})

        launch = root / 'launch.json'
        launch.write_text(json.dumps({'browser': {'launchOptions': {'executablePath': '/opt/google/chrome/chrome', 'headless': True, 'chromiumSandbox': True,
                      'ignoreDefaultArgs': ['--disable-blink-features=AutomationControlled', '--enable-unsafe-swiftshader', '--unsafely-disable-devtools-self-xss-warnings']}}, 'outputMode': 'stdout'}))
        try:
            monitor.browser.cli('jobs', 'open', url, '--profile=' + str(root / 'profile'), '--config=' + str(launch), seconds=40)
            page = monitor.browser.describe('jobs', force=True)['pages'][0]
            assert page['url'] == url
            before = monitor.browser.run('jobs', {'kind': 'describe'})
            packet = {'title': 'Write a gardening note and send a diagnostic request', 'topic': 'Gardening', 'goal': 'Demonstrate generic work with two kinds of evidence',
                      'workspace': 'jobs', 'nextAction': 'Prepare note and check diagnostic receipt', 'successCriteria': [
                          {'id': 'request', 'description': 'Diagnostic portal accepts the request', 'evidence': 'page'},
                          {'id': 'note', 'description': 'A local note PDF exists', 'evidence': 'file'}]}
            saved = task('save', id='DIAGNOSTIC-gardening', revision=0, packet=packet)
            checks['generic_packet_over_authenticated_http'] = saved['state'] == 'ready'
            claimed = request({'op': 'claim-item', 'item': saved['id'], 'revision': saved['revision']})
            saved['revision'] = claimed['revision']
            proof = {'op': 'task', 'action': 'receipt', 'id': saved['id'], 'revision': saved['revision'], 'criterion': 'request', 'page': page['id'], 'pageGeneration': page['generation'], 'url': url}
            request({**proof, 'text': 'Hidden completion must not count'}, expected=409)
            request({**proof, 'text': 'Request accepted: DIAGNOSTIC-12345'}, expected=409)
            checks['hidden_or_missing_confirmation_rejected'] = True
            blocked = task('block', id=saved['id'], revision=saved['revision'], reason='Diagnostic decision needed', question='May the diagnostic request continue?', nextAction='Observe the isolated confirmation', url=url)
            assert request({'op': 'next-work'}) == []
            human_view = request({'op': 'task', 'action': 'list', 'state': 'needs-you'}, actor='human')
            assert 'May the diagnostic request continue?' in render(human_view['tasks'])
            view = subprocess.run([sys.executable, '-m', 'muse_monitor.task_view', '--config', str(config_file), '--text'], capture_output=True, text=True, timeout=15, check=True)
            assert 'May the diagnostic request continue?' in view.stdout
            checks['needs_you_visible_and_removed_from_ready_queue'] = True
            checks['on_demand_view_reads_same_journal_over_unix_socket'] = True
            saved = task('unblock', id=saved['id'], revision=blocked['revision'], resolution='Diagnostic test may continue')
            monitor.browser.cli('jobs', 'run-code', 'async p => { await p.getByRole("button",{name:"Send diagnostic request"}).click(); return {diagnosticOnly:true}; }')
            proof['revision'] = saved['revision']
            request({**proof, 'text': 'Request accepted: DIAGNOSTIC-12345', 'url': url + 'wrong'}, expected=409)
            request({**proof, 'text': 'Request accepted: DIAGNOSTIC-12345', 'pageGeneration': 'stale'}, expected=409)
            saved = request({**proof, 'text': 'Request accepted: DIAGNOSTIC-12345'})
            checks['real_visible_exact_page_receipt'] = saved['receipts']['request']['method'] == 'page'
            checks['wrong_url_and_stale_document_rejected'] = True
            folder = monitor.root / 'artifacts'
            folder.mkdir(mode=0o700)
            content = b'%PDF-1.4\nPrivate diagnostic garden note\n%%EOF'
            (folder / 'Diagnostic_Garden.pdf').write_bytes(content)
            saved = task('receipt', id=saved['id'], revision=saved['revision'], criterion='note', name='Diagnostic_Garden.pdf', sha256=hashlib.sha256(content).hexdigest())
            done = task('finish', id=saved['id'], revision=saved['revision'], summary='Diagnostic confirmation and local note checked')
            checks['all_criteria_required_for_durable_completion'] = done['state'] == 'done' and len(done['receipts']) == 2
            request({'op': 'claim-item', 'item': saved['id']}, expected=409)
            request({'op': 'task', 'action': 'list'}, actor='alt', expected=403)
            checks['completed_task_not_replayed_and_unenrolled_role_denied'] = True
            pin=request({'op':'site','action':'remember','url':url,'workspace':'jobs','revision':0,'note':'Private fixture chosen profile; no login claim'})
            browser_task=task('save',id='DIAGNOSTIC-continuity',revision=0,packet={**packet,'targetURL':url})
            block={'op':'task','action':'block','id':browser_task['id'],'revision':browser_task['revision'],'category':'captcha','siteRevision':pin['revision'],
                   'reason':'Private diagnostic challenge','question':'User must resolve the diagnostic checkpoint','nextAction':'Inspect this same form','url':url}
            blocked=request(block);assert blocked['notificationRecommended']
            duplicate=request({**block,'revision':blocked['revision']});assert not duplicate['notificationRecommended']
            checks['site_profile_memory_and_single_unresolved_notice']=True
            spec,runtime,base,cwd=monitor.browser.paths('jobs')
            native=execute(base,spec['session'],cwd,['list'])
            assert native.muse_current_url==url
            try:
                guard_site('click',['unused-ref'],native.muse_current_url,lambda *args:(_ for _ in ()).throw(AssertionError('Extra browser call')),
                    lambda target:unix_request(config_file,{'op':'site','action':'check','workspace':'jobs','url':target}))
            except ValueError:
                checks['old_handler_guard_stops_blocked_site_without_extra_browser_rpc']=True
            else:raise AssertionError('Blocked write was admitted')
            try:
                guard_site('goto',['https://independent.example/'],native.muse_current_url,lambda *args:None,
                    lambda target:unix_request(config_file,{'op':'site','action':'check','workspace':'jobs','url':target}))
            except ValueError:pass
            else:raise AssertionError('Independent navigation replaced a parked form')
            inspection=request({'op':'claim','workspace':'jobs'})
            request({'op':'browser','workspace':'jobs',**inspection,'requestId':'parked-nav-test-1',
                     'action':{'kind':'goto','page':page['id'],'document':page['generation'],'url':'https://independent.example/'}},expected=409)
            request({'op':'release','workspace':'jobs',**inspection})
            checks['parked_form_protected_from_navigation_to_other_work']=True
            connector=task('save',id='DIAGNOSTIC-connected-preparation',revision=0,packet={**packet,'execution':'connector','targetURL':url})
            queued=request({'op':'next-work'})
            assert any(r['id']==connector['id'] for r in queued) and not any(r['id']==browser_task['id'] for r in queued)
            checks['connected_preparation_continues_while_website_waits']=True
            cleared=task('unblock',id=browser_task['id'],revision=blocked['revision'],challengeId=blocked['blocker']['challengeId'],resolution='Private diagnostic user action observed')
            assert cleared['state']=='ready'
            monitor.browser.cli('jobs','run-code','async p => { if(await p.locator("#draft").inputValue()!=="keep my diagnostic draft") throw Error("Draft changed"); return {draftPreserved:true}; }')
            checks['explicit_resolution_preserves_form_and_browser_session']=True
            measured=unix_request(config_file,{'op':'work-brief','measure':True})
            assert measured['timing']['controllerMs']>=0 and measured['timing']['guestRoundTripMs']>0
            checks['controller_and_guest_round_trip_timings_measured']=True
            perf=request({'op':'performance','workspace':'jobs','page':page['id'],'pageGeneration':page['generation']})
            assert perf['lastDocumentNavigation']['domReadyMs']>=0
            diag=request({'op':'diagnostics'})
            assert diag['layers']['guestRpcMs']['samples']>0 and diag['layers']['browserOperationMs']['samples']>0
            checks['browser_navigation_and_adapter_layers_measured_without_content_logging']=True
            if os.environ.get('MUSE_TASK_VIEW_GUI') == '1':
                decision = task('save', id='DIAGNOSTIC-human-decision', revision=0, packet={**packet, 'title': 'Private human confirmation',
                    'successCriteria': [{'id': 'decision', 'description': 'Human reviewed the diagnostic note', 'evidence': 'human'}]})
                viewer = subprocess.Popen([sys.executable, '-m', 'muse_monitor.task_view', '--config', str(config_file)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                def window(title):
                    end = time.monotonic() + 10
                    while time.monotonic() < end:
                        result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', '^' + title + '$'], text=True, capture_output=True)
                        if result.returncode == 0:
                            return result.stdout.split()[-1]
                        time.sleep(.1)
                    raise AssertionError('Private native window missing: ' + title)
                def key(win, *keys):
                    subprocess.run(['xdotool', 'windowfocus', '--sync', win], check=True)
                    subprocess.run(['xdotool', 'key', '--clearmodifiers', *keys], check=True)
                try:
                    win = window('Muse Tasks')
                    time.sleep(.75)
                    geometry = subprocess.check_output(['xdotool', 'getwindowgeometry', '--shell', win], text=True)
                    size = dict(line.split('=', 1) for line in geometry.splitlines())
                    from PIL import ImageGrab
                    ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save('/tmp/muse-tasks-private-dialog.png')
                    # Observed GTK layout: Close, Refresh, Confirm a result.
                    subprocess.run(['xdotool', 'mousemove', '--window', win, str(int(size['WIDTH']) * 5 // 6), str(int(size['HEIGHT']) - 25), 'click', '1'], check=True)
                    win = window('Confirm a result')
                    key(win, 'End', 'Return')
                    time.sleep(.3)
                    win = window('Confirm a result')
                    key(win, 'Home', 'Return')
                    win = window('Record your confirmation')
                    subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '1', 'I reviewed the private diagnostic note'], check=True)
                    key(win, 'Return')
                    win = window('Muse Tasks')
                    key(win, 'Escape')
                    viewer.communicate(timeout=10)
                    assert viewer.returncode == 0
                    recorded = task('read', id=decision['id'])
                    assert recorded['receipts']['decision']['checkedBy'] == 'human'
                    assert recorded['receipts']['decision']['confirmation'] == 'I reviewed the private diagnostic note'
                    checks['real_native_human_confirmation_then_close'] = True
                finally:
                    if viewer.poll() is None:
                        viewer.terminate()
                    _, errors = viewer.communicate(timeout=5)
                    (Path('/tmp') / 'muse-tasks-private-viewer-errors.txt').write_bytes(errors or b'')
            monitor.store.db.close()
            monitor.store = Store(monitor.root / 'journal.sqlite3')
            monitor.tasks = Tasks(monitor)
            again = task('read', id=saved['id'])
            checks['receipt_and_completion_survive_store_reopen'] = again['receipts'] == done['receipts'] and again['state'] == 'done'
            after = monitor.browser.run('jobs', {'kind': 'describe'})
            checks['reads_and_workflow_preserve_browser_identity_and_pages'] = before == after
            checks['production_profiles_never_opened'] = str(root / 'profile').startswith(tempfile.gettempdir())
        finally:
            monitor.browser.cli('jobs', 'close')
            api.shutdown()
            api.server_close()
            unix.shutdown()
            unix.server_close()
            fixture.shutdown()
            fixture.server_close()
            monitor.store.db.close()
    assert all(checks.values()), checks
    print(json.dumps({'checks': checks, 'passed': len(checks), 'complete': True}))


if __name__ == '__main__':
    main()
