"""Evidence-backed operator connections, stored in the controller journal."""
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .google_bridge import Google, GoogleUnavailable, MAIL_SCOPES
from .store import Conflict, Denied

HUMANS = {'windows-human', 'local-human'}
MUSE = {'main': ('Main', 'Applications'), 'alt': ('Alt', 'Opportunity Scout'),
        'dad': ('Dad', 'Tech & Inbox'), 'sister': ('Sister', 'Claims, Creative & Websites')}
MUSE_URL = 'https://muse.ai/'
CROSSWALK_URL = 'https://crosswalk.to/keys?for=muse'
RELAY_URL = 'https://chatgpt.com/schedules'
KNOWN_STATES = {'Not configured', 'Needs human consent', 'Authenticated', 'Permission limited',
                'Tested read', 'Tested write', 'Operational', 'Expired/revoked', 'Unavailable'}


class Connections:
    def __init__(self, monitor):
        self.monitor = monitor
        self.store = monitor.store
        self.settings = monitor.config.get('manager', {})
        self.refresh_lock = threading.Lock()

    @staticmethod
    def entry(id, label, account, scope, status, detail, url=None, **extra):
        return dict(id=id, label=label, account=account, scope=scope, status=status, detail=detail,
                    consentUrl=url, verifiedAt=None, **extra)

    def saved(self, name):
        with self.store.lock:
            row = self.store.db.execute('SELECT data FROM setting WHERE id=?', ('connection:' + name,)).fetchone()
        return json.loads(row['data']) if row else {}

    def record(self, name, status, detail, **evidence):
        if status not in KNOWN_STATES:
            raise ValueError('Unknown connection status')
        value = dict(status=status, detail=detail, verifiedAt=time.time(), **evidence)
        with self.store.tx() as db:
            db.execute('INSERT INTO setting VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       ('connection:' + name, json.dumps(value)))
        self.monitor.diagnostics.emit('connection_check', 'manager', command=name,
                                      outcome='ok' if status in ('Operational', 'Tested read', 'Tested write') else 'limited')
        return value

    def list(self, actor):
        from .server import iso
        if actor.get('role') != 'human' or actor['id'] not in HUMANS:
            raise Denied('Attended operator connection status required')
        e = self.entry
        rows = [e('controller', 'Ubuntu controller', actor['id'], 'Paired native controller HTTPS / local Unix',
                  'Operational', 'Authenticated response from the existing controller.'),
                e('github', 'Private GitHub repository', 'example-owner/muse-monitor', 'Private source and release access',
                  'Not configured', 'Verify using GitHub CLI in the signed-in Windows profile.',
                  'https://github.com/example-owner/muse-monitor'),
                e('development', 'Windows to Ubuntu development', 'muse-operator', 'Pinned key-only SSH on VMnet8 port 2223',
                  'Not configured', 'Verify from Windows through muse-workspace-dev.'),
                e('google', 'Google Drive, Docs & Sheets', 'Foot/cloud', 'Desktop OAuth; canonical writes guarded',
                  'Not configured' if self.monitor.google is None else 'Permission limited',
                  'Run Verify to check consent, account, and live mappings.' if self.monitor.google else 'Desktop bridge is not configured.',
                  'https://console.cloud.google.com/auth/overview'),
                e('gmail', 'Professional Gmail', 'Professional mailbox', 'Separate Gmail read-only OAuth',
                  'Not configured', 'Optional. Enable only with the mailbox owner’s consent.',
                  'https://console.cloud.google.com/auth/overview'),
                e('hermes', 'Hermes / OpenAI Codex', 'Attended OpenAI account', 'Subscription OAuth; scoped inference',
                  'Needs human consent', 'Complete the official Hermes device flow, then run the inference probe.',
                  'https://auth.openai.com/codex/device'),
                e('codex', 'Codex worker intake', 'Codex', 'Coding handoff; repository scope',
                  'Unavailable', 'No verified automated worker receipt.'),
                e('relay', 'ChatGPT Research Relay', 'Existing scheduled task owner', 'Private Drive packet with consumer readback',
                  'Unavailable', 'A scheduled consumer receipt has not been verified.', RELAY_URL),
                e('vault', 'AgentVault / Obsidian', 'Windows owner', 'Existing local Markdown vault; curated read-only use',
                  'Not configured', 'Verify the Windows vault without sending its contents.')]
        rows[0]['verifiedAt'] = time.time()
        for name, (account, role) in MUSE.items():
            scope = 'Native role and its existing schedules'
            detail = 'Separate account enrollment has not been verified.'
            status = 'Needs human consent'
            if name == 'main' and self.monitor.config['principals'].get('main', {}).get('role') == 'main':
                detail = 'Existing Main controller route configured. Live cloud intake and native scheduler require independent verification.'
                status = 'Permission limited'
            rows.append(e('muse-' + name, 'Muse ' + account, account, f'{role}. {scope}.', status, detail, MUSE_URL))
        for name in ('hermes', *MUSE):
            account = 'Hermes' if name == 'hermes' else MUSE[name][0]
            rows.append(e('crosswalk-' + name, 'Crosswalk / ' + account, account, 'Private board with a distinct key and receipt',
                          'Needs human consent', 'Sign in to Crosswalk and install this identity’s connector through its secure credential prompt.',
                          CROSSWALK_URL if name != 'hermes' else 'https://crosswalk.to/keys'))
        for wid, spec in self.monitor.config['workspaces'].items():
            if wid not in actor.get('workspaces', []):
                continue
            state = self.store.workspace(wid)
            observed = self.monitor.observed.get(wid, {})
            ok = time.time() - observed.get('utc', 0) < 15 and not observed.get('error')
            rows.append(e('browser-' + wid, spec['name'] + ' browser', wid, 'Existing persistent browser resource',
                          'Tested read' if ok else 'Unavailable',
                          ('Human pause held. ' if state['paused'] else '') +
                          ('Controller observed the profile recently; site login is separate.' if ok else 'No recent profile observation.')))
            if ok:
                rows[-1]['verifiedAt'] = observed['utc']
        for row in rows:
            row.update(self.saved(row['id']))
            checked = row.get('verifiedAt')
            row['verifiedAt'] = iso(checked) if checked else None
            row['stale'] = bool(checked and time.time() - checked > 86400)
        return {'schema': 1, 'observedAt': iso(), 'entries': rows}

    def verify(self, actor, name):
        if actor.get('role') != 'human' or actor['id'] not in HUMANS:
            raise Denied('Attended operator verification required')
        if name not in ('google', 'gmail'):
            raise Conflict('Use the specified attended provider or runtime verification')
        if not self.refresh_lock.acquire(blocking=False):
            raise Conflict('A connection check is already running')
        try:
            started = time.monotonic()
            if name == 'google':
                self.google()
            else:
                self.gmail()
            self.monitor.diagnostics.emit('connection_latency', 'manager', command=name,
                                          elapsedMs=(time.monotonic() - started) * 1000)
            return self.list(actor)
        finally:
            self.refresh_lock.release()

    def google(self):
        api = self.monitor.google
        if api is None:
            return self.record('google', 'Not configured', 'Provide the existing Desktop OAuth configuration.')
        if not api.token.is_file():
            return self.record('google', 'Needs human consent', 'Foot/cloud Desktop OAuth grant is missing.')
        spec = self.settings.get('google', {})
        try:
            api.connect(force_refresh=True)
            identity = api.call('drive', 'about?fields=user(emailAddress,permissionId)').get('user', {})
            account = identity.get('emailAddress')
            if not account or not spec.get('account') or account.casefold() != spec['account'].casefold():
                return self.record('google', 'Permission limited', 'OAuth refreshed, but the expected Foot/cloud identity must be pinned and matched.')
            sheets = self.monitor.outbox.sheets if self.monitor.outbox else None
            if sheets is None:
                return self.record('google', 'Permission limited', 'Drive identity verified; canonical mapping is missing.', account=account)
            for mapping in sheets.schema.values():
                sheets.headers(mapping)
            doc = spec.get('docsProbeId')
            if not isinstance(doc, str) or not re.fullmatch(r'[A-Za-z0-9_-]{15,150}', doc):
                return self.record('google', 'Permission limited', 'Drive and Sheets verified. Configure one approved Docs read probe.', account=account)
            if api.call('docs', 'documents/' + quote(doc, safe='') + '?fields=documentId').get('documentId') != doc:
                raise Conflict('Docs document identity did not match')
            writes = bool(self.monitor.config['google'].get('productionEnabled', False))
            return self.record('google', 'Tested read',
                               f'Official refresh, Drive identity, {len(sheets.schema)} live Sheet mappings and Docs read passed. '
                               + ('Canonical writer is enabled.' if writes else 'Canonical writer is off.'), account=account)
        except GoogleUnavailable as error:
            status = 'Expired/revoked' if 'expired or was revoked' in str(error) else 'Permission limited'
            return self.record('google', status, str(error))
        except Conflict:
            return self.record('google', 'Permission limited', 'Live Drive/Docs identity or Sheet headers changed; inspect the mapping.')
        except Exception:
            return self.record('google', 'Unavailable', 'Google verification could not complete. Check network and Ubuntu runtime diagnostics.')

    def gmail(self):
        cfg = self.settings.get('gmail', {})
        if not cfg.get('tokenFile') or not cfg.get('account'):
            return self.record('gmail', 'Not configured', 'No separate professional mailbox consent is configured.')
        if not Path(cfg['tokenFile']).is_file():
            return self.record('gmail', 'Needs human consent', 'Authorize the professional mailbox with gmail.readonly in the supported browser.')
        try:
            api = Google(cfg['tokenFile'], MAIL_SCOPES)
            api.connect(force_refresh=True)
            account = api.call('gmail', 'users/me/profile?fields=emailAddress').get('emailAddress', '')
            if account.casefold() != cfg['account'].casefold():
                return self.record('gmail', 'Permission limited', 'Mailbox identity mismatch. No messages accessed.')
            return self.record('gmail', 'Tested read', 'OAuth refresh and exact mailbox profile verified. No messages accessed.', account=account)
        except GoogleUnavailable as error:
            status = 'Expired/revoked' if 'expired or was revoked' in str(error) else 'Permission limited'
            return self.record('gmail', status, str(error))
        except Exception:
            return self.record('gmail', 'Unavailable', 'The mailbox profile check did not complete. Check Ubuntu runtime diagnostics.')
