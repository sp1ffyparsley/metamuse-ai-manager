"""Explicit site/profile memory and unresolved challenges in existing settings.

No cookie access, navigation, challenge solving, timer or notification sender.
"""
import hashlib
import contextlib
import json
import time
from urllib.parse import urlsplit
from .store import Conflict, Denied

PREFIX = 'site-memory:'


def identity(url, context='default'):
    from .server import safe_url
    from .tasks import text
    p = urlsplit(safe_url(url))
    host = p.hostname.rstrip('.').encode('idna').decode().lower()
    try:
        port = p.port
    except ValueError:
        raise Conflict('Invalid site port') from None
    if port and port not in (80, 443):
        host += ':' + str(port)
    context = text(context, 'site context label', 80)
    return host, context, PREFIX + hashlib.sha256((host + '|' + context).encode()).hexdigest()


class Sites:
    def __init__(self, monitor):
        self.monitor = monitor

    @property
    def store(self):
        return self.monitor.store

    def read(self, db, key):
        row = db.execute('SELECT data FROM setting WHERE id=?', (key,)).fetchone()
        return json.loads(row['data']) if row else None

    def check(self, workspace, url, context='default'):
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return None
        origin=urlsplit(url)._replace(path='',query='',fragment='').geturl()
        _, _, key = identity(origin, context)
        with self.store.lock:
            value = self.read(self.store.db, key)
        if value:
            if value['workspace'] != workspace:
                raise Conflict('Site belongs to another saved browser workspace; do not switch profiles or copy cookies')
            if value.get('challenge'):
                raise Conflict('Site is waiting for human challenge resolution; use Needs you and continue independent work')
        return value

    def task_problem(self, packet):
        if packet.get('execution', 'browser' if packet.get('workspace') else 'offline') != 'browser' or not packet.get('targetURL'):
            return None
        try:
            self.check(packet.get('workspace'), packet['targetURL'], packet.get('siteContext', 'default'))
        except Conflict as error:
            return str(error)
        return None

    def request(self, actor, body, db=None):
        from .server import private_data
        from .tasks import text
        self.monitor.tasks.authorize(actor)
        private_data(body)
        action = body.get('action', 'list')
        if action == 'list':
            with self.store.lock:
                rows = self.store.db.execute('SELECT data FROM setting WHERE id LIKE ? ORDER BY id LIMIT 201', (PREFIX + '%',)).fetchall()
            sites = [json.loads(r['data']) for r in rows[:200]]
            return {'sites': [s for s in sites if s['workspace'] in actor.get('workspaces', [])], 'limit': 200, 'truncated': len(rows) > 200}
        host, context, key = identity(body.get('url'), body.get('context', 'default'))
        with self.store.tx() if db is None else contextlib.nullcontext(db) as db:
            old = self.read(db, key)
            if old:
                self.monitor.scope(actor, old['workspace'])
            if action == 'read':
                return old or {'host': host, 'context': context, 'revision': 0, 'known': False}
            if action == 'check':
                self.monitor.scope(actor, body.get('workspace'))
                self.check(body['workspace'], body['url'], context)
                return {'allowed': True, 'binding': old, 'authentication': 'Not verified by routing memory'}
            if action not in ('remember', 'challenge', 'resolve'):
                raise Conflict('Use site list, read, check, remember, challenge or resolve')
            # Repeated identical unresolved blockers are reads, not new writes or
            # fresh notifications, even if the caller lost the first response.
            if action == 'challenge' and old and old.get('challenge'):
                if body.get('workspace', old['workspace']) != old['workspace']:
                    raise Conflict('Unresolved challenge belongs to the saved workspace')
                return {**old, 'notificationRecommended': False, 'unchanged': True, 'notificationDelivery': 'not asserted'}
            revision = body.get('revision')
            if type(revision) is not int or revision != (old['revision'] if old else 0):
                raise Conflict('Site memory changed; read its revision before updating')
            wid = body.get('workspace', old['workspace'] if old else None)
            self.monitor.scope(actor, wid)
            if old and wid != old['workspace']:
                if old.get('challenge') or body.get('rebind') is not True:
                    raise Conflict('Resolve any challenge and explicitly confirm a deliberate profile rebind')
            if old is None and db.execute('SELECT count(*) FROM setting WHERE id LIKE ?', (PREFIX + '%',)).fetchone()[0] >= 200:
                raise Conflict('Site memory limit reached; review existing bindings')
            value = dict(old or {}, host=host, context=context, workspace=wid, revision=revision + 1,
                         updatedAt=time.time(), updatedBy=actor['id'])
            if old and wid!=old['workspace']:value['previousWorkspace']=old['workspace']
            notify = False
            if action == 'remember':
                value['note'] = text(body.get('note'), 'observed profile/session note', 500)
                value['authentication'] = 'Routing preference only; verify current website sign-in before use'
            elif action == 'challenge':
                question = text(body.get('question'), 'specific human challenge action', 1000)
                value['challenge'] = {'id': hashlib.sha256((key + '|' + str(value['updatedAt'])).encode()).hexdigest()[:24],
                                      'firstObservedAt': time.time(), 'question': question,
                                      'nextAction': text(body.get('nextAction'), 'continuation step', 1000)}
                notify = True
            else:
                if not old or not old.get('challenge'):
                    raise Conflict('No unresolved site challenge')
                if body.get('challengeId') != old['challenge']['id']:
                    raise Conflict('The challenge changed; inspect it before resolving')
                value['lastResolution'] = text(body.get('resolution'), 'observed resolution after human action', 1000)
                value.pop('challenge', None)
            db.execute('INSERT INTO setting(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (key, json.dumps(value)))
            return {**value, 'notificationRecommended': notify, 'notificationDelivery': 'not asserted'}


def plan(kind):
    if kind not in ('research', 'records', 'documents', 'website', 'offline'):
        raise Conflict('Plan research, records, documents, website or offline work')
    route = 'browser' if kind == 'website' else 'offline' if kind == 'offline' else 'connector'
    return {'execution': route, 'kind': kind, 'browserLeaseNeeded': route == 'browser',
            'nextAction': {'research': 'Use available authorized search/source tools and record sources before opening a portal',
                           'records': 'Use the existing authorized record connector and canonical IDs; read back the result',
                           'documents': 'Prepare/import required approved files before claiming a browser',
                           'website': 'Reuse the saved profile, check unresolved challenges, then claim only for the website step',
                           'offline': 'Prepare the local result without browser ownership'}[kind],
            'availability': 'Worker must verify its actual connector permissions; this plan grants none'}
