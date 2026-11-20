"""Explicit Research Relay publication in the approved Drive folder."""
import hashlib
import json
import re
from urllib.parse import quote

from .store import Conflict, Denied

DRIVE_ID = re.compile(r'[A-Za-z0-9_-]{15,150}')
CONSUMER_ID = re.compile(r'[A-Za-z0-9_-]{8,150}')


class Relay:
    def __init__(self, manager):
        self.manager = manager
        self.monitor = manager.monitor
        self.store = manager.store

    def settings(self):
        with self.store.lock:
            return self.manager.settings(self.store.db, 'relay-target', self.manager.config.get('relay', {}))

    def status(self):
        spec = self.settings()
        return {'folderId': spec.get('folderId'), 'consumerTaskId': spec.get('consumerTaskId'),
                'revision': spec.get('revision', 0), 'configured': bool(spec.get('folderId') and spec.get('consumerTaskId'))}

    def verified_google(self):
        if not self.monitor.google or self.monitor.connections.saved('google').get('status') != 'Tested read':
            raise Conflict('Verify the existing Google runtime account before configuring or publishing a Relay')

    def configure(self, body):
        self.verified_google()
        folder, consumer, revision = body.get('folderId'), body.get('consumerTaskId'), body.get('revision')
        if not (isinstance(folder, str) and DRIVE_ID.fullmatch(folder)
                and isinstance(consumer, str) and CONSUMER_ID.fullmatch(consumer) and type(revision) is int):
            raise Conflict('An approved folder ID and the exact scheduled task ID are required')
        self.verify_folder(folder)
        with self.store.tx() as db:
            current = self.manager.settings(db, 'relay-target', self.manager.config.get('relay', {}))
            if current.get('revision', 0) != revision:
                raise Conflict('Relay target changed. Refresh before editing.')
            self.manager.save(db, 'relay-target', {'folderId': folder, 'consumerTaskId': consumer, 'revision': revision + 1})
        self.monitor.diagnostics.emit('relay_target', 'manager', outcome='ok')
        return self.status()

    def target(self):
        spec = self.settings()
        folder, consumer = spec.get('folderId'), spec.get('consumerTaskId')
        if not (isinstance(folder, str) and DRIVE_ID.fullmatch(folder)
                and isinstance(consumer, str) and CONSUMER_ID.fullmatch(consumer)):
            raise Conflict('Configure one approved private Drive folder and scheduled consumer ID')
        return folder, consumer

    def verify_folder(self, folder):
        api = self.monitor.google
        safe = quote(folder, safe='')
        exact = api.call('drive', f'files/{safe}?fields=id,mimeType,trashed,capabilities(canAddChildren)')
        if (exact.get('id') != folder or exact.get('trashed') or
                exact.get('mimeType') != 'application/vnd.google-apps.folder' or
                exact.get('capabilities', {}).get('canAddChildren') is not True):
            raise Denied('Approved Relay folder unavailable or not writable')
        grants = api.call('drive', f'files/{safe}/permissions?fields=nextPageToken,permissions(type)&pageSize=100')
        permissions = grants.get('permissions')
        if (not isinstance(permissions, list) or not permissions or grants.get('nextPageToken') or
                any(not isinstance(entry, dict) or entry.get('type') not in ('user', 'group') for entry in permissions)):
            raise Denied('Choose a private Relay folder without public or domain-wide access')

    def packet(self, flow, consumer):
        from .server import iso
        evidence = [{k: item[k] for k in ('url', 'sha256', 'observedAt')} for item in flow['sourceEvidence']
                    if item.get('availability') == 'available']
        obj = {'schema': 1, 'canonicalItemId': flow['canonicalItemId'], 'taskId': flow['taskId'],
               'question': flow['packet']['objective'], 'findings': flow['result']['summary'],
               'datedEvidence': evidence, 'review': flow['review']['feedback'], 'reviewedAt': iso(flow['review']['reviewedAt']),
               'consumerTaskId': consumer,
               'receipt': {'expectedAction': 'Record the exact task ID and evidence SHA-256 when the scheduled task reads this packet.'}}
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8') + b'\n'

    def request(self, body):
        action = body['action']
        api = self.monitor.google
        task = body.get('taskId')
        with self.store.lock:
            row, flow = self.manager.row(self.store.db, task)
        if flow['state'] != 'done' or flow['packet']['capability'] != 'research':
            raise Conflict('Relay accepts only verified research results')
        previous = flow.get('relay')
        if previous and previous['state'] == 'stored':
            return previous
        if action == 'relay-reconcile' and previous is None:
            raise Conflict('No Relay operation exists for this item')
        if action == 'relay-publish' and previous:
            raise Conflict('Publication already started; reconcile its immutable Drive ID')
        self.verified_google()
        if previous is None:
            folder, consumer = self.target()
        else:
            folder, consumer = previous['folderId'], previous['consumerTaskId']
        content = self.packet(flow, consumer)
        checksum = hashlib.sha256(content).hexdigest()
        if previous is None:
            self.manager.task_revision(row, body)
            self.verify_folder(folder)
            ids = api.call('drive', 'files/generateIds?count=1&space=drive').get('ids', [])
            if len(ids) != 1 or not isinstance(ids[0], str) or not DRIVE_ID.fullmatch(ids[0]):
                raise Conflict('Drive did not supply one valid file ID')
            operation = 'relay-' + hashlib.sha256(task.encode()).hexdigest()
            record = {'state': 'unknown', 'fileId': ids[0], 'consumerTaskId': consumer,
                      'operationId': operation, 'sha256': checksum, 'folderId': folder,
                      'detail': 'Drive write pending reconciliation. Scheduled consumer receipt unverified.'}
            with self.store.tx() as db:
                checked, current = self.manager.row(db, task)
                self.manager.task_revision(checked, body)
                if current.get('relay'):
                    raise Conflict('Existing Relay publication requires reconciliation')
                spec = self.manager.settings(db, 'relay-target', self.manager.config.get('relay', {}))
                if (spec.get('folderId'), spec.get('consumerTaskId')) != (folder, consumer):
                    raise Conflict('Relay target changed before publication. Confirm the new destination.')
                current['relay'] = record
                self.manager.update(db, checked, current)
                db.execute("INSERT INTO outbox(id,kind,data,state,attempts) VALUES(?,'manager-relay',?,'uncertain',1)",
                           (operation, json.dumps({'taskId': task, 'fileId': ids[0], 'sha256': checksum})))
            try:
                api.upload_text(ids[0], folder, 'MetaMuse Research ' + task + '.txt', content)
            except Exception:
                return record
        else:
            record = previous
            if record['sha256'] != checksum:
                raise Conflict('The prior Relay packet differs from the recorded checksum.')
        id = record['fileId']
        try:
            metadata = api.call('drive', 'files/' + quote(id, safe='') + '?fields=id,parents,trashed')
            if metadata.get('trashed') or metadata.get('parents') != [folder]:
                return record
            readback = api.call('drive', 'files/' + quote(id, safe='') + '?alt=media', raw=True)
            if metadata.get('id') != id or hashlib.sha256(readback).hexdigest() != checksum:
                return record
        except Exception:
            return record
        result = {**record, 'state': 'stored', 'detail': 'Drive content read back and checksum verified. Awaiting scheduled consumer receipt.',
                  'url': 'https://drive.google.com/open?id=' + id}
        with self.store.tx() as db:
            checked, current = self.manager.row(db, task)
            if current.get('relay', {}).get('operationId') != record['operationId']:
                raise Conflict('Relay state changed during readback')
            current['relay'] = result
            self.manager.update(db, checked, current)
            db.execute("UPDATE outbox SET state='synced',result=? WHERE id=?", (json.dumps(result), record['operationId']))
        return result
