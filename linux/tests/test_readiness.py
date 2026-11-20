import contextlib,importlib.util,io,json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('ready_legacy',Path(__file__).parents[1]/'legacy-control.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)


class ReadinessTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self,command,paused=True):
        # Exercise the actual forced-handler dispatcher without touching /run,
        # any real lease, Chrome profile, or desktop session.
        with tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
            previous_mask=os.umask(0o077);stack.callback(os.umask,previous_mask)
            root=Path(tmp);home=root/'home';home.mkdir(mode=0o700);base=root/'base';base.mkdir(mode=0o700)
            runtime=root/'runtime';runtime.mkdir(mode=0o700);(runtime/'muse-browser-main').mkdir(mode=0o700)
            if paused:(base/'paused').touch()
            (root/'site.json').write_text(json.dumps({'roles':{'fixture':'main'},'checkpoint':str(base),'node_bin':'/unreachable-node'}))
            account=SimpleNamespace(pw_name='fixture',pw_uid=os.getuid(),pw_dir=str(home))
            actual_path=Path
            def path(value):
                return runtime if str(value)==f'/run/user/{account.pw_uid}' else actual_path(value)
            stack.enter_context(patch.object(legacy,'__file__',str(root/'control.py')))
            stack.enter_context(patch.object(legacy,'Path',side_effect=path))
            stack.enter_context(patch.object(legacy.pwd,'getpwuid',return_value=account))
            stack.enter_context(patch.object(legacy.signal,'signal'))
            stack.enter_context(patch.dict(os.environ,{'SSH_ORIGINAL_COMMAND':command}))
            yield root,base,runtime/'muse-browser-main'

    def test_inactive_status_reports_pause_and_desktop_without_browser_dispatch(self):
        with self.fixture('status') as (_,base,runtime),patch.object(legacy,'gui_available',return_value=False),patch.object(legacy.subprocess,'run',side_effect=AssertionError('No browser/process probe allowed')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(legacy.run(['--ssh']),0)
            data=json.loads(next(line.removeprefix('Readiness: ') for line in out.getvalue().splitlines() if line.startswith('Readiness: ')))
            self.assertEqual(data['blockers'],['HUMAN_PAUSED','DESKTOP_UNAVAILABLE'])
            self.assertFalse(data['desktopReady']);self.assertTrue(data['admissionBlocked'])
            self.assertIn('Automation: paused',out.getvalue());self.assertTrue((base/'paused').exists())
            self.assertFalse((runtime/'lease').exists())

    def test_release_inactive_paused_browser_requires_own_token_but_no_gui(self):
        for supplied,success in [('fixture-token',True),('wrong-token',False)]:
            with self.fixture('release '+supplied) as (_,base,runtime),patch.object(legacy,'gui_available',side_effect=AssertionError('Release must not depend on GUI')),patch.object(legacy.subprocess,'run',side_effect=AssertionError('Release must not dispatch browser')),contextlib.redirect_stdout(io.StringIO()):
                lease=runtime/'lease';lease.write_text(json.dumps({'token':'fixture-token','until':time.monotonic()+300}))
                if success:self.assertEqual(legacy.run(['--ssh']),0)
                else:
                    with self.assertRaisesRegex(ValueError,'different controller'):legacy.run(['--ssh'])
                self.assertEqual(lease.exists(),not success);self.assertTrue((base/'paused').exists())

    def test_claim_distinguishes_pause_from_inactive_desktop_and_never_dispatches(self):
        for paused,expected in [(True,legacy.HumanPaused),(False,legacy.DesktopUnavailable)]:
            with self.fixture('claim',paused) as (_,base,runtime),patch.object(legacy,'gui_available',return_value=False),patch.object(legacy.subprocess,'run',side_effect=AssertionError('Claim must fail before dispatch')):
                with self.assertRaises(expected):legacy.run(['--ssh'])
                self.assertFalse((runtime/'lease').exists())

    def test_failure_log_uses_fixed_gate_class_without_message_or_token(self):
        from unittest.mock import Mock
        trace=Mock()
        with patch.object(legacy,'request_logging',return_value=(trace,None)),patch.object(legacy,'run',side_effect=legacy.HumanPaused('private context')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(legacy.main(['claim']),1)
        self.assertEqual(trace.emit.call_args.kwargs['errorType'],'HumanPaused')
        self.assertNotIn('private context',repr(trace.emit.call_args))


if __name__=='__main__':unittest.main()
