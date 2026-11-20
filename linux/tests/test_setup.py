import contextlib,json,os,stat,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from muse_monitor.google_bridge import authorize,Google,SCOPES
from muse_monitor.rules import shared,validate
from muse_monitor.store import Store,Conflict,Denied
from muse_monitor.browser import Browser,BrowserError

class SetupTests(unittest.TestCase):
    def test_shared_drive_authorization_does_not_grant_other_mailbox_access(self):
        bridge=Google('/not-a-credential')
        with self.assertRaises(Denied):bridge.call('gmail','users/me/messages')
        self.assertNotIn('https://www.googleapis.com/auth/gmail.readonly',SCOPES)
    def test_closed_workspace_opens_normal_chrome_without_closing_missing_browser(self):
        from muse_monitor import human_login
        with tempfile.TemporaryDirectory() as root:
            home=Path(root);base=home/'research';profile=base/'profile';(profile/'Default').mkdir(parents=True)
            (profile/'Default/Preferences').write_text(json.dumps({'profile':{'name':'old'},'unrelated':{'preserve':True}}))
            (profile/'Local State').write_text(json.dumps({'unrelated':'retain'}))
            config=home/'.config/muse-monitor/config.json';config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'state':root,'workspaces':{'research':{'name':'Research','base':str(base),'profile':str(profile)}}}))
            browser=Mock();browser.exclusive.side_effect=lambda wid:contextlib.nullcontext();launched=[]
            def launch(command,**kwargs):launched.append(command)
            def current(profile):return [{'pid':101,'automated':False}] if launched else []
            with patch.object(human_login.Path,'home',return_value=home),patch.object(human_login,'Browser',return_value=browser),patch.object(human_login,'owners',side_effect=current),patch.object(human_login,'human'),patch.object(human_login,'request',return_value={'workspaces':[{'id':'research','actor':None,'pages':[]}]}),patch.object(human_login.subprocess,'Popen',side_effect=launch),patch('sys.argv',['human-login','research','begin']):
                human_login.main()
            browser.cli.assert_not_called()
            self.assertEqual(json.loads((profile/'Default/Preferences').read_text())['unrelated'],{'preserve':True})
            self.assertEqual(json.loads((profile/'Local State').read_text())['unrelated'],'retain')
            self.assertTrue((base/'manual-login.json').exists())
            self.assertIn('--profile-directory=Default',launched[0])
            self.assertFalse(any('remote-debugging' in a or 'incognito' in a or a=='--guest' for a in launched[0]))
    def test_human_login_marker_disconnects_all_cli_and_startup(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root,'manual-login.json').write_text('{}')
            config={'runtime':root,'workspaces':{'jobs':{'base':root,'profile':root+'/profile','session':'main'}}}
            browser=Browser(config)
            with patch('subprocess.Popen') as launch:
                with self.assertRaises(BrowserError):browser.cli('jobs','run-code','not executed')
                browser.start_additional('jobs')
                launch.assert_not_called()
    def test_google_loopback_pkce_and_private_atomic_token(self):
        with tempfile.TemporaryDirectory() as root:
            client=Path(root)/'client.json';client.write_text('{}');token=Path(root)/'token.json'
            flow=Mock();flow.run_local_server.return_value.to_json.return_value='{"fixture":true}'
            with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file',return_value=flow) as factory:
                authorize(client,token)
            self.assertTrue(factory.call_args.kwargs['autogenerate_code_verifier'])
            self.assertEqual(flow.run_local_server.call_args.kwargs['host'],'127.0.0.1')
            self.assertEqual(flow.run_local_server.call_args.kwargs['timeout_seconds'],300)
            self.assertEqual(stat.S_IMODE(token.stat().st_mode),0o600)
            self.assertEqual(json.loads(token.read_text()),{'fixture':True})
            self.assertFalse(token.with_name('token.json.new').exists())
    def test_shared_rules_revisions_and_scopes(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'test.db');monitor=Mock(store=store)
            rule={'id':'fixture','title':'Fixture','color':'blue','domains':['example.com'],'priority':0}
            human={'id':'human','role':'human'};alt={'id':'alt','role':'alt'}
            self.assertEqual(shared(monitor,human,'jobs',{})['revision'],0)
            result=shared(monitor,human,'jobs',{'rules':[rule],'revision':0})
            self.assertEqual(result['revision'],1)
            with self.assertRaises(Conflict):shared(monitor,human,'jobs',{'rules':[],'revision':0})
            with self.assertRaises(Denied):shared(monitor,alt,'jobs',{'rules':[],'revision':1})
            self.assertEqual(shared(monitor,human,'research',{})['rules'],[])
            self.assertEqual(shared(monitor,alt,'jobs',{})['rules'],[rule])
            with self.assertRaises(Conflict):validate([{**rule,'domains':['example.com/steal']}])
            store.db.close()
