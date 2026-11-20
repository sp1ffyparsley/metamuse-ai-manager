"""Authenticated private HUD/worker bridge. No raw CDP or open proxy."""
import argparse,base64,datetime,hashlib,hmac,json,math,os,re,secrets,socket,socketserver,ssl,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from . import VERSION
from .browser import Browser,BrowserError,Uncertain
from .store import Store,Denied,Conflict,digest

def iso(t=None):return datetime.datetime.fromtimestamp(time.time() if t is None else t,datetime.timezone.utc).isoformat().replace('+00:00','Z')
def safe_url(value):
    if not isinstance(value,str) or len(value)>4096:raise Conflict('Bounded URL required')
    p=urlsplit(value)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:raise Conflict('HTTP(S) URL without credentials required')
    if any(k.lower() in ('access_token','refresh_token','id_token','password','otp','auth','code') for k in parse_qs(p.query)):raise Conflict('Do not capture login challenges or tokens')
    return value

def private_data(value):
    if isinstance(value,dict):
        for k,v in value.items():
            if re.search(r'password|secret|token|otp|verification.?code',k,re.I):raise Conflict('Credentials and challenge codes must not be persisted')
            private_data(v)
    elif isinstance(value,list):
        for v in value:private_data(v)

class Monitor:
    def __init__(self,config):
        self.config=config;self.instance=uuid.uuid4().hex;self.sequence=0;self.seq_lock=threading.Lock()
        self.root=Path(config['state']);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.store=Store(self.root/'journal.sqlite3');self.browser=Browser(config)
        from .tasks import Tasks
        self.tasks=Tasks(self)
        from .sites import Sites
        self.sites=Sites(self)
        from .diagnostics import Diagnostics
        self.diagnostics=Diagnostics(self.root/'diagnostics')
        self.observed={};self.activity={};self.stop=threading.Event();self.sample_threads=[]
        for wid in config['workspaces']:self.store.add_workspace(wid)
        from .discovery import Boards
        self.boards=Boards()
        self.google=None;self.outbox=None
        if config.get('google'):
            from .google_bridge import Google,Sheets,Outbox
            self.google=Google(config['google']['token'])
            schema=Path(config.get('schemas','/opt/muse-monitor/canonical-schemas.json'))
            if schema.exists():self.outbox=Outbox(self.store,Sheets(self.google,json.loads(schema.read_text())))
            if not Path(config['google']['token']).exists():self.store.incident('google-authorization','sync',None,{'reason':'Google Desktop OAuth client/consent required; canonical synchronization not active'})
        from .connections import Connections
        self.connections=Connections(self)
        from .manager import Manager
        self.manager=Manager(self)

    def principal(self,token):
        for actor,spec in self.config['principals'].items():
            if hmac.compare_digest(digest(token),spec['hash']):return {'id':actor,**spec}
        raise Denied('Pairing rejected')

    def scope(self,actor,wid,human=False):
        if wid not in self.config['workspaces'] or wid not in actor.get('workspaces',[]):raise Denied('Workspace scope denied')
        if human and actor['role']!='human':raise Denied('Human control required')

    def sample(self,wid):
        if self.browser.manual_login(wid):return
        if self.browser.legacy_waiting(wid):return
        try:
            with self.browser.exclusive(wid,wait=False):
                if self.browser.legacy_waiting(wid):return
                state=self.store.workspace(wid)
                agent_active=self.browser.legacy_owned(wid) or bool(state['owner'] and (state['lease_until'] or 0)>time.time())
                human=bool(state['paused'] or not agent_active)
                if not self.browser.input_mode_matches(wid,human):self.browser.human_mode(wid,human,locked=True)
                desc=self.browser.describe(wid,True,locked=True)
            prior=self.observed.get(wid)
            if prior and prior.get('browser')!=desc['browser']:
                with self.store.tx() as db:db.execute('UPDATE workspace SET generation=generation+1,lease_hash=NULL,lease_until=NULL,owner=NULL,human_hash=NULL WHERE id=?',(wid,))
            self.observed[wid]=dict(desc,error=None)
            self.store.incident('browser-'+wid,'browser',wid,{'reason':'Browser responsive'},resolved=True)
        except Conflict:return
        except (BrowserError,OSError,ValueError):
            old=self.observed.get(wid,{})
            self.observed[wid]=dict(old,error='Browser control unavailable; inspect workspace')
            self.store.incident('browser-'+wid,'browser',wid,{'reason':'Browser control unavailable'})

    def start(self):
        def loop(wid):
            try:self.browser.start_additional(wid)
            except (BrowserError,Conflict,OSError):pass
            while not self.stop.is_set():
                _,_,base,_=self.browser.paths(wid)
                state=self.store.workspace(wid)
                if (base/'paused').exists() and not state['paused']:
                    try:self.store.pause(wid,state['generation'])
                    except Conflict:pass
                self.browser.marker(wid,self.store.workspace(wid))
                self.sample(wid);self.stop.wait(3 if self.browser.legacy_owned(wid) or state['owner'] or state['paused'] else 8)
        for wid in self.config['workspaces']:
            t=threading.Thread(target=loop,args=(wid,),daemon=True);t.start();self.sample_threads.append(t)
        def sync():
            # Infrastructure only: never schedules discovery or applications.
            while not self.stop.wait(60):
                if self.outbox and self.config.get('google',{}).get('productionEnabled') and Path(self.config['google']['token']).exists():
                    try:self.outbox.step()
                    except (OSError,ValueError,Conflict):self.store.incident('sync-failed','sync',None,{'reason':'Canonical synchronization needs reconciliation'})
        threading.Thread(target=sync,daemon=True).start()

    def status(self,actor):
        rows=[];now=time.time();health=self.store.health();queued=len(self.next_work(actor))
        for wid,spec in self.config['workspaces'].items():
            if wid not in actor.get('workspaces',[]):continue
            w=self.store.workspace(wid);seen=self.observed.get(wid,{});active=self.activity.get(wid)
            cache=self.browser.cache.get(wid)
            if cache and cache.get('utc',0)>seen.get('utc',0):seen=dict(cache,error=None)
            manual=self.browser.manual_login(wid)
            fresh=now-seen.get('utc',0)<15 and not seen.get('error') and not manual
            state='waiting' if fresh else 'offline';owner=None;role=None;phase='No observed browser action'
            blocker=seen.get('error') or (None if fresh else 'No recent successful browser observation')
            if self.browser.legacy_owned(wid):owner='main';role='legacy browser worker';phase='Browser lease held; cloud reasoning unobserved'
            if w['owner'] and (w['lease_until'] or 0)>now:owner=w['owner'];role=self.config['principals'].get(owner,{}).get('role');phase='Workspace lease held'
            if active:state='working';phase=active['command']
            if w['paused']:
                state='human' if w['human_hash'] and (w['human_until'] or 0)>now else 'paused';phase='Human pause persists until explicit release'
                if state=='human':owner=w['human_actor'];role='human'
            uncertain=any(x['workspace']==wid for x in health['uncertain'])
            if uncertain:state='needs-you';blocker='Uncertain action requires outcome reconciliation'
            if manual:
                state='paused';phase='Human Google sign-in: ordinary Chrome, automation disconnected';blocker='Finish the attended sign-in handoff before resuming agents'
            rows.append({'id':wid,'name':spec['name'],'generation':w['generation'],'state':state,'actor':owner,'role':role,'task':w['task'],'phase':phase,'blocker':blocker,'observedAt':iso(seen.get('utc',0)),'actionStartedAt':iso(active['started']) if active else None,'queue':queued,'pendingSync':health['pendingSync'],'recordUrl':None,'pages':seen.get('pages',[]) if fresh else []})
        with self.seq_lock:self.sequence+=1;sequence=self.sequence
        return {'protocol':1,'instance':self.instance,'sequence':sequence,'observedAt':iso(),'revision':self.config.get('revision',VERSION),'workspaces':rows,'connections':{'google':'authorized' if self.config.get('google') and Path(self.config['google']['token']).exists() else 'authorization-required','enrolledWorkers':['main'],'pendingEnrollment':['alt','dad','sister']}}

    def exact(self,wid,body):
        page=body.get('page');document=body.get('pageGeneration')
        if not page or not document:raise Conflict('Select an exact observed page')
        self.browser.exact(wid,page,document)
        return page,document

    def legacy_settled(self,wid):
        _,runtime,_,_=self.browser.paths(wid)
        try:events=json.loads((runtime/'request-history.json').read_text())
        except (OSError,ValueError):return
        actions=[e for e in events if e.get('command')!='list']
        if not actions or actions[-1].get('exit')!=124:return
        last=actions[-1];rid='legacy-'+digest(json.dumps(last,sort_keys=True))[:32]
        with self.store.lock:row=self.store.db.execute('SELECT state FROM intent WHERE id=?',(rid,)).fetchone()
        if not row:
            self.store.begin(rid,'main',wid,last['command']);self.store.finish(rid,uncertain=True)
        elif row['state']=='reconciled':return
        raise Conflict('Legacy command outcome requires Main to inspect evidence and reconcile '+rid)

    def control(self,actor,wid,kind,body):
        self.scope(actor,wid,True);rid=body.get('requestId');gen=body.get('generation')
        self.store.begin(rid,actor['id'],wid,kind);grant=None
        try:
            if kind=='pause':
                self.store.pause(wid,gen)
                self.browser.marker(wid,self.store.workspace(wid))
                # Admission is closed before waiting for the old action. No
                # acknowledgment or human grant while its outcome is unknown.
                with self.browser.exclusive(wid,wait=True):
                    _,runtime,_,_=self.browser.paths(wid)
                    self.legacy_settled(wid)
                    (runtime/'lease').unlink(missing_ok=True)
                    self.browser.human_mode(wid,True,locked=True)
            elif kind=='takeover':
                page,doc=self.exact(wid,body)
                # This control request itself is not an unresolved page action.
                self.store.finish(rid)
                with self.browser.exclusive(wid):
                    self.legacy_settled(wid)
                    self.browser.human_mode(wid,True,locked=True)
                    grant=self.store.takeover(wid,actor['id'],page,doc,gen)
                grant['expiresAt']=iso(grant['expiresAt'])
            elif kind=='resume':
                if self.browser.manual_login(wid):raise Conflict('Human sign-in is still open; finish that handoff first')
                page,doc=self.exact(wid,body)
                self.store.finish(rid)
                with self.browser.exclusive(wid):
                    self.store.human(wid,actor['id'],body.get('grant'),gen,page,doc)
                    self.browser.human_mode(wid,True,locked=True)
                    try:self.store.resume(wid,actor['id'],body.get('grant'),gen,page,doc)
                    except Exception:
                        self.browser.human_mode(wid,True,locked=True)
                        raise
                    _,_,base,_=self.browser.paths(wid);(base/'paused').unlink(missing_ok=True)
            else:raise Conflict('Unknown control action')
            self.store.finish(rid)
            self.diagnostics.emit('human_'+kind,'controller',workspace=wid,actor=actor['id'],generation=self.store.workspace(wid)['generation'],outcome='ok')
        except Uncertain:
            self.store.finish(rid,uncertain=True);raise
        except Exception:
            self.store.finish(rid,{'denied':True});raise
        return {'status':self.status(actor),'grant':grant}

    def input(self,actor,wid,body):
        self.scope(actor,wid,True)
        page,doc=body.get('page'),body.get('pageGeneration')
        self.store.human(wid,actor['id'],body.get('grant'),body.get('generation'),page,doc)
        self.browser.exact(wid,page,doc)
        action=body.get('action',{});kind=action.get('kind')
        request={'kind':kind,'page':page,'document':doc}
        if kind=='click':
            x,y=action.get('x'),action.get('y')
            if any(not isinstance(n,(int,float)) or not math.isfinite(n) or n<0 or n>1 for n in (x,y)):raise Conflict('Invalid normalized coordinates')
            frame=self.browser.frames.get((wid,page,doc))
            if not frame or time.monotonic()-frame['at']>5:raise Conflict('View a current frame before clicking')
            request.update(kind='point',x=x,y=y,width=frame['width'],height=frame['height'])
        elif kind=='text':
            text=action.get('text')
            if not isinstance(text,str) or len(text)>4096:raise Conflict('Bounded text required')
            request['text']=text
        elif kind=='key':
            if action.get('key') not in ('Tab','Enter','Escape','Backspace','ArrowUp','ArrowDown'):raise Conflict('Unsupported key')
            request['key']=action['key']
        else:raise Denied('Unsupported input action')
        with self.browser.exclusive(wid):
            self.store.human(wid,actor['id'],body.get('grant'),body.get('generation'),page,doc)
            rid=body.get('requestId');self.store.begin(rid,actor['id'],wid,'human-'+kind)
            self.activity[wid]={'command':'human-'+kind,'started':time.time()}
            try:
                result=self.browser.action(wid,request,locked=True);self.store.finish(rid,{'observed':True})
            except (Uncertain,BrowserError):self.store.finish(rid,uncertain=True);raise
            finally:self.activity.pop(wid,None)
        return {'accepted':True,'page':result.get('page')}

    def extension(self,actor,body):
        if not actor['id'].startswith('extension-') or actor['role']!='human' or len(actor['workspaces'])!=1:raise Denied('Authenticated installed extension required')
        wid=actor['workspaces'][0];op=body.get('op');w=self.store.workspace(wid)
        if op=='rules':
            from .rules import shared
            return shared(self,actor,wid,body)
        if op=='status':return {'status':self.status(actor),'workspace':wid,'google':'authorized' if Path(self.config['google']['token']).exists() else 'authorization-required'}
        if op=='capture':return self.worker(actor,dict(body,op='capture'))
        if op in ('pause','takeover','resume'):return self.control(actor,wid,op,body)
        if op=='adopt':
            if not w['paused'] or (w['human_hash'] and w['human_actor']!=actor['id']):raise Denied('Pause this idle workspace before explicitly managing a tab')
            self.browser.exact(wid,body.get('page'),body.get('pageGeneration'))
            return self.browser.action(wid,{'kind':'adopt','page':body['page'],'document':body['pageGeneration']})
        if op=='tab-permit':
            if not w['paused'] or (w['human_hash'] and w['human_actor']!=actor['id']) or wid in self.activity:raise Denied('Pause and settle the workspace first')
            if any(i['workspace']==wid for i in self.store.health()['uncertain']):raise Conflict('Uncertain work remains protected')
            tabs=body.get('tabs',[])
            if not isinstance(tabs,list) or not tabs or len(tabs)>100:raise Conflict('Bounded exact tab inventory required')
            seen=self.browser.describe(wid,True);allowed=[]
            for tab in tabs:
                url=safe_url(tab.get('url'))
                matches=[p for p in seen['pages'] if p.get('url')==url]
                if len(matches)!=1 or not matches[0].get('managed') or tab.get('dirty') is not False or tab.get('pinned') or tab.get('pending') or not tab.get('documentId'):continue
                allowed.append({'id':tab['id'],'url':url,'windowId':tab['windowId'],'documentId':tab['documentId'],'page':matches[0]['id'],'generation':matches[0]['generation']})
            if not allowed:raise Denied('Unknown, dirty, duplicate, pinned or unowned tabs stay protected')
            rid=body.get('requestId');self.store.begin(rid,actor['id'],wid,'extension-group')
            return {'requestId':rid,'workspaceGeneration':w['generation'],'expiresAt':time.time()+5,'tabs':allowed}
        if op=='tab-finish':
            with self.store.lock:row=self.store.db.execute('SELECT * FROM intent WHERE id=?',(body.get('requestId'),)).fetchone()
            if not row or row['actor']!=actor['id'] or row['kind']!='extension-group':raise Denied('Operation belongs to another actor')
            self.store.finish(body['requestId'],{'observed':bool(body.get('success'))},uncertain=not body.get('success'))
            return {'recorded':True}
        raise Denied('Unsupported extension operation')

    def next_work(self,actor):
        rows=self.store.next_work(actor['id'])
        return [r for r in rows if '_museTask' not in r['data'] or (actor['role'] in ('main','human') and not actor['id'].startswith('extension-') and (r['data']['_museTask']['packet'].get('workspace') is None or r['data']['_museTask']['packet']['workspace'] in actor.get('workspaces',[])) and not self.sites.task_problem(r['data']['_museTask']['packet']))]

    def worker(self,actor,body):
        op=body.get('op');wid=body.get('workspace','jobs')
        if op=='diagnostics':
            if actor['role'] not in ('main','human'):raise Denied('Execution or attended diagnostics scope required')
            return dict(self.diagnostics.summary(),revision=self.config.get('revision',VERSION))
        if op=='rules':
            from .rules import shared
            return shared(self,actor,wid,body)
        if op=='capabilities':return {'protocol':1,'actor':actor['id'],'role':actor['role'],'workspaces':actor['workspaces'],'workspaceNames':{k:v['name'] for k,v in self.config['workspaces'].items() if k in actor['workspaces']},'applicationIdentity':self.config.get('applicationIdentity',{}) if actor['role']=='main' else {},'ops':['status','claim','heartbeat','release','browser','capture','next-work','claim-item','checkpoint','checkpoint-read','prepare','artifact','artifact-import','discover','sync-enqueue','google-status','reconcile','health','diagnostics','task','site','work-brief','performance','timing-sample'],'tasks':{'actions':['guide','plan','save','list','read','block','unblock','receipt','finish'],'topics':'Any subject; topic is a label, not another browser profile','recordLimitBytes':32768,'storage':'Existing item journal; no scheduler or new daemon','instructions':'TASK-WORKFLOW.md; prepare before execution, checkpoint, block precisely, verify each success criterion before finish'},'enrollment':{'main':'existing restricted SSH identity','alt':'not enrolled','dad':'not enrolled','sister':'not enrolled'}}
        if op=='task':return self.tasks.request(actor,body)
        if op=='site':return self.sites.request(actor,body)
        if op=='work-brief':
            self.tasks.authorize(actor)
            return {'revision':self.config.get('revision',VERSION),'tasks':self.tasks.listing(actor,body),
                    'sites':self.sites.request(actor,{'action':'list'}),
                    'workspaceNames':{k:v['name'] for k,v in self.config['workspaces'].items() if k in actor['workspaces']},
                    'routine':'Use connected tools for research/records/documents when authorized; browser only for website steps. Reuse pinned profiles; park unresolved challenges and continue independent work. Read the guide once per revision.'}
        if op=='performance':
            self.tasks.authorize(actor);self.scope(actor,wid)
            return self.browser.action(wid,{'kind':'performance','page':body.get('page'),'document':body.get('pageGeneration')})
        if op=='timing-sample':
            self.tasks.authorize(actor)
            values={k:body.get(k) for k in ('workerTotalMs','guestRoundTripMs')}
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=300000 for v in values.values()):raise Conflict('Bounded timing milliseconds required')
            if values['workerTotalMs']<values['guestRoundTripMs']:raise Conflict('Worker total cannot be shorter than the guest measurement')
            self.diagnostics.emit('worker_probe','worker-reported',**values)
            return {'recorded':True,'unattributedOutsideGuestMs':values['workerTotalMs']-values['guestRoundTripMs'],'scope':'Worker-reported wall time includes proxy/SSH/tool/startup; this difference is not pure network latency'}
        if op=='status':return self.status(actor)
        if op=='health':return self.store.health()
        if op=='local-resume':
            if actor['id']!='local-human':raise Denied('Attended desktop control required')
            self.scope(actor,wid,True)
            if self.browser.manual_login(wid):raise Conflict('Human sign-in handoff must finish first')
            if self.store.health()['uncertain']:raise Conflict('Reconcile uncertain outcomes before local resume')
            with self.browser.exclusive(wid):
                self.browser.human_mode(wid,True,locked=True)
                with self.store.tx() as db:db.execute('UPDATE workspace SET paused=0,generation=generation+1,human_hash=NULL,human_until=NULL,owner=NULL,lease_hash=NULL,lease_until=NULL WHERE id=?',(wid,))
                _,_,base,_=self.browser.paths(wid);(base/'paused').unlink(missing_ok=True)
                self.browser.marker(wid,self.store.workspace(wid))
            return {'resumed':True}
        if op=='next-work':return self.next_work(actor)
        if op=='google-status':return {'authorization':'available' if self.config.get('google') and Path(self.config['google']['token']).exists() else 'required','writesEnabled':self.config.get('google',{}).get('productionEnabled',False),'pendingSync':self.store.health()['pendingSync']}
        if op=='discover':
            if actor['role'] not in ('main','alt'):raise Denied('Research worker scope required')
            if body.get('provider')=='jobspy':
                from .discovery import bounded_jobspy
                result=bounded_jobspy(body.get('terms'),body.get('location','North Carolina, USA'),min(int(body.get('limit',20)),30))
            elif body.get('provider')=='greenhouse-detail':result=self.boards.detail(body.get('board'),body.get('requisition'))
            else:result=self.boards.fetch(body.get('provider'),body.get('board'))
            self.store.source_attempt(str(body.get('provider'))+':'+str(body.get('board','search')),result['state'])
            return result
        if op=='artifact':
            if actor['role']!='main':raise Denied('Execution preparation scope required')
            from .workflow import generate_pdf_artifact
            return generate_pdf_artifact(self.root,body.get('approved',{}),body.get('filename',''))
        if op=='artifact-import':
            if actor['role']!='main' or not self.google:raise Denied('Authorized execution preparation scope required')
            from .artifacts import import_drive_pdf
            return import_drive_pdf(self.google,self.root,body.get('fileId'),body.get('filename'),body.get('sha256'))
        if op=='checkpoint-read':
            if actor['role']!='main':raise Denied('Execution checkpoint scope required')
            with self.store.lock:r=self.store.db.execute('SELECT data,version FROM checkpoint WHERE item=?',(body.get('item'),)).fetchone()
            if not r:raise Conflict('Checkpoint not found')
            return {'checkpoint':json.loads(r['data']),'version':r['version']}
        if op=='sync-enqueue':
            if actor['role']!='main' or not self.outbox:raise Denied('Canonical writer is not available to this actor')
            name=body.get('table');changes=body.get('changes',{})
            if name not in self.outbox.sheets.schema:raise Denied('Derived or unknown table is not writable')
            self.outbox.enqueue(body.get('requestId'),name,body.get('canonicalId'),changes,body.get('insert') is True)
            return {'state':'pending','authorization':'available' if Path(self.config['google']['token']).exists() else 'required','writesEnabled':self.config['google'].get('productionEnabled',False)}
        if op=='reconcile':self.store.reconcile(body.get('requestId'),actor['id'],body.get('evidence'));return {'reconciled':True}
        if op=='capture':
            data=body.get('item',{});private_data(data);url=safe_url(data.get('url'));kind=data.get('kind','unclassified')
            canonical=data.get('canonicalId') or digest(kind+'|'+url)
            return self.store.capture(canonical,kind,data)
        if op=='claim-item':
            if actor['role']!='main':raise Denied('Only the execution owner can claim an application')
            with self.store.lock:row=self.store.db.execute('SELECT data FROM item WHERE id=?',(body.get('item'),)).fetchone()
            if row and '_museTask' in json.loads(row['data']):
                task=self.tasks.request(actor,{'action':'read','id':body.get('item')})
                if task['state']!='ready' or task['readinessProblems']:raise Conflict('Task is not ready; resolve its questions and exact file versions first')
            return self.store.claim_item(body.get('item'),actor['id'],body.get('revision'))
        if op=='checkpoint':private_data(body.get('checkpoint'));self.store.checkpoint(body.get('item'),actor['id'],body.get('checkpoint'));return {'saved':True}
        if op=='prepare':
            from .workflow import prepare_packet
            return prepare_packet(self,actor,body)
        self.scope(actor,wid)
        with self.browser.exclusive(wid,wait=True):
            if op=='claim':
                if body.get('task'):
                    with self.store.lock:taskrow=self.store.db.execute('SELECT data FROM item WHERE id=?',(body['task'],)).fetchone()
                    taskdata=json.loads(taskrow['data']).get('_museTask') if taskrow else None
                    if taskdata:
                        packet=taskdata['packet']
                        if packet.get('execution','browser' if packet.get('workspace') else 'offline')!='browser':raise Conflict('This task uses connected tools or offline preparation; no browser lease is needed')
                        if packet.get('workspace')!=wid:raise Conflict('Claim the task\'s saved browser workspace')
                        if self.sites.task_problem(packet):raise Conflict(self.sites.task_problem(packet))
                self.legacy_settled(wid)
                if self.browser.legacy_owned(wid):raise Conflict('Existing Main controller holds this browser')
                _,_,base,_=self.browser.paths(wid)
                if (base/'paused').exists():raise Conflict('Human pause is active')
                from .legacy import admission
                _,runtime,_,_=self.browser.paths(wid)
                with admission(runtime):
                    legacy=sum(self.browser.legacy_owned(k) for k in self.config['workspaces'])
                    result=self.store.claim(wid,actor['id'],body.get('task'),self.config.get('capacity',2)-legacy)
                    try:self.browser.human_mode(wid,False,locked=True)
                    except Exception:
                        self.store.release(wid,actor['id'],result['token'],result['generation'])
                        raise
                    self.browser.marker(wid,self.store.workspace(wid))
                    self.diagnostics.emit('lease_start','controller',workspace=wid,actor=actor['id'],runId=digest(result['token'])[:16],generation=result['generation'])
                    return result
            if op=='heartbeat':self.store.heartbeat(wid,actor['id'],body.get('token'),body.get('generation'));self.browser.marker(wid,self.store.workspace(wid));return {'renewed':True}
            if op=='release':
                self.store.release(wid,actor['id'],body.get('token'),body.get('generation'));self.browser.marker(wid,self.store.workspace(wid))
                try:self.browser.human_mode(wid,True,locked=True)
                except (BrowserError,Uncertain,Conflict,OSError):self.browser.input_modes.pop(wid,None)
                self.diagnostics.emit('lease_end','controller',workspace=wid,actor=actor['id'],runId=digest(body.get('token',''))[:16],outcome='released')
                return {'released':True}
            if op!='browser':raise Conflict('Unknown operation')
            self.store.agent(wid,actor['id'],body.get('token'),body.get('generation'))
            req=dict(body.get('action',{}));kind=req.get('kind')
            if kind not in ('snapshot','new','goto','fill','click','check','select','upload','close'):raise Denied('Unsupported bounded browser operation')
            if kind in ('new','goto'):req['url']=safe_url(req.get('url'))
            if kind not in ('snapshot','new','goto') and actor['role']!='main':raise Denied('This worker cannot perform application mutations')
            if kind!='new':self.browser.exact(wid,req.get('page'),req.get('document'),locked=True)
            if kind in ('new','goto'):
                if kind=='goto':
                    current=next((p for p in self.browser.describe(wid,locked=True)['pages'] if p['id']==req.get('page')),None)
                    if current:self.sites.check(wid,current.get('url',''),body.get('siteContext','default'))
                self.sites.check(wid,req['url'],body.get('siteContext','default'))
            elif kind!='snapshot':
                pages=self.browser.describe(wid,locked=True)['pages']
                current=next((p for p in pages if p['id']==req.get('page')),None)
                if current:self.sites.check(wid,current.get('url',''),body.get('siteContext','default'))
            if kind in ('fill','click','check','select','upload') and not re.fullmatch(r'(?:f\d+)?e\d+',str(req.get('ref',''))):raise Conflict('Fresh complete aria reference required')
            if len(json.dumps(req))>16000:raise Conflict('Browser operation exceeds bound')
            if kind=='fill' and (not isinstance(req.get('text'),str) or len(req['text'])>4096):raise Conflict('Bounded text required')
            if kind=='upload':
                from .artifacts import LIMIT,valid_artifact
                candidate=self.root/'artifacts'/str(req.get('artifact',''));file=candidate.resolve()
                if candidate.is_symlink() or file.parent!=(self.root/'artifacts').resolve() or not file.is_file() or file.stat().st_size>LIMIT or not valid_artifact(file.name,file.read_bytes()):raise Denied('Use a registered private PDF/DOCX preparation artifact')
                req['path']=str(file)
            if kind=='close':
                if not body.get('taskComplete') or body.get('pendingIntent'):raise Denied('Explicit completion without pending work required')
                req['owner']=digest(actor['id']+'|'+str(self.store.workspace(wid)['task']))
            if kind=='new':req['owner']=digest(actor['id']+'|'+str(self.store.workspace(wid)['task']))
            rid=body.get('requestId');self.store.begin(rid,actor['id'],wid,kind)
            self.activity[wid]={'command':kind,'started':time.time()}
            started=time.monotonic();outcome='ok'
            try:
                result=self.browser.action(wid,req,locked=True)
                self.store.finish(rid,{'observed':True,'page':result.get('page') if isinstance(result,dict) else None})
                return result
            except (Uncertain,BrowserError):outcome='uncertain';self.store.finish(rid,uncertain=True);raise
            finally:
                self.activity.pop(wid,None)
                self.diagnostics.emit('command','controller',workspace=wid,actor=actor['id'],runId=digest(body.get('token',''))[:16],command=kind,elapsedMs=(time.monotonic()-started)*1000,outcome=outcome)

class Handler(BaseHTTPRequestHandler):
    server_version='MuseMonitor/1'
    def log_message(self,*args):pass
    def send(self,code,value,png=False):
        raw=value if png else json.dumps(value).encode()
        try:
            self.send_response(code);self.send_header('Content-Type','image/png' if png else 'application/json')
            self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Server-Timing','controller;dur='+str(round((time.monotonic()-getattr(self,'request_started',time.monotonic()))*1000,3)));self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ssl.SSLError):
            self.close_connection=True
    def serve(self):
        self.request_started=time.monotonic()
        monitor=self.server.monitor
        try:
            if self.headers.get('Origin') or self.headers.get('X-Muse-Protocol')!='1':raise Denied('Native authenticated protocol required')
            auth=self.headers.get('Authorization','')
            if not auth.startswith('Bearer ') or len(auth)>256:raise Denied('Pairing rejected')
            actor=monitor.principal(auth[7:]);route=urlsplit(self.path)
            if len(self.path)>2048:raise Conflict('Route exceeds bound')
            dedicated=getattr(self.server,'manager_worker_only',False)
            if dedicated != (route.path=='/v1/manager/worker') or (actor.get('role')=='manager-worker' and not dedicated):
                raise Denied('Dedicated manager runtime route required')
            if self.command=='GET' and route.path=='/v1/status':return self.send(200,monitor.status(actor))
            if self.command=='GET' and route.path=='/v1/health':return self.send(200,monitor.store.health())
            if self.command=='GET' and route.path=='/v1/connections':return self.send(200,monitor.connections.list(actor))
            if self.command=='GET' and route.path=='/v1/manager':return self.send(200,monitor.manager.operator(actor,None))
            m=re.fullmatch(r'/v1/workspaces/([a-z0-9-]+)/pages/([A-Za-z0-9_-]+)/frame',route.path)
            if self.command=='GET' and m:
                wid,page=m.groups();monitor.scope(actor,wid);query=parse_qs(route.query)
                if monitor.store.workspace(wid)['generation']!=int(query.get('generation',['-1'])[0]):raise Conflict('Workspace generation changed')
                return self.send(200,monitor.browser.frame(wid,page,query.get('document',[''])[0]),True)
            length=int(self.headers.get('Content-Length','0'))
            if self.command!='POST' or not 0<length<=65536:raise Conflict('Bounded JSON POST required')
            if self.headers.get_content_type()!='application/json':raise Conflict('JSON required')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise Conflict('JSON object required')
            if route.path=='/v1/connections/verify':return self.send(200,monitor.connections.verify(actor,body.get('id')))
            if route.path=='/v1/manager':return self.send(200,monitor.manager.operator(actor,body))
            if route.path=='/v1/manager/worker':return self.send(200,monitor.manager.worker(actor,body))
            if route.path=='/v1/worker':return self.send(200,monitor.worker(actor,body))
            if route.path=='/v1/extension':return self.send(200,monitor.extension(actor,body))
            m=re.fullmatch(r'/v1/workspaces/([a-z0-9-]+)/(pause|takeover|resume|input)',route.path)
            if not m:raise Conflict('Unknown route')
            wid,kind=m.groups()
            return self.send(200,monitor.input(actor,wid,body) if kind=='input' else monitor.control(actor,wid,kind,body))
        except Denied as e:self.send(401 if str(e)=='Pairing rejected' else 403,{'error':str(e)})
        except Conflict as e:self.send(409,{'error':str(e)})
        except (ValueError,KeyError,TypeError):self.send(409,{'error':'State or request changed; inspect and reconcile'})
        except (Uncertain,BrowserError,OSError):self.send(503,{'error':'Browser result uncertain or unavailable; inspect before retrying'})
        except Exception as error:
            monitor.store.incident('adapter-'+type(error).__name__,'adapter',None,{'reason':'Adapter failure: '+type(error).__name__})
            self.send(500,{'error':'Adapter failed; inspect controller health before retrying'})
    do_GET=serve
    do_POST=serve

class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=json.loads(Path(args.config).read_text());monitor=Monitor(config)
    server=ThreadingHTTPServer((config['bind'],config['port']),Handler);server.daemon_threads=True;server.monitor=monitor
    if config.get('tls'):
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.minimum_version=ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(config['tls']['certificate'],config['tls']['key']);server.socket=tls.wrap_socket(server.socket,server_side=True)
    path=Path(config['runtime'])/'muse-monitor.sock';path.unlink(missing_ok=True)
    unix=UnixServer(str(path),Handler);unix.monitor=monitor;path.chmod(0o600)
    threading.Thread(target=unix.serve_forever,daemon=True).start()
    manager=None
    if path_value:=config.get('manager',{}).get('workerSocket'):
        manager_path=Path(path_value)
        manager_path.unlink(missing_ok=True)
        manager=UnixServer(str(manager_path),Handler)
        manager.monitor=monitor
        manager.manager_worker_only=True
        manager_path.chmod(0o660)
        threading.Thread(target=manager.serve_forever,daemon=True).start()
    monitor.start()
    try:server.serve_forever()
    finally:
        monitor.stop.set();unix.shutdown();unix.server_close();server.server_close();path.unlink(missing_ok=True)
        if manager:
            manager.shutdown();manager.server_close();manager_path.unlink(missing_ok=True)

if __name__=='__main__':main()
