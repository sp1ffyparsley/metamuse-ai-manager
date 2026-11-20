"""AI Manager workflow over the existing item journal and native authority."""
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from .connections import HUMANS, MUSE
from .research import Research, source_url
from .store import Conflict, Denied
from .tasks import text

KEY = '_manager'
ACTOR = 'manager-hermes'
LEASE_SECONDS = 180
REVISION_LIMIT = 1
DAILY_CALL_LIMIT = 16
PROVIDER_PROOF_SECONDS = 86400
FAILED_PROBE_COOLDOWN = 300
GOALS = {
    'creative': ('Content and creative production', 90),
    'tools': ('Editing, VFX and 3D tools', 80),
    'software': ('Software and GitHub', 75),
    'music-acting': ('Music, acting and casting', 68),
    'creator': ('Creator and sponsorship research', 62),
    'admin': ('Personal administration and education', 58),
    'jobs': ('Selective job maintenance', 35),
    'scholarships': ('Selective scholarship maintenance', 35),
}
CAPABILITIES = {
    'research': 'Collect public evidence and review a research brief',
    'email-review': 'Review and rewrite a user-supplied draft',
}
ACTIVE = {'queued', 'planning', 'working', 'reviewing'}
PHASE = {'queued': 'plan', 'planning': 'plan', 'working': 'work', 'reviewing': 'review'}


def json_value(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def id_value(value, field='identity'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise Conflict('Valid ' + field + ' required')
    return value


def bounded_number(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Conflict(f'{name} must be between {low} and {high}')
    return value


def strings(value, name, count, length):
    if not isinstance(value, list) or len(value) > count:
        raise Conflict(f'At most {count} {name} entries allowed')
    return list(dict.fromkeys(text(entry, name, length) for entry in value))


def rank(flow, goals, now):
    packet = flow['packet']
    goal = goals[packet['goal']]
    due = packet.get('dueAt')
    urgency = 0 if not due else 40 if due <= now else max(0, 36 - int((due - now) / 3600))
    factors = {'goal': goal['weight'], 'priority': packet['priority'] * 5, 'urgency': urgency,
               'value': packet['value'] * 4, 'effort': -packet['effort'] * 3,
               'sources': min(12, len(packet['sources']) * 4 + len(packet['facts']) * 2),
               'revision': -flow['corrections'] * 8, 'inProgress': 25 if flow['state'] != 'queued' else 0}
    return sum(factors.values()), factors


class Manager:
    def __init__(self, monitor):
        self.monitor = monitor
        self.store = monitor.store
        self.research = Research(self.store)
        self.config = monitor.config.get('manager', {})
        from .relay import Relay
        self.relay = Relay(self)
        from .crosswalk import Board
        self.crosswalk = Board(self)
        self.log = monitor.diagnostics
        self.log.emit('manager_start', 'manager', outcome='ok')

    def wake(self):
        target = self.config.get('wakePath')
        if not target:
            return
        try:
            path = Path(target)
            # systemd's user path unit watches this controller-owned rendezvous.
            # It contains no task, credential, identity or context.
            with path.open('wb') as marker:
                marker.write(b'ready\n')
            path.chmod(0o640)
        except OSError as error:
            self.log.emit('manager_wake', 'manager', outcome='error', errorType=type(error).__name__)

    def settings(self, db, key, default):
        row = db.execute('SELECT data FROM setting WHERE id=?', ('manager:' + key,)).fetchone()
        return json.loads(row['data']) if row else default

    def save(self, db, key, value):
        db.execute('INSERT INTO setting VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   ('manager:' + key, json_value(value)))

    def goals(self, db):
        overrides = self.settings(db, 'goals', {})
        return {id: {'id': id, 'label': label, 'weight': weight, 'enabled': True, 'constraints': [],
                     'revision': 0, **overrides.get(id, {})} for id, (label, weight) in GOALS.items()}

    def goal(self, body):
        id = body.get('goal')
        if id not in GOALS:
            raise Conflict('Choose an existing goal')
        weight = bounded_number(body.get('weight'), 'Goal weight', 0, 100)
        enabled = body.get('enabled')
        if type(enabled) is not bool:
            raise Conflict('Set the goal enabled flag explicitly')
        constraints = strings(body.get('constraints', []), 'goal constraint', 8, 200)
        with self.store.tx() as db:
            current = self.goals(db)[id]
            if type(body.get('revision')) is not int or body['revision'] != current['revision']:
                raise Conflict('Goal revision changed; reload before editing')
            record = {'weight': weight, 'enabled': enabled, 'constraints': constraints, 'revision': current['revision'] + 1}
            overrides = self.settings(db, 'goals', {})
            overrides[id] = record
            self.save(db, 'goals', overrides)
        self.log.emit('manager_goal', 'manager', command=id, outcome='ok')
        return {**current, **record}

    def packet(self, body):
        from .server import private_data
        private_data(body)
        capability = body.get('capability')
        goal = body.get('goal')
        if capability not in CAPABILITIES or goal not in GOALS:
            raise Conflict('Choose a configured goal and supported capability')
        sources = strings(body.get('sources', []), 'public source', 3, 700)
        canonical_sources = list(dict.fromkeys(source_url(url) for url in sources))
        due = body.get('dueAt')
        if due is not None:
            due = bounded_number(due, 'Unix due time', 1, 4102444800)
        packet = {
            'goal': goal, 'capability': capability,
            'objective': text(body.get('objective'), 'objective', 700),
            'criteria': strings(body.get('criteria', []), 'criterion', 6, 300),
            'sources': canonical_sources,
            'facts': strings(body.get('facts', []), 'approved fact', 8, 300),
            'dueAt': due,
            'priority': bounded_number(body.get('priority', 3), 'Priority', 1, 5),
            'value': bounded_number(body.get('value', 3), 'Value', 1, 5),
            'effort': bounded_number(body.get('effort', 2), 'Effort', 1, 5),
        }
        if not packet['criteria']:
            raise Conflict('At least one observable acceptance criterion is required')
        if capability == 'research' and not packet['sources']:
            raise Conflict('Supply an allowed public source for evidence collection')
        if (reviewer := body.get('crosswalkReviewer')) is not None:
            if capability != 'research' or not isinstance(reviewer, str) or reviewer not in MUSE:
                raise Conflict('Choose a Muse Crosswalk reviewer for a public research task')
            packet['crosswalkReviewer'] = reviewer
        automatic = body.get('autoCrosswalkReview', False)
        if not isinstance(automatic, bool) or (automatic and 'crosswalkReviewer' not in packet):
            raise Conflict('Automatic review requires a selected public research reviewer')
        if automatic:
            packet['autoCrosswalkReview'] = True
        if capability == 'email-review':
            if canonical_sources:
                raise Conflict('Use approved facts and the supplied draft for email review')
            packet['draft'] = text(body.get('draft'), 'draft body', 3000)
            packet['recipient'] = text(body.get('recipient'), 'recipient or role', 200)
            packet['subject'] = text(body.get('subject'), 'subject', 200)
        return packet

    def create(self, body):
        packet = self.packet(body)
        request = id_value(body.get('requestId'), 'creation request ID')
        supplied = body.get('canonicalItemId')
        canonical = id_value(supplied, 'canonical item ID') if supplied else 'metamuse-' + request
        signature = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()
        now = time.time()
        flow = {'schema': 1, 'taskId': canonical, 'canonicalItemId': canonical, 'packet': packet,
                'createdByRequest': request, 'signature': signature, 'state': 'queued', 'nextSafeAction': 'Plan within the approved scope.',
                'sourceEvidence': [], 'history': [], 'corrections': 0, 'calls': 0, 'createdAt': now, 'updatedAt': now}
        with self.store.tx() as db:
            previous = db.execute('SELECT * FROM item WHERE id=?', (canonical,)).fetchone()
            if previous:
                value = json.loads(previous['data']).get(KEY, {})
                if value.get('createdByRequest') == request and value.get('signature') == signature:
                    return self.view(previous, value)
                raise Conflict('Canonical item already exists; use its existing owner and record')
            if 'crosswalkReviewer' in packet:
                self.crosswalk.require_verified_recipient(packet['crosswalkReviewer'])
            if packet.get('autoCrosswalkReview'):
                flow['crosswalkGrant'] = self.crosswalk.route(packet['crosswalkReviewer'])
            if db.execute("SELECT count(*) FROM item WHERE kind='manager' AND state NOT IN ('completed','cancelled')").fetchone()[0] >= 150:
                raise Conflict('Manager queue is full; resolve or cancel existing work first')
            db.execute("INSERT INTO item(id,kind,data,state) VALUES(?,'manager',?,'manager-active')",
                       (canonical, json_value({KEY: flow})))
        self.log.emit('manager_intake', 'manager', command=packet['capability'], outcome='ok')
        self.wake()
        return {**flow, 'revision': 1, 'owner': None}

    def row(self, db, task):
        row = db.execute("SELECT * FROM item WHERE id=? AND kind='manager'", (id_value(task, 'task ID'),)).fetchone()
        if not row:
            raise Conflict('Manager task not found')
        return row, json.loads(row['data'])[KEY]

    def view(self, row, flow):
        return {**flow, 'revision': row['revision'], 'owner': row['owner'], 'leaseUntil': row['until']}

    def update(self, db, row, flow, owner=None, until=None):
        flow['updatedAt'] = time.time()
        state = {'done': 'completed', 'cancelled': 'cancelled', 'blocked': 'manager-blocked',
                 'unknown': 'manager-unknown'}.get(flow['state'], 'manager-active')
        data = json_value({KEY: flow})
        if len(data.encode()) > 32768:
            raise Conflict('Manager task exceeds the journal record limit')
        db.execute('UPDATE item SET data=?,state=?,owner=?,until=?,revision=revision+1 WHERE id=?',
                   (data, state, owner, until, row['id']))
        return {**flow, 'revision': row['revision'] + 1, 'owner': owner, 'leaseUntil': until}

    def task_revision(self, row, body):
        if type(body.get('revision')) is not int or body['revision'] != row['revision']:
            raise Conflict('Task revision changed; refresh before continuing')

    def event(self, flow, state, actor, detail):
        flow['history'] = (flow['history'] + [{'at': time.time(), 'state': state, 'actor': actor, 'detail': detail}])[-18:]

    def dashboard(self):
        from .server import iso
        with self.store.lock:
            db = self.store.db
            goals = self.goals(db)
            records = db.execute("SELECT * FROM item WHERE kind='manager' ORDER BY rowid DESC LIMIT 80").fetchall()
            usage = self.settings(db, 'usage', {})
        views = []
        overall = Counter()
        now = time.time()
        for row in records:
            flow = json.loads(row['data'])[KEY]
            score, factors = rank(flow, goals, now)
            overall[flow['state']] += 1
            goal = goals[flow['packet']['goal']]
            if flow['state'] == 'done':
                goal['verifiedOutcomes'] = goal.get('verifiedOutcomes', 0) + 1
            # Detailed results are fetched on demand rather than every HUD refresh.
            dispatch = flow.get('crosswalk')
            if dispatch and 'replyText' in dispatch:
                dispatch = {key: val for key, val in dispatch.items() if key != 'replyText'}
            views.append({'taskId': row['id'], 'objective': flow['packet']['objective'], 'capability': flow['packet']['capability'],
                          'goal': goal['id'], 'state': flow['state'], 'revision': row['revision'], 'owner': row['owner'],
                          'score': score, 'factors': factors, 'corrections': flow['corrections'], 'updatedAt': flow['updatedAt'],
                          'blocker': flow.get('blocker'), 'nextSafeAction': flow['nextSafeAction'],
                          'relay': flow.get('relay'), 'crosswalk': dispatch,
                          'crosswalkReviewer': flow['packet'].get('crosswalkReviewer')})
        views.sort(key=lambda row: (row['state'] not in ACTIVE, -row['score'], -row['updatedAt']))
        roster = [{'id': 'muse-' + name, 'provider': 'Muse', 'account': account, 'role': role,
                   'enrollment': 'Controller route configured' if name == 'main' else 'Needs human consent',
                   'capabilities': ['existing restricted Main route'] if name == 'main' else []}
                  for name, (account, role) in MUSE.items()]
        proof = self.monitor.connections.saved('hermes')
        roster.append({'id': ACTOR, 'provider': 'Hermes', 'account': proof.get('account', 'OpenAI Codex OAuth'),
                       'role': 'Planning, research synthesis and quality review',
                       'enrollment': proof.get('status', 'Needs human consent'), 'capabilities': list(CAPABILITIES)})
        roster.extend({'id': id, 'provider': label, 'account': label, 'role': role, 'enrollment': 'Receipt unverified',
                       'capabilities': []} for id, label, role in (
                           ('codex', 'Codex', 'Coding handoff'), ('relay', 'ChatGPT', 'Research Relay consumer')))
        return {'schema': 1, 'observedAt': iso(), 'goals': list(goals.values()), 'board': views,
                'relayTarget': self.relay.status(), 'crosswalkTargets': self.crosswalk.capable(),
                'crosswalkAssignment': True, 'autoCrosswalkReview': True,
                'roster': roster, 'capabilities': CAPABILITIES, 'metrics': {'recentStates': dict(overall),
                'recentItems': len(views), 'reservedCallsToday': usage.get('calls', 0) if usage.get('day') == time.strftime('%Y-%m-%d', time.gmtime()) else 0,
                'dailyCallLimit': DAILY_CALL_LIMIT, 'modelLatencyMs': proof.get('elapsedMs'), 'scope': 'Last 80 tasks; local observations'}}

    def operator(self, actor, body):
        if actor['id'] not in HUMANS or actor.get('role') != 'human':
            raise Denied('Manager operator access required')
        if body is None:
            return self.dashboard()
        action = body.get('action')
        if action == 'goal':
            result = self.goal(body)
            self.wake()
            return result
        if action == 'create':
            return self.create(body)
        if action in ('relay-publish', 'relay-reconcile'):
            return self.relay.request(body)
        if action == 'relay-configure':
            return self.relay.configure(body)
        if action == 'crosswalk-check':
            return self.crosswalk.check()
        if action == 'crosswalk-start':
            return self.crosswalk.begin(body.get('role'))
        if action == 'crosswalk-reconcile':
            return self.crosswalk.reconcile(body.get('role'))
        if action == 'crosswalk-dispatch':
            return self.crosswalk.dispatch(body)
        if action == 'crosswalk-dispatch-reconcile':
            return self.crosswalk.reconcile_dispatch(body.get('taskId'))
        if action == 'read':
            with self.store.lock:
                return self.view(*self.row(self.store.db, body.get('taskId')))
        if action not in ('cancel', 'retry'):
            raise Conflict('Unknown manager operator action')
        with self.store.tx() as db:
            row, flow = self.row(db, body.get('taskId'))
            self.task_revision(row, body)
            if row['owner'] and row['until'] > time.time():
                raise Conflict('An active worker holds this item; wait for its response or lease expiry')
            if flow['state'] in ('done', 'cancelled'):
                raise Conflict('Terminal manager item cannot be replayed')
            if action == 'retry':
                if flow['state'] not in ('blocked', 'unknown') or flow['calls'] >= 6:
                    raise Conflict('Only blocked or unknown analysis within the per-task budget can be reopened')
                flow['state'] = flow.pop('resumeState', 'queued')
                flow['nextSafeAction'] = 'Resume the prior bounded analysis within its original scope.'
            else:
                flow['state'] = 'cancelled'
                flow['nextSafeAction'] = 'No further execution.'
            flow.pop('blocker', None)
            flow.pop('run', None)
            self.event(flow, flow['state'], actor['id'], 'Operator ' + action)
            value = self.update(db, row, flow)
        if action == 'retry':
            self.wake()
        return value

    def claim(self, actor):
        now = time.time()
        proof = self.monitor.connections.saved('hermes')
        with self.store.tx() as db:
            for row in db.execute("SELECT * FROM item WHERE kind='manager' AND owner=? AND until<=?", (actor['id'], now)).fetchall():
                flow = json.loads(row['data'])[KEY]
                flow['resumeState'] = flow['state']
                flow['state'] = 'unknown'
                flow['blocker'] = 'Worker response missing after lease expiry. Check evidence before an explicit retry.'
                flow['nextSafeAction'] = 'Inspect worker logs and retry explicitly if the inference call can be repeated.'
                self.event(flow, 'unknown', 'controller', 'Lease expired without a worker receipt')
                self.update(db, row, flow)
            if db.execute("SELECT 1 FROM item WHERE kind='manager' AND owner=? AND until>?", (actor['id'], now)).fetchone():
                return {'task': None, 'reason': 'Hermes already has a live claim'}
            goals = self.goals(db)
            candidates = []
            for row in db.execute("SELECT * FROM item WHERE kind='manager' AND state='manager-active' AND owner IS NULL"):
                flow = json.loads(row['data'])[KEY]
                if flow['state'] in ACTIVE and goals[flow['packet']['goal']]['enabled']:
                    score, factors = rank(flow, goals, now)
                    candidates.append((score, -flow['createdAt'], row, flow, factors))
            if not candidates:
                return {'task': None, 'reason': 'No ready manager work'}
            day = time.strftime('%Y-%m-%d', time.gmtime(now))
            usage = self.settings(db, 'usage', {})
            used = usage.get('calls', 0) if usage.get('day') == day else 0
            if used >= DAILY_CALL_LIMIT:
                return {'task': None, 'reason': 'Daily inference call ceiling reached'}
            _, _, row, flow, factors = max(candidates, key=lambda candidate: candidate[:2])
            if flow['calls'] >= 6:
                flow['state'] = 'blocked'
                flow['blocker'] = 'Task inference call ceiling reached.'
                flow['nextSafeAction'] = 'Inspect completed stages and decide whether to close the item.'
                self.update(db, row, flow)
                return {'task': None, 'reason': 'Task inference call ceiling reached'}
            proof_age = now - proof.get('verifiedAt', 0)
            if proof.get('status') != 'Operational' or proof_age > PROVIDER_PROOF_SECONDS:
                if proof.get('status') == 'Unavailable' and proof_age < FAILED_PROBE_COOLDOWN:
                    return {'task': None, 'reason': 'Hermes inference verification is backing off after a failed probe'}
                return {'task': None, 'probeRequired': True, 'reason': 'Queued work requires a fresh Hermes inference probe'}
            phase = PHASE[flow['state']]
            accounting = uuid.uuid4().hex
            lease = dict(runId=accounting, attemptId=uuid.uuid4().hex, fencingId=uuid.uuid4().hex,
                         workerId=actor['id'], provider='hermes', providerAccountId=proof.get('providerAccountId', 'oauth-verified'),
                         roleId='manager-review', workspaceId=None, phase=phase)
            flow['run'] = lease
            flow['state'] = 'planning' if phase == 'plan' else flow['state']
            flow['calls'] += 1
            flow['rankFactors'] = factors
            flow['nextSafeAction'] = 'Wait for the owned ' + phase + ' receipt.'
            self.event(flow, 'claimed', actor['id'], phase)
            self.save(db, 'usage', {'day': day, 'calls': used + 1})
            result = self.update(db, row, flow, actor['id'], now + LEASE_SECONDS)
        if phase == 'plan' and flow['packet']['sources']:
            evidence = [self.research.get(url) for url in flow['packet']['sources']]
            with self.store.tx() as db:
                row, flow = self.owned(db, actor, result)
                flow['sourceEvidence'] = evidence
                if not any(entry['availability'] == 'available' for entry in evidence):
                    flow['resumeState'] = flow['state']
                    flow['state'] = 'blocked'
                    flow['blocker'] = 'No approved public source could be collected.'
                    flow['nextSafeAction'] = 'Inspect access or provide a permitted readable public source.'
                    self.update(db, row, flow)
                    return {'task': None, 'reason': flow['blocker']}
                result = self.update(db, row, flow, actor['id'], row['until'])
        packet = {key: value for key, value in result['packet'].items()
                  if key not in ('crosswalkReviewer', 'autoCrosswalkReview')}
        return {'task': {'taskId': result['taskId'], 'canonicalItemId': result['canonicalItemId'],
                         'revision': result['revision'], 'run': lease, 'leaseUntil': result['leaseUntil'],
                         'packet': packet, 'constraints': goals[packet['goal']]['constraints'],
                         'evidence': [{k: v for k, v in evidence.items() if k not in ('etag', 'lastModified')}
                                      for evidence in result['sourceEvidence']],
                         'plan': result.get('plan'), 'result': result.get('result'),
                         'review': result.get('review'), 'corrections': result['corrections']}}

    def owned(self, db, actor, body):
        row, flow = self.row(db, body.get('taskId'))
        self.task_revision(row, body)
        claimed = flow.get('run') or {}
        provided = body.get('run') or {}
        if row['owner'] != actor['id'] or (row['until'] or 0) <= time.time() or any(
                not claimed.get(k) or claimed[k] != provided.get(k) for k in ('runId', 'attemptId', 'fencingId')):
            raise Denied('Current task lease and exact run fencing required')
        return row, flow

    def result_value(self, flow, payload):
        packet = flow['packet']
        summary = text(payload.get('summary'), 'output summary', 1600)
        if packet['capability'] == 'email-review':
            return {'summary': summary, 'draft': text(payload.get('draft'), 'reviewed draft', 3000),
                    'issues': strings(payload.get('issues', []), 'draft issue', 8, 250)}
        allowed = {row['url']: row['sha256'] for row in flow['sourceEvidence'] if row['availability'] == 'available'}
        citations = strings(payload.get('citations', []), 'citation', 3, 700)
        if any(url not in allowed for url in citations):
            raise Conflict('Every citation must point to exact controller-collected evidence')
        return {'summary': summary, 'citations': [{'url': url, 'sha256': allowed[url]} for url in citations]}

    def submit(self, actor, body):
        payload = body.get('result')
        if not isinstance(payload, dict):
            raise Conflict('Bounded worker result object required')
        with self.store.tx() as db:
            row, flow = self.owned(db, actor, body)
            phase = flow['run']['phase']
            if phase == 'plan':
                flow['plan'] = {'approach': text(payload.get('approach'), 'plan', 1000),
                                'checks': strings(payload.get('checks', []), 'proposed check', 6, 240),
                                'selectedWorker': ACTOR, 'selectionReason': 'Verified scoped analysis worker; no browser or outbound action required.'}
                flow['state'] = 'working'
                flow['nextSafeAction'] = 'Produce the bounded artifact from approved input.'
            elif phase == 'work':
                flow['result'] = self.result_value(flow, payload)
                flow['state'] = 'reviewing'
                flow['nextSafeAction'] = 'Evaluate the draft independently against the exact criteria.'
            else:
                verdict = payload.get('verdict')
                if verdict not in ('accept', 'revise'):
                    raise Conflict('Reviewer must give an accept or revise verdict')
                feedback = text(payload.get('feedback'), 'review evidence', 1400)
                criteria = strings(payload.get('criteriaMet', []), 'satisfied criterion', 6, 300)
                approved = set(criteria) == set(flow['packet']['criteria'])
                if flow['packet']['capability'] == 'research' and not flow['result'].get('citations'):
                    approved = False
                    feedback = 'Add at least one exact citation from the collected evidence. ' + feedback
                if flow['packet']['capability'] == 'email-review' and flow['result']['draft'].strip() == flow['packet']['draft'].strip() and flow['result']['issues']:
                    approved = False
                    feedback = 'The output lists issues but repeats the original draft verbatim. ' + feedback
                verdict = 'accept' if verdict == 'accept' and approved else 'revise'
                flow['review'] = {'verdict': verdict, 'feedback': feedback[:1500], 'criteriaMet': criteria,
                                  'reviewedAt': time.time(), 'runId': flow['run']['runId']}
                if verdict == 'accept':
                    flow['state'] = 'done'
                    flow['nextSafeAction'] = ('The assigned Crosswalk review is queued for delivery.'
                                              if flow['packet'].get('autoCrosswalkReview') else
                                              'Read or copy the verified result. Any external action needs separate authority.')
                    if flow['packet'].get('autoCrosswalkReview'):
                        self.crosswalk.queue_automatic(db, flow)
                elif flow['corrections'] < REVISION_LIMIT:
                    flow['corrections'] += 1
                    flow['state'] = 'working'
                    flow['nextSafeAction'] = 'Correct the specific review findings once.'
                else:
                    flow['state'] = 'blocked'
                    flow['resumeState'] = 'reviewing'
                    flow['blocker'] = 'The correction limit was reached without meeting acceptance criteria.'
                    flow['nextSafeAction'] = 'Inspect the reviewed result and resolve the specific criteria.'
            self.event(flow, flow['state'], actor['id'], 'Recorded ' + phase + ' receipt')
            value = self.update(db, row, flow)
        self.log.emit('manager_receipt', 'manager', runId=value['run']['runId'], command=phase, outcome=value['state'])
        if value['state'] == 'done' and value['packet'].get('autoCrosswalkReview'):
            self.crosswalk.deliver_automatic(row['id'])
            with self.store.lock:
                value = self.view(*self.row(self.store.db, row['id']))
        return {'taskId': row['id'], 'state': value['state'], 'revision': value['revision']}

    def worker(self, actor, body):
        if actor['id'] != ACTOR or actor.get('role') != 'manager-worker':
            raise Denied('Dedicated Hermes adapter authentication required')
        action = body.get('action')
        if action in ('provider-ready', 'provider-unavailable'):
            if action == 'provider-ready':
                elapsed = bounded_number(body.get('elapsedMs'), 'Inference latency', 1, 120000)
                account = id_value(body.get('providerAccountId'), 'provider credential identifier')
                model = id_value(body.get('model').replace('.', '_') if isinstance(body.get('model'), str) else None, 'model')
                self.monitor.connections.record('hermes', 'Operational', 'Codex OAuth inference and scoped adapter verified.',
                                                account='OpenAI Codex subscription', providerAccountId=account, model=model, elapsedMs=elapsed)
                self.wake()
            else:
                self.monitor.connections.record('hermes', 'Unavailable', 'The installed adapter could not verify inference. Inspect the Hermes probe and account entitlement.')
            return {'accepted': True}
        if action == 'claim':
            if pending := self.crosswalk.next_automatic():
                self.crosswalk.deliver_automatic(pending)
                return {'task': None, 'deliveryProcessed': True}
            return self.claim(actor)
        if action == 'result':
            return self.submit(actor, body)
        if action not in ('heartbeat', 'failed'):
            raise Conflict('Unknown manager worker operation')
        with self.store.tx() as db:
            row, flow = self.owned(db, actor, body)
            if action == 'heartbeat':
                until = time.time() + LEASE_SECONDS
                db.execute('UPDATE item SET until=? WHERE id=?', (until, row['id']))
                return {'revision': row['revision'], 'leaseUntil': until}
            flow['resumeState'] = flow['state']
            flow['state'] = 'unknown'
            flow['blocker'] = 'The adapter could not verify an inference or result receipt.'
            flow['nextSafeAction'] = 'Inspect the provider response and explicitly retry within the recorded usage limit.'
            self.event(flow, 'unknown', actor['id'], 'Unverified provider result')
            self.update(db, row, flow)
            return {'state': 'unknown'}
