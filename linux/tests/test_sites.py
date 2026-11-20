import json
import subprocess
import unittest
from unittest.mock import Mock, patch
import test_authority
import test_tasks
from muse_monitor.store import Conflict, Denied
from muse_monitor.sites import identity, plan
from muse_monitor.legacy_actions import guard_site


class SiteTests(unittest.TestCase):
    setUp=test_authority.AuthorityTests.setUp
    tearDown=test_authority.AuthorityTests.tearDown
    packet=test_tasks.TaskTests.packet
    api=test_tasks.TaskTests.api
    save=test_tasks.TaskTests.save

    def site(self, action, actor=None, **body):
        return self.m.worker(actor or self.main,{'op':'site','action':action,**body})

    def remember(self, workspace='jobs', context='default'):
        return self.site('remember',url='https://portal.example/apply',workspace=workspace,context=context,revision=0,note='Chosen existing workspace; login must still be checked')

    def test_profile_memory_normalizes_host_and_keeps_contexts_separate(self):
        a=self.remember()
        self.assertEqual(identity('https://PORTAL.EXAMPLE./other?utm_source=x')[-1],identity('http://portal.example/')[-1])
        self.assertNotIn('apply',json.dumps(a))
        self.assertEqual(self.site('read',url='https://portal.example/other')['workspace'],'jobs')
        self.remember('scholarships','other-authorized-identity')
        self.site('check',url='https://portal.example/',workspace='scholarships',context='other-authorized-identity')
        with self.assertRaises(Conflict):self.site('check',url='https://portal.example/',workspace='scholarships')
        self.assertIn('not verified',self.site('check',url='https://portal.example/',workspace='jobs')['authentication'].lower())

    def test_rebinding_is_explicit_and_stale_write_fails(self):
        a=self.remember()
        with self.assertRaises(Conflict):self.site('remember',url='https://portal.example',workspace='scholarships',revision=a['revision'],note='Do not silently switch')
        with self.assertRaises(Conflict):self.site('remember',url='https://portal.example',workspace='jobs',revision=0,note='stale')
        b=self.site('remember',url='https://portal.example',workspace='scholarships',revision=a['revision'],rebind=True,note='Correct existing authorized session verified here')
        self.assertEqual(b['workspace'],'scholarships')

    def test_tasks_and_known_navigation_are_blocked_in_wrong_profile(self):
        self.remember()
        t=self.save(workspace='scholarships',targetURL='https://portal.example/apply')
        self.assertEqual(t['state'],'draft')
        with self.assertRaises(Conflict):self.m.worker(self.main,{'op':'claim-item','item':t['id'],'revision':t['revision']})
        lease=self.m.worker(self.main,{'op':'claim','workspace':'scholarships'})
        before=len(self.m.browser.calls)
        with self.assertRaises(Conflict):self.m.worker(self.main,{'op':'browser','workspace':'scholarships',**lease,'requestId':'nav-fixture-1','action':{'kind':'new','url':'https://portal.example/apply'}})
        self.assertEqual(len(self.m.browser.calls),before)

    def test_captcha_block_is_durable_deduplicated_and_does_not_hold_other_work(self):
        binding=self.remember()
        t=self.save(targetURL='https://portal.example/apply')
        blocked=self.api('block',id=t['id'],revision=t['revision'],category='captcha',siteRevision=binding['revision'],reason='CAPTCHA',question='Please complete the challenge on this saved form',nextAction='Inspect the same form afterward',url='https://portal.example/apply')
        self.assertTrue(blocked['notificationRecommended'])
        changes=self.m.store.db.total_changes
        again=self.api('block',id=t['id'],revision=blocked['revision'],category='captcha',siteRevision=binding['revision'],reason='CAPTCHA',question='Please complete the challenge on this saved form',nextAction='Inspect the same form afterward',url='https://portal.example/apply')
        self.assertFalse(again['notificationRecommended']);self.assertEqual(self.m.store.db.total_changes,changes)
        self.save('independent',execution='connector',targetURL='https://portal.example/records')
        self.assertEqual([r['id'] for r in self.m.worker(self.main,{'op':'next-work'})],['independent'])
        self.assertEqual(self.m.status(self.main)['workspaces'][0]['queue'],1)
        self.assertEqual(self.m.browser.calls,[])
        self.assertFalse(self.m.store.workspace('jobs')['paused'],'A task blocker must not forge a global human pause')
        challenge=self.site('read',url='https://portal.example')['challenge']
        self.assertEqual(challenge['id'],blocked['blocker']['challengeId'])
        with self.assertRaises(Conflict):self.api('unblock',id=t['id'],revision=blocked['revision'],resolution='User completed it and the page was checked',challengeId='stale')
        resolved=self.api('unblock',id=t['id'],revision=blocked['revision'],resolution='User completed it; form visible again',challengeId=challenge['id'])
        self.assertEqual(resolved['state'],'ready')
        self.site('check',url='https://portal.example/apply',workspace='jobs')

    def test_site_challenge_duplicate_has_no_new_notification_or_expiry_retry(self):
        a=self.remember()
        b=self.site('challenge',url='https://portal.example',revision=a['revision'],question='Complete the saved challenge',nextAction='Continue here')
        count=self.m.store.db.total_changes
        c=self.site('challenge',url='https://portal.example',revision=0,question='Same blocked site',nextAction='Wait')
        self.assertFalse(c['notificationRecommended']);self.assertEqual(count,self.m.store.db.total_changes)
        self.assertEqual(b['challenge'],c['challenge'])
        with self.assertRaises(Conflict):self.site('check',url='https://portal.example',workspace='jobs')
        with self.assertRaises(Conflict):self.site('remember',actor=self.human,url='https://portal.example',revision=b['revision'],workspace='scholarships',rebind=True,note='Cannot relocate an unresolved challenge')

    def test_metadata_permissions_and_existing_schema_stay_narrow(self):
        before={r[0] for r in self.m.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.remember()
        self.assertEqual(before,{r[0] for r in self.m.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        limited=dict(self.human,workspaces=['research'])
        self.assertEqual(self.site('list',actor=limited)['sites'],[])
        with self.assertRaises(Denied):self.site('read',actor=limited,url='https://portal.example')
        with self.assertRaises(Denied):self.site('list',actor=self.m.principal(self.tokens['alt']))
        with self.assertRaises(Conflict):self.site('remember',url='https://portal.example/?access_token=not-a-real-credential',workspace='jobs',revision=0,note='must not persist this URL')

    def test_off_browser_planning_and_brief_do_not_touch_browser(self):
        for kind in ('research','records','documents'):
            self.assertEqual(self.api('plan',kind=kind)['execution'],'connector')
        t=self.save(execution='connector')
        with self.assertRaises(Conflict):self.m.worker(self.main,{'op':'claim','workspace':'jobs','task':t['id']})
        brief=self.m.worker(self.main,{'op':'work-brief'})
        self.assertEqual(brief['tasks']['tasks'][0]['id'],t['id'])
        self.assertEqual(brief['sites']['sites'],[])
        self.assertEqual(self.m.browser.calls,[])
        with self.assertRaises(Conflict):plan('invented-connector')

    def test_worker_timing_is_labelled_not_misrepresented_as_network(self):
        result=self.m.worker(self.main,{'op':'timing-sample','workerTotalMs':5200,'guestRoundTripMs':150})
        self.assertEqual(result['unattributedOutsideGuestMs'],5050)
        self.assertIn('not pure network',result['scope'])
        diag=self.m.worker(self.main,{'op':'diagnostics'})
        self.assertEqual(diag['layers']['workerTotalMs']['samples'],1)
        for v in (float('nan'),float('inf'),-1,True):
            with self.assertRaises(Conflict):self.m.worker(self.main,{'op':'timing-sample','workerTotalMs':v,'guestRoundTripMs':10})


class LegacyGuardTests(unittest.TestCase):
    def test_guard_uses_fresh_existing_url_without_another_browser_command(self):
        check=Mock();cli=Mock()
        guard_site('fill',['e1','private text'],'https://portal.example',cli,check)
        check.assert_called_once_with('https://portal.example');cli.assert_not_called()
        check.side_effect=ValueError('Site is blocked')
        with self.assertRaises(ValueError):guard_site('click',['e1'],'https://portal.example',cli,check)

    def test_read_and_recovery_are_available_during_challenge(self):
        check=Mock(side_effect=ValueError('blocked'));cli=Mock()
        for cmd,args in (('snapshot',[]),('tab-select',['1']),('upload',[]),('press',['Escape']),('dialog-dismiss',[])):
            guard_site(cmd,args,'https://portal.example',cli,check)
        check.assert_not_called();cli.assert_not_called()
        guard_site('goto',['https://another.example'],'https://portal.example',cli,Mock())

    def test_gate_receives_only_origin_not_form_queries_or_fragments(self):
        check=Mock()
        guard_site('click',['e1'],'https://portal.example/form?code=PUBLIC-JOB&session=PRIVATE#fragment',Mock(),check)
        check.assert_called_once_with('https://portal.example')

    def test_independent_work_cannot_navigate_away_from_a_blocked_form(self):
        def check(url):
            if url=='https://portal.example':raise ValueError('Human form is parked')
        with self.assertRaises(ValueError):guard_site('goto',['https://independent.example/'],'https://portal.example/form',Mock(),check)
        guard_site('tab-new',['https://independent.example/'],'https://portal.example/form',Mock(),check)

    def test_unknown_current_tab_fails_closed_without_guessing(self):
        cli=Mock(return_value=subprocess.CompletedProcess([],1,'unavailable'))
        check=Mock()
        with self.assertRaises(ValueError):guard_site('fill',['e1','text'],None,cli,check)
        check.assert_not_called()


if __name__=='__main__':unittest.main()
