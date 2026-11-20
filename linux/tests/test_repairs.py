import base64
import contextlib
import fcntl
import hashlib
import io,zipfile
import importlib.util
import json
import os
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from muse_monitor.artifacts import import_drive_pdf, list_artifacts,drive_id,valid_artifact
from muse_monitor.browser import Browser
from muse_monitor.server import Handler
from muse_monitor.store import Conflict, Denied

spec = importlib.util.spec_from_file_location('legacy_control', Path(__file__).parents[1] / 'legacy-control.py')
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


class TransferAPI:
    def __init__(self, data):
        self.data = data
        self.session = self
        self.downloads = 0
        self.meta = {'id': 'fixtureDriveFile123', 'mimeType': 'application/pdf', 'size': len(data)}

    def call(self, service, path):
        return dict(self.meta)

    def get(self, endpoint, **kw):
        assert endpoint.startswith('https://www.googleapis.com/drive/v3/files/fixtureDriveFile123')
        assert kw == {'stream': True, 'timeout': 15, 'allow_redirects': False}
        self.downloads += 1
        return self

    def __enter__(self):
        self.status_code = 200
        self.headers = {}
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.data


class RepairTests(unittest.TestCase):
    def test_drive_links_auto_names_and_word_files_use_existing_bounded_import(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>');z.writestr('word/document.xml','<document/>')
        data=b.getvalue();api=TransferAPI(data)
        self.assertTrue(valid_artifact('Resume.DOCX',data))
        api.meta.update(name='Resume draft.docx',mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        with tempfile.TemporaryDirectory() as tmp:
            r=import_drive_pdf(api,Path(tmp),'https://drive.google.com/file/d/fixtureDriveFile123/view')
            self.assertEqual(r['artifact'],'Resume_draft.docx')
            self.assertEqual(list_artifacts(tmp)['artifacts'],[r])
        self.assertEqual(drive_id('https://docs.google.com/document/d/fixtureDriveFile123/edit'),'fixtureDriveFile123')
        for value in ('https://evil.example/file/d/fixtureDriveFile123','../private-key'):
            with self.assertRaises(Conflict):drive_id(value)
        self.assertFalse(valid_artifact('fake.docx',b'not a document'))
        with zipfile.ZipFile(b,'a') as z:z.writestr('word/vbaProject.bin','not permitted')
        self.assertFalse(valid_artifact('macro.docx',b.getvalue()))

    def test_empty_fill_is_valid_when_transport_preserves_it_and_clear_is_unambiguous(self):
        self.assertEqual(legacy.command_values('fill',['e1','']),['e1',''])
        self.assertEqual(legacy.command_values('clear',['e1']),['e1'])
        with self.assertRaisesRegex(ValueError,'use clear'):
            legacy.command_values('fill',['e1'])
        self.assertEqual(legacy.command_values('press',['e1','Control+A']),['e1','Control+A'])
        with self.assertRaises(ValueError):legacy.command_values('press',['not-a-ref','Enter'])
        self.assertEqual(legacy.command_values('read-value',['f1e2']),['f1e2'])
        with self.assertRaises(ValueError):legacy.command_values('read-value',['input[name=password]'])

    def test_capabilities_describe_the_existing_upload_prerequisite_and_no_shell_copy(self):
        protocol=legacy.capabilities()['uploadProtocol']
        self.assertTrue(protocol['requiresOpenChooser'])
        self.assertFalse(protocol['shellFileTransferRequired'])
        self.assertEqual(protocol['cancel'],'upload without a filename')

    def test_cancel_without_a_chooser_is_idempotent_but_unknown_failures_are_not_success(self):
        import subprocess
        cli=Mock(return_value=subprocess.CompletedProcess([],1,'### Error\nupload requires an open file chooser.'))
        result=legacy.cancel_chooser(cli)
        self.assertEqual(result.returncode,0);self.assertIn('"noChooserOpen": true',result.stdout)
        cli=Mock(return_value=subprocess.CompletedProcess([],124,'deadline'))
        self.assertEqual(legacy.cancel_chooser(cli).returncode,124)
        self.assertEqual(cli.call_count,1)

    def test_gui_admission_rejects_inactive_locked_signed_out_or_ssh_session(self):
        online={'Type':'x11','Class':'user','State':'online','Active':'yes','LockedHint':'no'}
        self.assertTrue(legacy.usable_gui(online))
        for key,value in (('Active','no'),('Type','tty'),('Type','unspecified'),('State','closing'),('LockedHint','yes'),('Class','greeter')):
            self.assertFalse(legacy.usable_gui(dict(online,**{key:value})))
        self.assertFalse(legacy.usable_gui({}))

    def test_discoverable_preparation_and_tab_aliases_keep_restricted_operations(self):
        self.assertEqual(legacy.preparation_request('admit', ['fixtureDriveFile123', 'approved.pdf']),
                         {'op': 'artifact-import', 'fileId': 'fixtureDriveFile123', 'filename': 'approved.pdf'})
        for args in ([], ['filename.pdf'], ['id', 'file.pdf', 'extra']):
            with self.assertRaises(ValueError):legacy.preparation_request('admit', args)
        self.assertEqual(legacy.COMMAND_ALIASES['tab-switch'], 'tab-select')
        self.assertEqual(legacy.capabilities()['browserCommands'], list(legacy.REMOTE_COMMANDS))
        self.assertFalse(legacy.capabilities()['arbitraryShell'])
        self.assertNotIn('run-code', legacy.COMMAND_ALIASES.values())

    def test_admitted_inventory_reads_actual_bytes_and_rejects_outside_or_stale_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=b'%PDF-1.4\nfixture document\n%%EOF'
            api=TransferAPI(data)
            result=import_drive_pdf(api,root,api.meta['id'],'approved.pdf')
            (root/'artifacts/link.pdf').symlink_to(root/'artifacts/approved.pdf')
            (root/'artifacts/not-pdf.pdf').write_text('not a PDF')
            (root/'artifacts/secret.txt').write_text('private')
            rows=list_artifacts(root)['artifacts'];self.assertEqual(len(rows),1)
            self.assertEqual(rows[0],result)
            (root/'artifacts/approved.pdf').write_bytes(b'%PDF-1.4\nchanged bytes\n%%EOF')
            row=list_artifacts(root)['artifacts'][0]
            self.assertNotEqual(row['sha256'],result['sha256']);self.assertNotIn('source',row)

    def test_lossless_ssh_text_transport_does_not_expand_the_allowlist(self):
        text = 'Multi word "quoted" applicant\nUnicode: é\n$(must stay text)'
        payload = {'command': 'fill', 'args': ['f11e88', text]}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        self.assertEqual(legacy.decode_run(encoded), ('fill', ['f11e88', text]))
        self.assertNotIn('run-code', legacy.REMOTE_COMMANDS)
        self.assertNotIn('eval', legacy.REMOTE_COMMANDS)
        self.assertNotIn('close', legacy.REMOTE_COMMANDS)
        self.assertEqual(legacy.REMOTE_COMMANDS['upload'], (0, 1))
        self.assertEqual(legacy.command_values('fill', ['e1', 'B.S.', 'Cybersecurity']), ['e1', 'B.S. Cybersecurity'])
        self.assertEqual(legacy.command_values('select', ['e2', 'North', 'Carolina']), ['e2', 'North Carolina'])
        self.assertEqual(legacy.command_values('type', ['e3', 'two words']),['e3','two words'])
        self.assertEqual(legacy.command_values('type', ['e3',' leading and trailing ']),['e3',' leading and trailing '])
        for payload in ({'command': 'fill', 'args': [1]}, {'command': 'fill', 'args': ['\0']}, {'command': 'fill', 'args': [], 'shell': 'ignored'}):
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            with self.assertRaises(ValueError):
                legacy.decode_run(encoded)

    def test_viewer_yields_to_a_queued_remote_request_and_request_waits_for_short_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'runtime': str(root), 'workspaces': {'jobs': {'base': str(root / 'base'), 'session': 'main'}}}
            browser = Browser(config)
            _, runtime, _, _ = browser.paths('jobs')
            held = threading.Event()
            release = threading.Event()

            def holder():
                with (runtime / 'control.lock').open('a') as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX)
                    held.set()
                    release.wait(4)

            thread = threading.Thread(target=holder)
            thread.start()
            held.wait(1)
            entered = threading.Event()

            def request():
                with legacy.control_lock(runtime, True):
                    entered.set()

            worker = threading.Thread(target=request)
            worker.start()
            try:
                deadline = time.monotonic() + 1
                while not browser.legacy_waiting('jobs') and time.monotonic() < deadline:
                    time.sleep(.01)
                with self.assertRaises(Conflict):
                    browser.frame('jobs', 'not-reached', 'not-reached')
                time.sleep(1.6)  # the former 1.5s admission would have failed
                self.assertFalse(entered.is_set())
            finally:
                release.set()
                thread.join(2)
                worker.join(2)
            self.assertTrue(entered.is_set())
            self.assertFalse(browser.legacy_waiting('jobs'))

    def test_pdf_import_is_private_byte_exact_and_cannot_overwrite_or_traverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b'%PDF-1.4\nfixture document\n%%EOF'
            api = TransferAPI(data)
            sha = hashlib.sha256(data).hexdigest()
            result = import_drive_pdf(api, root, api.meta['id'], 'approved.pdf', sha)
            target = root / 'artifacts/approved.pdf'
            self.assertEqual(result['sha256'], sha)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(import_drive_pdf(api, root, api.meta['id'], 'approved.pdf')['sha256'], sha)
            for filename in ('../secret.pdf', '/tmp/secret.pdf', 'not-a-pdf.txt'):
                with self.assertRaises(Conflict):
                    import_drive_pdf(api, root, api.meta['id'], filename)
            api.data = b'%PDF-1.4\ndifferent file\n%%EOF'
            with self.assertRaises(Conflict):
                import_drive_pdf(api, root, api.meta['id'], 'approved.pdf')
            self.assertEqual(target.read_bytes(), data)
            (root / 'artifacts/link.pdf').symlink_to(target)
            with self.assertRaises(Conflict):
                import_drive_pdf(api, root, api.meta['id'], 'link.pdf')

    def test_import_rejects_bad_bytes_size_digest_and_trashed_files_without_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for data in (b'<!doctype html>not a PDF', b'%PDF-' + b'x' * 110):
                api = TransferAPI(data)
                api.meta['size'] = 20  # exercise the streamed bound independently
                with patch('muse_monitor.artifacts.LIMIT', 100):
                    with self.assertRaises(Denied):
                        import_drive_pdf(api, root, api.meta['id'], 'must-not-exist.pdf')
                self.assertEqual(list((root / 'artifacts').iterdir()), [])
            api = TransferAPI(b'%PDF-1.4\nfixture document\n%%EOF')
            with self.assertRaises(Conflict):
                import_drive_pdf(api, root, api.meta['id'], 'wrong-digest.pdf', '0' * 64)
            api.meta['trashed'] = True
            count = api.downloads
            with self.assertRaises(Denied):
                import_drive_pdf(api, root, api.meta['id'], 'trash.pdf')
            self.assertEqual(api.downloads, count)
            self.assertEqual(list((root / 'artifacts').iterdir()), [])

    def test_upload_cannot_read_secrets_or_follow_an_artifact_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / '.config/muse-monitor/config.json'
            config.parent.mkdir(parents=True)
            state = home / 'state'
            artifacts = state / 'artifacts'
            artifacts.mkdir(parents=True)
            config.write_text(json.dumps({'state': str(state)}))
            approved = artifacts / 'approved.pdf'
            approved.write_bytes(b'%PDF-1.4\nfixture\n%%EOF')
            self.assertEqual(legacy.upload_artifact(home, 'approved.pdf'), str(approved))
            secret = home / 'secret.pdf'
            secret.write_bytes(approved.read_bytes())
            (artifacts / 'link.pdf').symlink_to(secret)
            for value in ('../secret.pdf', str(secret), 'link.pdf'):
                with self.assertRaises(ValueError):
                    legacy.upload_artifact(home, value)

    def test_disconnected_tls_viewer_is_not_reported_as_a_browser_failure(self):
        handler = Handler.__new__(Handler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock(side_effect=ssl.SSLEOFError('client disconnected'))
        handler.send(200, {'observed': True})
        self.assertTrue(handler.close_connection)


if __name__ == '__main__':
    unittest.main()
