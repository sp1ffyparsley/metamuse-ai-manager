"""Optional Crosswalk transport and identity-specific connection receipts."""
import json
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.request import Request, build_opener

from .connections import MUSE
from .research import NoRedirect, source_url
from .store import Conflict, Denied

NUMBER = re.compile(r'\d{4}-\d{4}-\d{4}-\d{4}')
SLUG = re.compile(r'[a-z0-9][a-z0-9-]{1,62}')
POST_ID = re.compile(r'[A-Za-z0-9_-]{4,100}')


def decode_result(payload):
    if payload.get('error') or payload.get('result', {}).get('isError'):
        raise Conflict('Crosswalk refused the tool operation. Check the private board and key scope.')
    result = payload.get('result', {})
    if isinstance(result.get('structuredContent'), (dict, list)):
        return result['structuredContent']
    parts = result.get('content', [])
    value = '\n'.join(c['text'] for c in parts if c.get('type') == 'text' and isinstance(c.get('text'), str))
    value = value.strip()
    if value.startswith('```') and value.endswith('```'):
        value = value.split('\n', 1)[-1].rsplit('```', 1)[0]
    try:
        return json.loads(value)
    except ValueError:
        return {'text': value}


def objects(value):
    if isinstance(value, dict):
        yield value
        for entry in value.values():
            yield from objects(entry)
    if isinstance(value, list):
        for entry in value:
            yield from objects(entry)


class Board:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.settings = manager.config.get('crosswalk', {})
        self.opener = build_opener(NoRedirect)

    def credential(self):
        path = self.settings.get('credentialFile')
        if not path:
            raise Conflict('Install the board-scoped Hermes key in the private Ubuntu controller store')
        fd = os.open(Path(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise Denied('Crosswalk key owner or permissions are unsafe')
            result = stream.read(513).strip()
        if not re.fullmatch(r'[A-Za-z0-9._~+-]{20,512}', result):
            raise Conflict('Crosswalk secret format is invalid')
        return result

    def call(self, tool, arguments):
        spec = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool, 'arguments': arguments}}
        request = Request('https://mcp.crosswalk.to/', method='POST', data=json.dumps(spec).encode(),
                          headers={'Authorization': 'Bearer ' + self.credential(), 'Content-Type': 'application/json',
                                   'Accept': 'application/json, text/event-stream', 'User-Agent': 'MetaMuse/1'})
        try:
            with self.opener.open(request, timeout=12) as response:
                if response.headers.get_content_type() == 'text/event-stream':
                    size = 0
                    while line := response.readline(65537):
                        size += len(line)
                        if size > 262144:
                            raise ValueError('Crosswalk response exceeded its bound')
                        if line.startswith(b'data:'):
                            value = json.loads(line[5:])
                            if value.get('id') == 1:
                                return decode_result(value)
                    raise ValueError('Crosswalk event stream contained no tool receipt')
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError('Crosswalk response exceeded its bound')
            return decode_result(json.loads(raw))
        except (OSError, ValueError) as error:
            raise Conflict('Crosswalk response unavailable. Verify credential and reconcile any prior post.') from error

    def private_board(self):
        board = self.settings.get('board')
        own = self.settings.get('number')
        if not isinstance(board, str) or not SLUG.fullmatch(board) or not isinstance(own, str) or not NUMBER.fullmatch(own):
            raise Conflict('Pin the private board slug and Hermes provider number')
        result = self.call('crosswalks', {'action': 'list'})
        if not any(entry.get('slug') == board and
                   (entry.get('visibility') == 'private' or entry.get('private') is True or entry.get('is_private') is True)
                   for entry in objects(result)):
            raise Denied('Crosswalk could not provide a structured private-board match')
        identity = self.call('account', {'action': 'numbers'})
        # The provider's `you` is the authenticated caller. A number elsewhere
        # in its roster can belong to Main and is not proof of this key's identity.
        if not isinstance(identity, dict) or identity.get('you') != own:
            raise Denied('Hermes key identity does not match the pinned Crosswalk number')
        rules = self.call('account', {'action': 'rules'})
        key = rules.get('key') if isinstance(rules, dict) else None
        if not isinstance(key, dict) or key.get('scope') != 'full' or key.get('enters') != 'to/' + board:
            raise Denied('Hermes key must allow reads and posts in only the pinned private board')
        return board, own

    def check(self):
        board, own = self.private_board()
        self.manager.monitor.connections.record('crosswalk-hermes', 'Tested read',
                                                'Hermes key identity and private board membership matched.', providerAccountId=own)
        return {'board': board, 'providerAccountId': own}

    def recipient(self, role, own):
        if role not in MUSE:
            raise Conflict('Choose one of the four existing Muse roles')
        numbers = self.settings.get('workers', {})
        worker = numbers.get(role)
        if not isinstance(worker, str) or not NUMBER.fullmatch(worker) or worker == own or list(numbers.values()).count(worker) != 1:
            raise Conflict('Pin a distinct Crosswalk number for this authenticated Muse role')
        return worker

    def capable(self):
        own = self.settings.get('number')
        if not isinstance(own, str) or not NUMBER.fullmatch(own):
            return []
        roles = []
        for role, (label, _) in MUSE.items():
            try:
                number = self.recipient(role, own)
            except Conflict:
                continue
            proof = self.manager.monitor.connections.saved('crosswalk-' + role)
            if (proof.get('status') == 'Tested write' and proof.get('providerAccountId') == number and
                    proof.get('board') == self.settings.get('board') and proof.get('sender') == own):
                roles.append({'id': role, 'label': label})
        return roles

    def require_verified_recipient(self, role):
        if role not in {entry['id'] for entry in self.capable()}:
            raise Denied('Verify this account’s Crosswalk identity and reply before assigning work.')

    def route(self, role):
        own = self.settings.get('number')
        return {'board': self.settings.get('board'), 'sender': own,
                'worker': self.recipient(role, own), 'role': role}

    def begin(self, role):
        if role not in MUSE:
            raise Conflict('Choose one of the four existing Muse roles')
        board, own = self.private_board()
        worker = self.recipient(role, own)
        operation = 'crosswalk-probe-' + role
        with self.store.tx() as db:
            old = db.execute('SELECT state,data,result FROM outbox WHERE id=?', (operation,)).fetchone()
            if old:
                return self.result(old)
            record = {'role': role, 'board': board, 'worker': worker, 'sender': own, 'correlation': secrets.token_hex(12)}
            db.execute("INSERT INTO outbox(id,kind,data,state,attempts) VALUES(?,'manager-crosswalk',?,'uncertain',1)",
                       (operation, json.dumps(record)))
        nonce = record['correlation']
        reply = 'METAMUSE ACK ' + nonce
        return self.post_reserved(operation, record, 'METAMUSE CONNECTION TEST ' + nonce,
                                  'Connection verification only. Reply in this thread with the exact brief ' + reply +
                                  '. Take no other action.')

    def post_reserved(self, operation, record, brief, body):
        post = self.call('post', {'crosswalk': record['board'], 'for': record['worker'], 'brief': brief,
                                  'body': body, 'idempotency_key': operation + '-' + record['correlation']})
        found = next((entry.get('post_id') or entry.get('id') for entry in objects(post)
                      if isinstance(entry.get('post_id') or entry.get('id'), str)), None)
        if not found or not POST_ID.fullmatch(found):
            return {**record, 'state': 'unknown',
                    'detail': 'Crosswalk did not provide a structured post ID. Reconcile before another post.'}
        record['postId'] = found
        with self.store.tx() as db:
            db.execute("UPDATE outbox SET state='posted',data=? WHERE id=? AND state='uncertain'", (json.dumps(record), operation))
        return {**record, 'state': 'posted'}

    @staticmethod
    def result(row):
        return {'state': 'unknown' if row['state'] == 'uncertain' else row['state'],
                **json.loads(row['data']), **(json.loads(row['result']) if row['result'] else {})}

    def reconcile(self, role):
        if role not in MUSE:
            raise Conflict('Choose one of the four Muse roles')
        board, own = self.private_board()
        operation = 'crosswalk-probe-' + role
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM outbox WHERE id=?', (operation,)).fetchone()
        if not row:
            raise Conflict('Start the permitted connection test first')
        state = self.result(row)
        if state['board'] != board or state['sender'] != own or state['worker'] != self.settings.get('workers', {}).get(role):
            raise Denied('The enrolled identity or destination changed after posting')
        if row['state'] == 'synced':
            return state
        post_id, receipt = self.find_reply(state, 'METAMUSE CONNECTION TEST ', 'METAMUSE ACK ')
        if not receipt:
            if post_id:
                record = json.loads(row['data'])
                record['postId'] = post_id
                with self.store.tx() as db:
                    db.execute("UPDATE outbox SET state='posted',data=? WHERE id=? AND state='uncertain'",
                               (json.dumps(record), operation))
                self.manager.monitor.connections.record(
                    'crosswalk-' + role, 'Permission limited',
                    'Connection test posted; awaiting an authenticated reply on the same post.',
                    providerAccountId=state['worker'], board=board, sender=own, postId=post_id)
                return {**state, 'state': 'posted', 'postId': post_id}
            return state
        verified = {'state': 'verified', 'postId': post_id, 'ackId': receipt['id']}
        with self.store.tx() as db:
            db.execute("UPDATE outbox SET state='synced',result=? WHERE id=?", (json.dumps(verified), operation))
        connections = self.manager.monitor.connections
        connections.record('crosswalk-' + role, 'Tested write', 'Exact role-specific Crosswalk reply matched the private connection test.',
                           providerAccountId=state['worker'], board=state['board'], sender=own)
        connections.record('crosswalk-hermes', 'Tested write', 'Hermes posted privately and read an authenticated correlated reply.',
                           providerAccountId=own)
        return {**state, **verified}

    def board_feed(self, state):
        """Use provider metadata; post bodies and rendered headers are never receipts."""
        feed = self.call('read', {'action': 'all', 'crosswalk': state['board'], 'peek': True, 'budget_tokens': 4000})
        if not isinstance(feed, dict) or feed.get('kind') != 'feed' or not isinstance(feed.get('feed'), list):
            return []
        return [row for row in feed['feed'] if isinstance(row, dict) and row.get('crosswalk') == state['board']]

    def find_reply(self, state, request_prefix, reply_prefix):
        nonce = state['correlation']
        id = state.get('postId')
        feed = None
        if not id:
            lookup = self.call('read', {'action': 'search', 'crosswalk': state['board'], 'query': nonce, 'budget_tokens': 2000})
            id = next((entry.get('id') for entry in objects(lookup)
                       if entry.get('via_number') == state['sender'] and entry.get('brief') == request_prefix + nonce), None)
            if not id:
                feed = self.board_feed(state)
                matches = {row['id'] for row in feed if isinstance(row.get('id'), str) and POST_ID.fullmatch(row['id'])
                           and row.get('via_number') == state['sender'] and row.get('for_number') == state['worker']
                           and row.get('brief') == request_prefix + nonce}
                id = next(iter(matches)) if len(matches) == 1 else None
        if not isinstance(id, str) or not POST_ID.fullmatch(id):
            return None, None
        thread = self.call('open', {'ids': [id], 'thread': True, 'peek': True})
        receipt = next((entry for entry in objects(thread) if entry.get('via_number') == state['worker']
                        and entry.get('brief') == reply_prefix + nonce and entry.get('reply_to') == id
                        and isinstance(entry.get('id'), str) and POST_ID.fullmatch(entry['id'])), None)
        if not receipt and isinstance(thread, dict) and isinstance(thread.get('text'), str):
            feed = self.board_feed(state) if feed is None else feed
            receipt = next((row for row in feed if row.get('via_number') == state['worker']
                            and row.get('brief') == reply_prefix + nonce and row.get('reply_to') == id
                            and isinstance(row.get('id'), str) and row['id'] != id and POST_ID.fullmatch(row['id'])), None)
        return id, receipt

    def dispatch(self, body, *, automatic=False):
        """Send one operator-authorized public-source review to an enrolled role."""
        import hashlib
        task, role = body.get('taskId'), body.get('role')
        self.require_verified_recipient(role)
        board, own = self.private_board()
        worker = self.recipient(role, own)
        with self.store.lock:
            _, flow = self.manager.row(self.store.db, task)
        if flow['state'] != 'done' or flow['packet']['capability'] != 'research':
            raise Conflict('Only a verified public research task can be dispatched for a second review.')
        sources = [source_url(source['url']) for source in flow['result']['citations']]
        if not sources:
            raise Conflict('The research task has no verified public sources.')
        operation = 'crosswalk-review-' + hashlib.sha256(task.encode()).hexdigest()
        with self.store.tx() as db:
            row, current = self.manager.row(db, task)
            if current.get('crosswalk'):
                if current['crosswalk']['role'] != role:
                    raise Conflict('This task already has a Crosswalk recipient')
                return current['crosswalk']
            self.manager.task_revision(row, body)
            assigned = current['packet'].get('crosswalkReviewer')
            if assigned is not None and assigned != role:
                raise Conflict('Send this review to the Muse account assigned to the task')
            if automatic and current.get('crosswalkGrant') != {
                    'board': board, 'sender': own, 'worker': worker, 'role': role}:
                raise Denied('The previously authorized Crosswalk route has changed')
            record = {'board': board, 'sender': own, 'worker': worker, 'role': role, 'taskId': task,
                      'operationId': operation, 'state': 'unknown', 'correlation': secrets.token_hex(12)}
            current['crosswalk'] = record
            if automatic:
                current['nextSafeAction'] = 'Inspect Crosswalk delivery status in the task details.'
            self.manager.update(db, row, current)
            db.execute("INSERT INTO outbox(id,kind,data,state,attempts) VALUES(?,'manager-crosswalk',?,'uncertain',1)",
                       (operation, json.dumps(record)))
        correlation = record['correlation']
        instruction = ('Public-source review only. Cross-check the sources below, and summarize any disagreement or uncertainty. '
                       'Take no action on any account or user data. Reply in this thread using the exact brief METAMUSE RESULT ' +
                       correlation + ' and put the reviewed findings in the reply body.\n' + '\n'.join(sources))
        posted = self.post_reserved(operation, record, 'METAMUSE REVIEW ' + correlation, instruction)
        if posted['state'] == 'posted':
            return self.dispatch_state(task, posted)
        return posted

    def dispatch_state(self, task, value):
        with self.store.tx() as db:
            row, flow = self.manager.row(db, task)
            current = flow.get('crosswalk', {})
            if current.get('operationId') != value.get('operationId'):
                raise Conflict('The recorded research dispatch changed.')
            if current.get('state') == 'reply-verified':
                return current
            flow['crosswalk'] = value
            self.manager.update(db, row, flow)
            db.execute('UPDATE outbox SET state=?,result=? WHERE id=?',
                       ('synced' if value['state'] == 'reply-verified' else 'posted', json.dumps(value), value['operationId']))
        return value

    def reconcile_dispatch(self, task):
        with self.store.lock:
            _, flow = self.manager.row(self.store.db, task)
        record = flow.get('crosswalk')
        if not record:
            raise Conflict('This task has no Crosswalk dispatch.')
        if record['state'] == 'reply-verified':
            return record
        board, own = self.private_board()
        if (board, own, self.recipient(record['role'], own)) != (record['board'], record['sender'], record['worker']):
            raise Denied('The enrolled board or account changed after dispatch.')
        parent, receipt = self.find_reply(record, 'METAMUSE REVIEW ', 'METAMUSE RESULT ')
        if receipt:
            return self.dispatch_state(task, {**record, 'postId': parent, 'replyId': receipt['id'],
                                              'state': 'reply-verified'})
        if parent and record.get('postId') != parent:
            return self.dispatch_state(task, {**record, 'postId': parent, 'state': 'posted'})
        return record
