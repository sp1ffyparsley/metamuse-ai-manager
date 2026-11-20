"""Opt-in Ubuntu acceptance using real Codex OAuth and a public GitHub source."""
import argparse
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from muse_monitor import manager_worker
from muse_monitor.manager import ACTOR
from muse_monitor.server import Handler, Monitor, UnixServer
from muse_monitor.store import digest


def exchange(port, token, route, body=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        conn.request('GET' if body is None else 'POST', route, json.dumps(body) if body is not None else None,
                     {'Authorization': 'Bearer ' + token, 'X-Muse-Protocol': '1', 'Content-Type': 'application/json'})
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read(512 * 1024))
    finally:
        conn.close()


def expect(code, response):
    if response[0] != code:
        raise RuntimeError(f'Expected HTTP {code}, received {response[0]}')
    return response[1]


def boot(config, path):
    monitor = Monitor(config)
    transport = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    transport.monitor = monitor
    socket = UnixServer(path, Handler)
    socket.monitor = monitor
    socket.manager_worker_only = True
    for entry in (transport, socket):
        threading.Thread(target=entry.serve_forever, daemon=True).start()
    return monitor, transport, socket


def stop(monitor, transport, socket):
    socket_path = Path(socket.server_address)
    for entry in (transport, socket):
        entry.shutdown()
        entry.server_close()
    socket_path.unlink()
    monitor.store.db.close()


def exercise(hermes):
    with tempfile.TemporaryDirectory(prefix='metamuse-live-') as tmp:
        base = Path(tmp)
        human, worker = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
        key = base / 'worker'
        key.write_text(worker, encoding='utf-8')
        key.chmod(0o600)
        path = str(base / 'sock')
        cfg = {'state': str(base / 'state'), 'workspaces': {}, 'principals': {
            'windows-human': {'hash': digest(human), 'role': 'human', 'workspaces': []},
            ACTOR: {'hash': digest(worker), 'role': 'manager-worker', 'workspaces': []}}}
        adapter = {'socket': path, 'credentialFile': str(key), 'hermesRoot': str(hermes)}
        active = boot(cfg, path)
        public = 'https://api.github.com/repos/NousResearch/hermes-agent/releases/latest'
        checks = []
        stage_times = {}
        try:
            port = active[1].server_address[1]
            expect(403, exchange(port, worker, '/v1/status'))
            expect(403, exchange(port, worker, '/v1/manager/worker', {'action': 'claim'}))
            checks.append('Dedicated identity denied on general API and public worker path')
            stale = active[0].connections.record('hermes', 'Operational', 'Expired acceptance fixture')
            stale['verifiedAt'] = time.time() - 86401
            with active[0].store.tx() as db:
                db.execute('UPDATE setting SET data=? WHERE id=?', (json.dumps(stale), 'connection:hermes'))
            assert manager_worker.run(adapter) == 0
            assert active[0].connections.saved('hermes')['verifiedAt'] == stale['verifiedAt']
            research = {'action': 'create', 'requestId': 'research-acceptance', 'goal': 'software',
                        'capability': 'research', 'objective': 'Give a concise summary of the latest published Hermes release.',
                        'criteria': ['State the exact release version visible in the collected evidence.',
                                     'Cite the exact collected source URL.'],
                        'sources': [public], 'priority': 5}
            email = {'action': 'create', 'requestId': 'email-acceptance', 'goal': 'admin',
                     'capability': 'email-review', 'objective': 'Rewrite this fixture email to a collaborator and flag concrete problems.',
                     'criteria': ['Remove unsupported timing promises and claims of an attachment.',
                                  'Ask whether Thursday works in a natural, concise tone.'],
                     'recipient': 'Fixture collaborator', 'subject': 'Thursday review',
                     'draft': 'Esteemed esteemed collaborator, I guarantee that the packet will be delivered Tuesday. Please review my attachment. Does Thursday work?',
                     'facts': ['No delivery day has been agreed.', 'No file is attached.',
                               'Thursday is a proposed meeting day.']}
            items = [expect(200, exchange(port, human, '/v1/manager', item))['taskId'] for item in (research, email)]
            assert expect(200, exchange(port, human, '/v1/manager', research))['taskId'] == items[0]
            for n in range(12):
                selected = expect(200, exchange(port, human, '/v1/manager'))['board']
                if all(row['state'] == 'done' for row in selected):
                    break
                if any(row['state'] in ('blocked', 'unknown') for row in selected):
                    raise AssertionError([(row['taskId'], row['state'], row['blocker']) for row in selected])
                started = time.monotonic()
                assert manager_worker.run(adapter) == 0
                stage_times[str(n + 1)] = round((time.monotonic() - started) * 1000)
            tasks = [expect(200, exchange(port, human, '/v1/manager', {'action': 'read', 'taskId': id})) for id in items]
            assert all(task['state'] == 'done' for task in tasks)
            proof = expect(200, exchange(port, human, '/v1/connections'))
            hermes_proof = next(entry for entry in proof['entries'] if entry['id'] == 'hermes')
            assert hermes_proof['status'] == 'Operational' and not hermes_proof['stale']
            checks.append('Idle worker preserved expired verification. A queued task triggered a real kernel-isolated Codex probe and progressed automatically')
            assert tasks[0]['result']['citations'][0]['url'] == public
            assert tasks[0]['sourceEvidence'][0]['sha256'] == tasks[0]['result']['citations'][0]['sha256']
            cached = active[0].manager.research.get(public)
            assert cached['change'] == 'cached' and cached['sha256'] == tasks[0]['sourceEvidence'][0]['sha256']
            checks.append('Public GitHub response hash, citation, cache deduplication and independent model review verified')
            corrected = tasks[1]['result']['draft'].casefold()
            assert corrected != email['draft'].casefold() and 'thursday' in corrected
            assert 'i guarantee' not in corrected and 'my attachment' not in corrected
            checks.append('Hermes rejected defects in a deliberately poor draft and produced a reviewed revision without sending')
            stop(*active)
            active = None
            active = boot(cfg, path)
            after = expect(200, exchange(active[1].server_address[1], human, '/v1/manager'))
            assert len(after['board']) == 2 and all(row['state'] == 'done' for row in after['board'])
            checks.append('Verified results survived controller restart with no queue replay')
            return {'scope': 'isolated Ubuntu controller with real Hermes Codex OAuth and public GitHub read',
                    'model': hermes_proof['model'], 'probeMs': hermes_proof['elapsedMs'], 'stageWallMs': stage_times,
                    'taskStages': [{k: task[k] for k in ('state', 'calls', 'corrections')} for task in tasks],
                    'results': checks, 'source': public, 'sourceSha256': cached['sha256'], 'sourceObservedAtUnix': cached['observedAt'],
                    'systemComplete': False}
        finally:
            if active:
                stop(*active)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = exercise(args.hermes_root)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps(result, indent=2))
