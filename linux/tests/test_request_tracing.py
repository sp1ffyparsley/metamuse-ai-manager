import base64,contextlib,importlib.util,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('traced_legacy',Path(__file__).parents[1]/'legacy-control.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)

class RequestTracingTests(unittest.TestCase):
    def test_claim_uses_new_run_id_while_release_preserves_its_original_id(self):
        for operation,expected in [('claim','fedcba9876543210'),('release','0123456789abcdef')]:
            trace=Mock()
            with patch.object(legacy,'request_logging',return_value=(trace,Path('/unused'))),patch.object(legacy,'request_run_id',side_effect=['0123456789abcdef','fedcba9876543210']),patch.object(legacy,'run',return_value=0):
                self.assertEqual(legacy.main([operation]),0)
            self.assertEqual(trace.emit.call_args.kwargs['runId'],expected)

    def test_malformed_lease_metadata_is_not_an_action_failure_or_secret_source(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'lease'
            for data in ('[]','null','invalid JSON','{"runId":"SECRET"}'):
                p.write_text(data);self.assertIsNone(legacy.request_run_id(p))
            p.write_text('{"runId":"0123456789abcdef"}')
            self.assertEqual(legacy.request_run_id(p),'0123456789abcdef')

    def test_only_fixed_operations_can_be_recorded(self):
        secret='SECRET_TOKEN_OR_FORM_VALUE'
        for command,expected in [('run '+secret+' upload missing.pdf','upload'),
                                 ('run '+secret+' tab-switch 1','tab-select'),
                                 (secret+' '+secret,'invalid'),
                                 ('run '+secret,'invalid'),
                                 ('"unterminated','invalid')]:
            with patch.dict(os.environ,{'SSH_ORIGINAL_COMMAND':command}):
                label=legacy.request_operation(['--ssh'])
            self.assertEqual(label,expected);self.assertNotIn(secret,label)
        data=base64.urlsafe_b64encode(json.dumps({'command':'fill','args':['e1',secret]}).encode()).decode().rstrip('=')
        self.assertEqual(legacy.request_operation(['run-encoded',secret,data]),'fill')

    def test_failure_before_dispatch_is_logged_without_its_message(self):
        trace=Mock()
        with patch.object(legacy,'request_logging',return_value=(trace,None)),patch.object(legacy,'run',side_effect=ValueError('PRIVATE_FILE_OR_FIELD')) as run,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(legacy.main(['run','SECRET_TOKEN','upload','missing.pdf']),1)
        run.assert_called_once();trace.emit.assert_called_once()
        row=trace.emit.call_args.kwargs
        self.assertEqual(row['command'],'upload');self.assertEqual(row['errorType'],'ValueError')
        self.assertEqual(row['status'],1);self.assertGreaterEqual(row['elapsedMs'],0)
        self.assertNotIn('PRIVATE_FILE_OR_FIELD',repr(trace.emit.call_args))
        self.assertNotIn('SECRET_TOKEN',repr(trace.emit.call_args))

    def test_uncertain_outcome_and_logger_failure_never_replay_or_change_exit(self):
        for code in (0,1,124):
            trace=Mock();trace.emit.side_effect=OSError('logging unavailable')
            with patch.object(legacy,'request_logging',return_value=(trace,None)),patch.object(legacy,'run',return_value=code) as run:
                self.assertEqual(legacy.main(['run','TOKEN','fill','e1','text']),code)
            run.assert_called_once()
            self.assertEqual(trace.emit.call_args.kwargs['outcome'],'uncertain' if code==124 else 'ok' if code==0 else 'error')

if __name__=='__main__':unittest.main()
