import json,subprocess,unittest,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from muse_monitor.browser import Browser
from muse_monitor.desktop_mode import apply_mode,code,main

def done(value,exit=0):return subprocess.CompletedProcess([],exit,'### Result\n'+json.dumps(value))
class DesktopModeTests(unittest.TestCase):
    def test_cancelled_mode_dialog_cannot_resume_agents(self):
        with patch('sys.argv',['desktop-mode','choose']),patch('muse_monitor.desktop_mode.subprocess.run',return_value=subprocess.CompletedProcess([],0,'')),patch('muse_monitor.cli.request') as request:
            main();request.assert_not_called()

    def test_sign_in_on_either_profile_blocks_batch_resume_before_any_unpause(self):
        with tempfile.TemporaryDirectory() as root:
            home=Path(root);base=home/'secondary';base.mkdir();(base/'manual-login.json').write_text('{}')
            config=home/'.config/muse-monitor/config.json';config.parent.mkdir(parents=True);config.write_text(json.dumps({'workspaces':{'jobs':{'base':str(home/'jobs')},'scholarships':{'base':str(base)}}}))
            with patch('sys.argv',['desktop-mode','agent']),patch.object(Path,'home',return_value=home),patch('muse_monitor.cli.request') as request:
                with self.assertRaises(ValueError):main()
                request.assert_not_called()

    def test_browser_restart_invalidates_cached_input_mode(self):
        browser=Browser({'workspaces':{'jobs':{}}});browser.input_modes['jobs']=True;browser.mode_sessions['jobs']=('socket',1,10)
        with patch.object(browser,'_mode_signature',return_value=('socket',1,10)):
            self.assertTrue(browser.input_mode_matches('jobs',True))
        with patch.object(browser,'_mode_signature',return_value=('socket',2,20)):
            self.assertFalse(browser.input_mode_matches('jobs',True))
    def test_current_mode_is_fixed_and_receipt_is_compact(self):
        cli=Mock(return_value=done({'mode':'human','filesChanged':False}))
        out=apply_mode(cli,True);self.assertIn('"mode": "human"',out.stdout)
        self.assertNotIn('framenavigated',out.stdout);self.assertEqual(cli.call_count,1)
        self.assertIn('"human": false',code(False))

    def test_modal_handoff_uses_only_owned_blank_and_restores_original_index(self):
        cli=Mock(side_effect=[subprocess.CompletedProcess([],1,'Blocked by file chooser'),subprocess.CompletedProcess([],0,'### Result\n- 2: (current) original'),done({}),done({'mode':'human'})])
        self.assertEqual(apply_mode(cli,True).returncode,0)
        self.assertEqual(cli.call_args_list[2].args,('tab-new','about:blank'))
        self.assertIn('"restoreIndex": 2',cli.call_args_list[3].args[1])
        self.assertIn('"temporary": true',cli.call_args_list[3].args[1])

    def test_uncertain_dispatch_does_not_replay_and_missing_receipt_is_failure(self):
        cli=Mock(return_value=subprocess.CompletedProcess([],124,'uncertain'))
        self.assertEqual(apply_mode(cli,False).returncode,124);self.assertEqual(cli.call_count,1)
        self.assertNotEqual(apply_mode(Mock(return_value=done([])),True).returncode,0)

if __name__=='__main__':unittest.main()
