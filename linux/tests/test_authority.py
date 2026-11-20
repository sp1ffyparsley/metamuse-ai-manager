import base64,contextlib,copy,http.client,json,secrets,tempfile,threading,time,unittest,uuid
from pathlib import Path
from muse_monitor.server import Monitor,Handler,ThreadingHTTPServer
from muse_monitor.store import Store,Denied,Conflict,digest
from muse_monitor.workflow import deadline,match_challenge
from muse_monitor.discovery import Boards,canonical_url

class FakeBrowser:
    def manual_login(self,wid):return False
    def legacy_waiting(self,wid):return False
    def __init__(self,root):self.root=Path(root);self.locks={w:threading.RLock() for w in ('jobs','research','scholarships')};self.calls=[];self.frames={};self.cache={};self.input_modes={}
    def human_mode(self,wid,human,locked=False):self.input_modes[wid]=human;self.calls.append((wid,'human' if human else 'agent'))
    @contextlib.contextmanager
    def exclusive(self,wid,wait=False):
        if not self.locks[wid].acquire(timeout=5 if wait else 0):raise Conflict('Busy')
        try:yield
        finally:self.locks[wid].release()
    def paths(self,wid):
        runtime=self.root/wid;runtime.mkdir(exist_ok=True)
        return {},runtime,runtime,runtime
    def legacy_owned(self,wid):return False
    def marker(self,*args):pass
    def describe(self,wid,force=False,locked=False):return {'pages':[{'id':'p1','generation':'doc1','url':'https://example.com/'}]}
    def exact(self,wid,page,doc,locked=False):
        if page!='p1' or doc!='doc1':raise Conflict('Stale page')
    def action(self,wid,request,locked=False):
        self.calls.append((wid,request['kind']))
        return {'page':{'id':'p1','generation':'doc1','title':'Fixture'}}
    def frame(self,wid,page,doc):return base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII=')

class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.tokens={k:secrets.token_urlsafe(32) for k in ('main','human','alt')}
        self.config={'state':self.tmp.name,'runtime':self.tmp.name,'nodeBin':'/not-used','capacity':2,'principals':{k:{'hash':digest(v),'role':{'main':'main','human':'human','alt':'alt'}[k],'workspaces':['jobs','research','scholarships']} for k,v in self.tokens.items()},'workspaces':{k:{'name':k,'session':k,'base':self.tmp.name+'/'+k} for k in ('jobs','research','scholarships')}}
        self.m=Monitor(self.config);self.m.browser=FakeBrowser(self.tmp.name)
        for wid in self.config['workspaces']:self.m.observed[wid]={'utc':time.time(),'pages':[{'id':'p1','generation':'doc1','title':'Fixture'}]}
        self.main=self.m.principal(self.tokens['main']);self.human=self.m.principal(self.tokens['human'])
    def tearDown(self):self.m.store.db.close();self.tmp.cleanup()
    def request(self,**kw):return dict(requestId=str(uuid.uuid4()),generation=1,page='p1',pageGeneration='doc1',**kw)
    def test_lease_fences_capacity_and_expiry(self):
        a=self.m.worker(self.main,{'op':'claim','workspace':'jobs'})
        self.m.worker(self.main,{'op':'claim','workspace':'research'})
        with self.assertRaises(Conflict):self.m.worker(self.main,{'op':'claim','workspace':'scholarships'})
        with self.assertRaises(Conflict):self.m.worker(self.main,{'op':'claim','workspace':'jobs'})
        with self.assertRaises(Denied):self.m.store.agent('jobs','main','wrong',a['generation'])
        with self.assertRaises(Denied):self.m.store.agent('research','main',a['token'],a['generation'])
        with self.m.store.tx() as db:db.execute('UPDATE workspace SET lease_until=0 WHERE id="jobs"')
        with self.assertRaises(Denied):self.m.store.agent('jobs','main',a['token'],a['generation'])
    def test_retired_resource_cannot_be_claimed_from_old_journal(self):
        self.m.config['workspaces'].pop('research')
        self.m.config['workspaces']['jobs']['name']='Main'
        self.m.config['workspaces']['scholarships']['name']='Secondary'
        status=self.m.status(self.main)
        self.assertEqual([w['name'] for w in status['workspaces']],['Main','Secondary'])
        with self.assertRaises(Denied):self.m.worker(self.main,{'op':'claim','workspace':'research'})
        self.m.worker(self.main,{'op':'claim','workspace':'jobs'})
        self.m.worker(self.main,{'op':'claim','workspace':'scholarships'})
    def test_application_contact_uses_professional_policy_not_browser_account(self):
        from muse_monitor.workflow import prepare_packet
        self.m.config['applicationIdentity']={'primaryEmail':'professional@example.com'}
        fact={'value':'Fixture Applicant','source':'approved fixture','version':'1','verification':'verified','approvalScope':'application','path':'name'}
        packet={'canonicalItem':'fixture','kind':'jobs','applicant':'Fixture Applicant','sourceFacts':{k:'fixture' for k in ('employer','requisition','description','officialURL','eligibility')},'sourceTimestamps':['fixture-version-1'],'candidateFacts':[fact],'eligibility':'verified fixture','documents':[],'draftAnswers':{},'questions':[],'nextSafeAction':'review','contactEmail':'cloud@example.com'}
        self.assertFalse(prepare_packet(self.m,self.main,{'packet':packet})['ready'])
        packet['contactEmail']='professional@example.com'
        self.assertTrue(prepare_packet(self.m,self.main,{'packet':packet})['ready'])
    def test_watch_and_forged_actor_cannot_input(self):
        with self.assertRaises(Denied):self.m.input(self.human,'jobs',self.request(action={'kind':'text','text':'not sent'},actor='main'))
        self.assertEqual(self.m.browser.calls,[])
        with self.assertRaises(Denied):self.m.control(self.main,'jobs','pause',self.request())
    def test_pause_takeover_resume_and_stale_grants(self):
        claim=self.m.worker(self.main,{'op':'claim','workspace':'jobs'})
        _,runtime,_,_=self.m.browser.paths('jobs');(runtime/'lease').write_text('{"token":"old-legacy-fixture","until":9999999999}')
        body=self.request();body['generation']=claim['generation']
        paused=self.m.control(self.human,'jobs','pause',body)['status']['workspaces'][0]
        self.assertTrue(self.m.browser.input_modes['jobs'])
        self.assertFalse((runtime/'lease').exists(),'Pre-takeover legacy tokens must be fenced too')
        with self.assertRaises(Denied):self.m.store.agent('jobs','main',claim['token'],claim['generation'])
        body=self.request();body['generation']=paused['generation']
        grant=self.m.control(self.human,'jobs','takeover',body)['grant']
        body=self.request(action={'kind':'text','text':'fixture only'},grant=grant['token']);body['generation']=grant['generation']
        self.m.input(self.human,'jobs',body)
        with self.assertRaises(Conflict):self.m.input(self.human,'jobs',body)
        wrong=dict(body,requestId=str(uuid.uuid4()),pageGeneration='old')
        with self.assertRaises(Denied):self.m.input(self.human,'jobs',wrong)
        resume=self.request(grant=grant['token']);resume['generation']=grant['generation']
        self.m.control(self.human,'jobs','resume',resume)
        self.assertTrue(self.m.browser.input_modes['jobs'])
        next_claim=self.m.worker(self.main,{'op':'claim','workspace':'jobs'})
        self.assertFalse(self.m.browser.input_modes['jobs'])
        self.m.worker(self.main,dict(op='release',workspace='jobs',token=next_claim['token'],generation=next_claim['generation']))
        self.assertTrue(self.m.browser.input_modes['jobs'])
        with self.assertRaises(Denied):self.m.input(self.human,'jobs',dict(body,requestId=str(uuid.uuid4())))
    def test_restart_keeps_human_pause_but_revokes_grants(self):
        self.m.store.pause('jobs',1)
        grant=self.m.store.takeover('jobs','human','p1','doc1',2)
        self.m.store.db.close();self.m.store=Store(Path(self.tmp.name)/'journal.sqlite3')
        self.assertTrue(self.m.store.workspace('jobs')['paused'])
        with self.assertRaises(Denied):self.m.store.human('jobs','human',grant['token'],3,'p1','doc1')
    def test_manual_sign_in_is_not_exposed_as_automated_or_resumable(self):
        self.m.browser.manual_login=lambda wid:wid=='jobs'
        jobs=self.m.status(self.human)['workspaces'][0]
        self.assertEqual(jobs['state'],'paused');self.assertEqual(jobs['pages'],[])
        self.assertIn('automation disconnected',jobs['phase'])
        self.m.store.pause('jobs',1)
        with self.assertRaises(Conflict):self.m.control(self.human,'jobs','resume',self.request())
    def test_lost_response_intent_cannot_replay(self):
        rid=str(uuid.uuid4());self.m.store.begin(rid,'main','jobs','click');self.m.store.finish(rid,uncertain=True)
        with self.assertRaises(Conflict):self.m.store.begin(rid,'main','jobs','click')
        self.m.store.pause('jobs',1)
        with self.assertRaises(Conflict):self.m.store.takeover('jobs','human','p1','doc1',2)
        self.m.store.reconcile(rid,'main',{'outcome':'not submitted','evidence':'fixture receipt inspection'})
        self.m.store.takeover('jobs','human','p1','doc1',2)
    def test_item_claim_and_checkpoint_fresh_agent(self):
        self.m.store.capture('canonical-1','jobs',{'url':'https://example.com/'})
        self.m.store.claim_item('canonical-1','main')
        with self.assertRaises(Conflict):self.m.store.claim_item('canonical-1','other')
        checkpoint={'schema':1,'canonicalItem':'canonical-1','applicant':'fixture','workspace':'jobs','phase':'draft','artifacts':[],'pendingIntent':None,'nextSafeAction':'inspect receipt','sync':'pending'}
        self.m.store.checkpoint('canonical-1','main',checkpoint)
        row=self.m.store.db.execute('SELECT data FROM checkpoint WHERE item="canonical-1"').fetchone()
        self.assertEqual(json.loads(row[0])['nextSafeAction'],'inspect receipt')
    def test_read_only_status_can_progress_during_another_workspace_action(self):
        acquired=threading.Event();release=threading.Event()
        def hold():
            with self.m.browser.exclusive('jobs'):acquired.set();release.wait(2)
        t=threading.Thread(target=hold);t.start();acquired.wait(1)
        start=time.monotonic()
        self.m.status(self.human)
        with self.m.browser.exclusive('research'):pass
        self.assertLess(time.monotonic()-start,.2);release.set();t.join()
    def test_http_boundary_rejects_unknown_pairing_and_browser_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.monitor=self.m
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            def get(token,origin=None):
                c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2);h={'Authorization':'Bearer '+token,'X-Muse-Protocol':'1'}
                if origin:h['Origin']=origin
                c.request('GET','/v1/status',headers=h);r=c.getresponse();status=r.status;r.read();c.close();return status
            self.assertEqual(get('wrong'),401)
            self.assertEqual(get(self.tokens['human'],'https://evil.invalid'),403)
            self.assertEqual(get(self.tokens['human']),200)
        finally:server.shutdown();server.server_close()
    def test_deadline_and_verification_code_identity(self):
        self.assertIsNone(deadline('2027-06-30','America/Los_Angeles')['exactCutoff'])
        messages=[{'service':'appone','recipient':'fixture@example.com','receivedAt':100,'task':'task-a','code':'111111'}]
        with self.assertRaises(Conflict):match_challenge(messages,'appone','fixture@example.com',90,'task-b')
        self.assertEqual(match_challenge(messages,'appone','fixture@example.com',90,'task-a'),'111111')
    def test_source_normalization_retains_cycle_and_distinguishes_unavailable(self):
        self.assertIn('cycle=2027',canonical_url('https://example.com/apply?cycle=2027&utm_source=test'))
        rows=Boards.normalize('lever','fixture',[{'id':'r1','text':'Role','hostedUrl':'https://example.com/job','categories':{'location':'NC'}}])
        self.assertEqual(rows[0]['requisition'],'r1');self.assertIn('unverified',rows[0]['eligibility'])
    def test_alias_deduplication_and_source_coverage_history(self):
        a=self.m.store.capture('official-id','jobs',{'url':'https://employer.example/job/1','note':'Original note'})
        b=self.m.store.capture('discovered-id','jobs',{'url':'https://employer.example/job/1','note':'New provenance'})
        self.assertEqual(a['id'],b['id'])
        self.m.store.claim_item(a['id'],'main')
        with self.assertRaises(Conflict):self.m.store.claim_item(b['id'],'other')
        self.m.store.source_attempt('core','covered','cursor-a')
        successful=self.m.store.health()['sources'][0]['last_success']
        self.m.store.source_attempt('core','unavailable')
        source=self.m.store.health()['sources'][0];self.assertEqual(source['last_success'],successful);self.assertEqual(source['cursor'],'cursor-a')
        blocked=source['blocked_since'];self.m.store.source_attempt('core','unavailable');self.assertEqual(self.m.store.health()['sources'][0]['blocked_since'],blocked)

if __name__=='__main__':unittest.main()
