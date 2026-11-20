"""Small, topic-independent workflow stored in the existing item journal.

No scheduler or browser ownership is introduced. Evidence checks are read-only;
all mutations use the item's revision to reject stale/replayed updates.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from .store import Conflict, Denied

KEY = '_museTask'
LIMIT = 32768
STATES = ('ready', 'draft', 'needs-you', 'done')


def text(value, name, limit=2000, optional=False):
    if optional and value is None:
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise Conflict('Bounded ' + name + ' required')
    return value.strip()


def artifact(root, name, expected):
    from .artifacts import LIMIT as FILE_LIMIT, valid_artifact
    name = text(name, 'artifact filename', 120)
    if Path(name).name != name or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise Conflict('Use an admitted Muse Files filename')
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise Conflict('Exact artifact SHA-256 required')
    folder = Path(root) / 'artifacts'
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or not path.is_file() or path.resolve().parent != folder.resolve():
        raise Conflict('Required file is missing from Muse Files')
    # Open without following a substituted link; do not hash arbitrary files.
    import os
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > FILE_LIMIT:
            raise Conflict('Artifact exceeds the permitted format or size')
        data = stream.read(FILE_LIMIT + 1)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected or not valid_artifact(name, data):
        raise Conflict('File version or format changed; prepare the packet again')
    return {'name': name, 'sha256': actual, 'bytes': len(data)}


class Tasks:
    def __init__(self, monitor):
        self.monitor = monitor
        self.store = monitor.store

    def authorize(self, actor):
        if actor['role'] not in ('main', 'human') or actor['id'].startswith('extension-'):
            raise Denied('Main or attended task access required')

    def scope(self, actor, packet):
        if packet.get('workspace') is not None:
            self.monitor.scope(actor, packet['workspace'])

    def packet(self, actor, value):
        from .server import private_data, safe_url
        if not isinstance(value, dict):
            raise Conflict('Task packet object required')
        private_data(value)
        allowed = {'title', 'topic', 'goal', 'workspace', 'subject', 'sources', 'files', 'draft', 'questions', 'nextAction', 'successCriteria', 'execution', 'targetURL', 'siteContext'}
        if set(value) - allowed:
            raise Conflict('Unknown packet field; read task capabilities')
        p = {k: text(value.get(k), k, n) for k, n in (('title', 200), ('topic', 80), ('goal', 2000), ('nextAction', 1000))}
        p['workspace'] = value.get('workspace')
        self.scope(actor, p)
        p['execution'] = value.get('execution', 'browser' if p['workspace'] else 'offline')
        if p['execution'] not in ('browser','connector','offline'):
            raise Conflict('Execution is browser, connector or offline')
        if value.get('targetURL'):
            p['targetURL'] = safe_url(value['targetURL'])
            p['siteContext'] = text(value.get('siteContext','default'),'site context label',80)
            if p['execution']=='browser' and not p['workspace']:
                raise Conflict('Choose the intended persistent browser workspace')
            from .sites import identity
            identity(p['targetURL'],p['siteContext'])
        for key, bound in (('subject', 300), ('draft', 6000)):
            if value.get(key):
                p[key] = text(value[key], key, bound)
        sources = value.get('sources', [])
        if not isinstance(sources, list) or len(sources) > 12:
            raise Conflict('At most 12 source links per packet')
        p['sources'] = [safe_url(s) for s in sources]
        files = value.get('files', [])
        if not isinstance(files, list) or len(files) > 12:
            raise Conflict('At most 12 exact Muse Files references per packet')
        p['files'] = []
        for f in files:
            if not isinstance(f, dict) or set(f) != {'name', 'sha256'}:
                raise Conflict('Files need name and SHA-256 from artifacts')
            # Missing files are a visible preparation blocker, not a reason to
            # discard a useful packet. Syntax still must be valid.
            name = text(f['name'], 'filename', 120)
            if Path(name).name != name or not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[a-f0-9]{64}', str(f['sha256'])):
                raise Conflict('Safe file name and exact SHA-256 required')
            p['files'].append({'name': name, 'sha256': f['sha256']})
        questions = value.get('questions', [])
        if not isinstance(questions, list) or len(questions) > 12:
            raise Conflict('At most 12 specific questions per packet')
        p['questions'] = [text(q, 'question', 1000) for q in questions]
        criteria = value.get('successCriteria')
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12:
            raise Conflict('One to twelve explicit success criteria required')
        p['successCriteria'] = []
        for c in criteria:
            if not isinstance(c, dict) or set(c) != {'id', 'description', 'evidence'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', str(c.get('id'))):
                raise Conflict('Each criterion needs id, description, evidence')
            if c['evidence'] not in ('page', 'file', 'human'):
                raise Conflict('Evidence must be page, file, or human')
            p['successCriteria'].append({'id': c['id'], 'description': text(c['description'], 'success criterion', 500), 'evidence': c['evidence']})
        if len({c['id'] for c in p['successCriteria']}) != len(criteria):
            raise Conflict('Success criterion IDs must be unique')
        # Fits the existing bounded base64url SSH request, including its JSON
        # envelope. Larger essays/reports belong in linked documents.
        if len(json.dumps(p).encode()) > 8192:
            raise Conflict('Packet exceeds 8 KiB; link large supporting material')
        return p

    def problems(self, packet, check_site=True):
        problems = list(packet['questions'])
        site_problem = self.monitor.sites.task_problem(packet) if check_site else None
        if site_problem:problems.append(site_problem)
        for f in packet['files']:
            try:
                artifact(self.monitor.root, f['name'], f['sha256'])
            except (Conflict, OSError):
                problems.append('Prepare exact file: ' + f['name'])
        return problems

    def row(self, db, actor, item, revision=None):
        r = db.execute('SELECT * FROM item WHERE id=?', (item,)).fetchone()
        if not r:
            raise Conflict('Task not found')
        data = json.loads(r['data'])
        flow = data.get(KEY)
        if not isinstance(flow, dict) or flow.get('schema') != 1:
            raise Conflict('Prepare this item with task save first')
        self.scope(actor, flow['packet'])
        if revision is not None and (type(revision) is not int or revision != r['revision']):
            raise Conflict('Task revision changed; read it before making another update')
        return r, data, flow

    def write(self, db, r, data, flow, actor, release=False):
        if r['owner'] and r['owner'] != actor['id'] and (r['until'] or 0) > time.time() and actor['role'] != 'human':
            raise Conflict('Another worker owns this task')
        flow['updatedAt'] = time.time()
        flow['updatedBy'] = actor['id']
        if len(json.dumps(flow).encode()) > LIMIT:
            raise Conflict('Task record exceeds 32 KiB; use concise evidence and source links')
        data[KEY] = flow
        state = {'ready': 'queued', 'draft': 'draft', 'needs-you': 'needs-you', 'done': 'completed'}[flow['state']]
        db.execute('UPDATE item SET data=?,state=?,revision=revision+1,owner=CASE WHEN ? THEN NULL ELSE owner END,until=CASE WHEN ? THEN NULL ELSE until END WHERE id=?',
                   (json.dumps(data), state, release, release, r['id']))
        return {'id': r['id'], 'revision': r['revision'] + 1, **flow}

    def listing(self, actor, body):
        state = body.get('state', 'all')
        if state not in ('all', *STATES):
            raise Conflict('Unknown task state')
        cursor = body.get('cursor', 0)
        if type(cursor) is not int or cursor < 0:
            raise Conflict('Nonnegative task cursor required')
        rows = []
        more = False
        scanned = cursor
        with self.store.lock:
            records = self.store.db.execute("SELECT rowid,* FROM item WHERE rowid>? AND json_extract(data,'$._museTask.schema')=1 ORDER BY rowid LIMIT 501", (cursor,)).fetchall()
        for r in records[:500]:
            flow = json.loads(r['data'])[KEY]
            try:
                self.scope(actor, flow['packet'])
            except Denied:
                scanned = r['rowid']
                continue
            if state != 'all' and flow['state'] != state:
                scanned = r['rowid']
                continue
            if len(rows) == 50:
                more = True
                break
            scanned = r['rowid']
            p = flow['packet']
            site_problem = self.monitor.sites.task_problem(p)
            rows.append({'id': r['id'], 'revision': r['revision'], 'state': flow['state'], 'title': p['title'], 'topic': p['topic'],
                         'nextAction': flow.get('resumeAction', p['nextAction']), 'blocker': flow.get('blocker'), 'updatedAt': flow['updatedAt'], 'criteriaMet': len(flow['receipts']), 'criteriaTotal': len(p['successCriteria']),
                         'completionSummary': flow.get('completionSummary'), 'receipts': flow['receipts'],
                         'execution':p.get('execution','browser' if p.get('workspace') else 'offline'), 'waitingOnSite':site_problem, 'available':flow['state']=='ready' and not site_problem})
        return {'tasks': rows, 'nextCursor': scanned if more or len(records) > 500 else None, 'limit': 50}

    def evidence(self, actor, flow, body):
        from .server import safe_url
        criterion = next((c for c in flow['packet']['successCriteria'] if c['id'] == body.get('criterion')), None)
        if not criterion:
            raise Conflict('Unknown success criterion')
        method = criterion['evidence']
        proof = {'method': method, 'checkedAt': time.time(), 'checkedBy': actor['id'], 'packetHash': flow['packetHash']}
        if method == 'file':
            proof.update(artifact(self.monitor.root, body.get('name'), body.get('sha256')))
            proof['scope'] = 'Exact local file bytes exist; delivery or content quality is not implied'
        elif method == 'human':
            if actor['role'] != 'human':
                raise Denied('This criterion requires explicit attended human confirmation')
            proof.update(confirmation=text(body.get('confirmation'), 'human confirmation', 1000), scope='Human attestation; not independently checked against a website')
        else:
            wid = flow['packet'].get('workspace')
            if wid is None:
                raise Conflict('A page receipt requires an assigned browser workspace')
            self.monitor.scope(actor, wid)
            self.monitor.legacy_settled(wid)
            page = text(body.get('page'), 'exact page identity', 100)
            document = text(body.get('pageGeneration'), 'exact document generation', 100)
            expected = text(body.get('text'), 'visible confirmation text', 500)
            if len(expected) < 8:
                raise Conflict('Use specific confirmation text, at least eight characters')
            url = safe_url(body.get('url'))
            result = self.monitor.browser.action(wid, {'kind': 'receipt', 'page': page, 'document': document, 'url': url, 'text': expected})
            if not result.get('matched') or result.get('page', {}).get('id') != page or result['page'].get('generation') != document or result['page'].get('url') != url:
                raise Conflict('Confirmation was not observed on the exact current page')
            proof.update(page=page, pageGeneration=document, url=url, text=expected, scope='Visible text on this exact page/document; not a guarantee beyond that evidence')
        proof['checkedAt'] = time.time()
        return criterion['id'], proof

    def request(self, actor, body):
        from .server import private_data
        self.authorize(actor)
        private_data(body)
        action = body.get('action', 'list')
        if action == 'plan':
            from .sites import plan
            return plan(body.get('kind'))
        if action == 'guide':
            for path in (Path(__file__).parents[1] / 'TASK-WORKFLOW.md', Path(__file__).parents[2] / 'TASK-WORKFLOW.md'):
                if path.is_file():
                    return {'schema': 1, 'instructions': path.read_text()[:30000]}
            raise Conflict('Task guide is not installed')
        if action == 'list':
            return self.listing(actor, body)
        item = text(body.get('id'), 'stable task ID', 128)
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', item):
            raise Conflict('Stable task ID must contain letters, numbers, dot, dash, underscore or colon')
        if action == 'read':
            with self.store.lock:
                r, _, flow = self.row(self.store.db, actor, item)
            return {'id': item, 'revision': r['revision'], **flow, 'readinessProblems': self.problems(flow['packet'])}
        revision = body.get('revision')
        if type(revision) is not int or revision < 0:
            raise Conflict('Read the task revision first; use revision 0 only for a new task')
        if action == 'save':
            packet = self.packet(actor, body.get('packet'))
            problems = self.problems(packet)
            packet_hash = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()
            with self.store.tx() as db:
                existing = db.execute('SELECT * FROM item WHERE id=?', (item,)).fetchone()
                if existing is None:
                    if revision != 0:
                        raise Conflict('New task requires revision 0')
                    db.execute('INSERT INTO item(id,kind,data,revision,state) VALUES(?,?,?,0,?)', (item, 'unclassified', '{}', 'draft'))
                    r = db.execute('SELECT * FROM item WHERE id=?', (item,)).fetchone()
                    data, old = {}, {}
                else:
                    r = existing
                    if revision != r['revision']:
                        raise Conflict('Task revision changed; read before saving')
                    data = json.loads(r['data'])
                    old = data.get(KEY, {})
                    if old:
                        self.scope(actor, old['packet'])
                    if old.get('state') == 'done':
                        raise Conflict('Completed task is immutable; create a separate follow-up task')
                same = old.get('packetHash') == packet_hash
                state = 'needs-you' if packet['questions'] else 'draft' if problems else 'ready'
                flow = {'schema': 1, 'state': state, 'packet': packet, 'packetHash': packet_hash, 'receipts': old.get('receipts', {}) if same else {}, 'createdAt': old.get('createdAt', time.time())}
                if old.get('blocker'):
                    flow.update(state='needs-you', blocker=old['blocker'])
                elif packet['questions']:
                    flow['blocker'] = {'reason': 'Packet has unanswered questions', 'question': '\n'.join(packet['questions']), 'nextAction': packet['nextAction']}
                return self.write(db, r, data, flow, actor)
        # Read a snapshot without retaining the journal transaction during a
        # browser RPC; CAS is checked again before committing the result.
        with self.store.lock:
            r, data, flow = self.row(self.store.db, actor, item, revision)
        if flow['state'] == 'done':
            raise Conflict('Completed task is immutable; read its existing receipt')
        if action == 'receipt':
            criterion, proof = self.evidence(actor, flow, body)
        with self.store.tx() as db:
            r, data, flow = self.row(db, actor, item, revision)
            release = False
            if action == 'block':
                from .server import safe_url
                blocker = {k: text(body.get(k), k, 1000) for k in ('reason', 'question', 'nextAction')}
                if body.get('url'):
                    blocker['url'] = safe_url(body['url'])
                category=body.get('category','other')
                if category not in ('other','captcha'):raise Conflict('Block category is other or captcha')
                site = None
                if category=='captcha':
                    packet=flow['packet']
                    if not packet.get('targetURL') or not packet.get('workspace'):raise Conflict('CAPTCHA blocker needs the intended targetURL and workspace in its packet')
                    site=self.monitor.sites.request(actor,{'action':'challenge','url':packet['targetURL'],'context':packet.get('siteContext','default'),
                        'workspace':packet['workspace'],'revision':body.get('siteRevision',0),'question':blocker['question'],'nextAction':blocker['nextAction']},db=db)
                    blocker.update(category='captcha',challengeId=site['challenge']['id'])
                if flow.get('blocker')==blocker and flow['state']=='needs-you':
                    return {'id':item,'revision':r['revision'],**flow,'unchanged':True,'notificationRecommended':False}
                flow.update(state='needs-you', blocker=blocker)
                release = True
            elif action == 'unblock':
                resolution = text(body.get('resolution'), 'observed resolution', 1000)
                if flow['state'] != 'needs-you':
                    raise Conflict('Task is not on the Needs you list')
                if flow.get('blocker',{}).get('category')=='captcha':
                    packet=flow['packet']
                    site=self.monitor.sites.request(actor,{'action':'read','url':packet['targetURL'],'context':packet.get('siteContext','default')},db=db)
                    if site.get('challenge'):
                        self.monitor.sites.request(actor,{'action':'resolve','url':packet['targetURL'],'context':packet.get('siteContext','default'),
                            'revision':site['revision'],'challengeId':body.get('challengeId'),'resolution':resolution},db=db)
                if self.problems(flow['packet']):
                    raise Conflict('Update unanswered packet questions or missing files before resolving')
                flow['resumeAction'] = flow.pop('blocker', {}).get('nextAction', flow['packet']['nextAction'])
                flow.update(state='ready', lastResolution=resolution)
            elif action == 'receipt':
                flow['receipts'][criterion] = proof
            elif action == 'finish':
                if flow['state'] == 'needs-you' or self.problems(flow['packet'],check_site=False):
                    raise Conflict('Resolve task blockers and file changes before completion')
                if any(c['id'] not in flow['receipts'] or flow['receipts'][c['id']]['packetHash'] != flow['packetHash'] for c in flow['packet']['successCriteria']):
                    raise Conflict('Every success criterion needs a checked receipt; a click or claim is insufficient')
                for proof in flow['receipts'].values():
                    if proof['method'] == 'file':
                        artifact(self.monitor.root, proof['name'], proof['sha256'])
                flow.update(state='done', completedAt=time.time(), completionSummary=text(body.get('summary'), 'completion summary', 1000))
                release = True
            else:
                raise Conflict('Use task save, read, list, block, unblock, receipt, or finish')
            result=self.write(db, r, data, flow, actor, release)
            if action=='block':result['notificationRecommended']=site['notificationRecommended'] if site else True
            return result
