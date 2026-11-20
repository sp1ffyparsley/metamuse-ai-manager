"""Private durable coordination, checkpoints, and reconciliation outbox."""
import contextlib,hashlib,json,secrets,sqlite3,threading,time

class Denied(Exception):pass
class Conflict(Exception):pass

def digest(value):return hashlib.sha256(value.encode()).hexdigest()

class Store:
    def __init__(self,path):
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=3000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS workspace(id TEXT PRIMARY KEY,generation INTEGER NOT NULL DEFAULT 1,
         paused INTEGER NOT NULL DEFAULT 0, owner TEXT,lease_hash TEXT,lease_until REAL,
         task TEXT,human_hash TEXT,human_actor TEXT,human_page TEXT,human_document TEXT,human_until REAL);
        CREATE TABLE IF NOT EXISTS item(id TEXT PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,
         owner TEXT,until REAL,revision INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'queued');
        CREATE TABLE IF NOT EXISTS intent(id TEXT PRIMARY KEY,actor TEXT NOT NULL,workspace TEXT,
         kind TEXT NOT NULL,state TEXT NOT NULL,started REAL NOT NULL,finished REAL,result TEXT);
        CREATE TABLE IF NOT EXISTS checkpoint(item TEXT PRIMARY KEY,version INTEGER NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,result TEXT);
        CREATE TABLE IF NOT EXISTS incident(id TEXT PRIMARY KEY,kind TEXT NOT NULL,workspace TEXT,first REAL NOT NULL,last REAL NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source(id TEXT PRIMARY KEY,last_attempt REAL,last_success REAL,blocked_since REAL,cursor TEXT,state TEXT);
        CREATE TABLE IF NOT EXISTS setting(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alias(id TEXT PRIMARY KEY,canonical TEXT NOT NULL);
        ''')
        # A restart revokes authority; human pause remains durable. Running
        # intents become uncertain, never silently replayed.
        with self.tx() as db:
            db.execute('UPDATE workspace SET generation=generation+1,lease_hash=NULL,lease_until=NULL,owner=NULL,human_hash=NULL,human_until=NULL')
            db.execute("UPDATE intent SET state='uncertain' WHERE state='running'")

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:yield self.db;self.db.execute('COMMIT')
            except BaseException:self.db.execute('ROLLBACK');raise

    def add_workspace(self,wid):
        with self.tx() as db:db.execute('INSERT OR IGNORE INTO workspace(id) VALUES(?)',(wid,))

    def workspace(self,wid):
        with self.lock:r=self.db.execute('SELECT * FROM workspace WHERE id=?',(wid,)).fetchone()
        if not r:raise Conflict('Unknown workspace')
        return dict(r)

    def claim(self,wid,actor,task,capacity):
        token=secrets.token_urlsafe(32);now=time.time()
        with self.tx() as db:
            w=db.execute('SELECT * FROM workspace WHERE id=?',(wid,)).fetchone()
            if not w or w['paused']:raise Conflict('Workspace is human-paused')
            if db.execute("SELECT 1 FROM intent WHERE workspace=? AND state IN ('running','uncertain') LIMIT 1",(wid,)).fetchone():raise Conflict('Reconcile the previous uncertain action before a new claim')
            if (w['lease_until'] or 0)>now:raise Conflict('Workspace already owned')
            active=db.execute('SELECT count(*) FROM workspace WHERE lease_until>?',(now,)).fetchone()[0]
            if active>=capacity:raise Conflict('Browser worker capacity reached')
            db.execute('UPDATE workspace SET generation=generation+1,owner=?,task=?,lease_hash=?,lease_until=? WHERE id=?',(actor,task,digest(token),now+300,wid))
        return {'token':token,'generation':self.workspace(wid)['generation'],'expiresAt':now+300}

    def agent(self,wid,actor,token,generation):
        w=self.workspace(wid)
        if w['paused'] or w['generation']!=generation or w['owner']!=actor or (w['lease_until'] or 0)<=time.time() or not secrets.compare_digest(w['lease_hash'] or '',digest(token or '')):
            raise Denied('Missing, expired or fenced workspace lease')
        return w

    def heartbeat(self,wid,actor,token,generation):
        with self.tx() as db:
            self.agent(wid,actor,token,generation)
            db.execute('UPDATE workspace SET lease_until=? WHERE id=?',(time.time()+300,wid))

    def release(self,wid,actor,token,generation):
        with self.tx() as db:
            self.agent(wid,actor,token,generation)
            db.execute('UPDATE workspace SET owner=NULL,lease_hash=NULL,lease_until=NULL,task=NULL WHERE id=?',(wid,))

    def pause(self,wid,generation):
        with self.tx() as db:
            w=self.workspace(wid)
            if w['generation']!=generation:raise Conflict('Workspace generation changed')
            db.execute('UPDATE workspace SET paused=1,generation=generation+1,owner=NULL,lease_hash=NULL,lease_until=NULL,human_hash=NULL,human_until=NULL WHERE id=?',(wid,))

    def takeover(self,wid,actor,page,document,generation):
        token=secrets.token_urlsafe(32);until=time.time()+300
        with self.tx() as db:
            w=self.workspace(wid)
            if not w['paused'] or w['generation']!=generation:raise Conflict('Pause and refresh this workspace first')
            unresolved=db.execute("SELECT 1 FROM intent WHERE workspace=? AND state IN ('running','uncertain') LIMIT 1",(wid,)).fetchone()
            if unresolved:raise Conflict('An uncertain action requires reconciliation before takeover')
            db.execute('UPDATE workspace SET generation=generation+1,human_hash=?,human_actor=?,human_page=?,human_document=?,human_until=? WHERE id=?',(digest(token),actor,page,document,until,wid))
        return {'token':token,'workspace':wid,'generation':self.workspace(wid)['generation'],'expiresAt':until,'page':page,'pageGeneration':document}

    def human(self,wid,actor,token,generation,page,document):
        w=self.workspace(wid)
        if not w['paused'] or w['generation']!=generation or w['human_actor']!=actor or w['human_page']!=page or w['human_document']!=document or (w['human_until'] or 0)<=time.time() or not secrets.compare_digest(w['human_hash'] or '',digest(token or '')):
            raise Denied('Current exact-page human grant required')
        return w

    def resume(self,wid,actor,token,generation,page,document):
        with self.tx() as db:
            self.human(wid,actor,token,generation,page,document)
            if db.execute("SELECT 1 FROM intent WHERE workspace=? AND state IN ('running','uncertain') LIMIT 1",(wid,)).fetchone():raise Conflict('Settle outstanding actions before resuming')
            db.execute('UPDATE workspace SET paused=0,generation=generation+1,human_hash=NULL,human_until=NULL,human_actor=NULL WHERE id=?',(wid,))

    def begin(self,rid,actor,wid,kind):
        if not isinstance(rid,str) or not 8<=len(rid)<=128:raise Conflict('A stable requestId is required')
        with self.tx() as db:
            if db.execute('SELECT 1 FROM intent WHERE id=?',(rid,)).fetchone():raise Conflict('Request already recorded; reconcile rather than replay')
            db.execute('INSERT INTO intent(id,actor,workspace,kind,state,started) VALUES(?,?,?,?,?,?)',(rid,actor,wid,kind,'running',time.time()))

    def finish(self,rid,result=None,uncertain=False):
        with self.tx() as db:db.execute('UPDATE intent SET state=?,finished=?,result=? WHERE id=?',('uncertain' if uncertain else 'done',time.time(),json.dumps(result or {}),rid))

    def reconcile(self,rid,actor,evidence):
        if not isinstance(evidence,dict) or not evidence.get('outcome') or not evidence.get('evidence'):raise Conflict('Observed outcome and evidence are required')
        with self.tx() as db:
            row=db.execute('SELECT * FROM intent WHERE id=?',(rid,)).fetchone()
            if not row or row['actor']!=actor:raise Denied('Intent belongs to another actor')
            if row['state']!='uncertain':raise Conflict('Only uncertain intents need reconciliation')
            db.execute("UPDATE intent SET state='reconciled',result=?,finished=? WHERE id=?",(json.dumps(evidence),time.time(),rid))

    def capture(self,item,kind,data):
        if kind not in ('jobs','scholarships','research','tech','unclassified'):raise Conflict('Explicit supported task type required')
        if '_museTask' in data:raise Denied('Task receipts and state must use the revision-checked task API')
        with self.tx() as db:
            aliases=data.get('aliases',[])+([data['url']] if data.get('url') else [])
            if len(aliases)>30 or any(not isinstance(a,str) or len(a)>4096 for a in aliases):raise Conflict('Bounded source identity aliases required')
            known={row[0] for alias in aliases for row in db.execute('SELECT canonical FROM alias WHERE id=?',(alias,))}
            if len(known)>1:raise Conflict('Conflicting canonical identities; reconcile instead of guessing a duplicate')
            if known:item=next(iter(known))
            old=db.execute('SELECT kind,data FROM item WHERE id=?',(item,)).fetchone()
            if old:
                if old['kind']!='unclassified' and kind not in (old['kind'],'unclassified'):raise Conflict('Canonical item already has another task type; reconcile routing explicitly')
                previous=json.loads(old['data']);notes=previous.get('captureNotes',[])
                if data.get('note') and data['note'] not in notes:notes=(notes+[data['note']])[-40:]
                data={**previous,**{k:v for k,v in data.items() if v not in (None,'',[])},'captureNotes':notes}
            db.execute("INSERT INTO item(id,kind,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,kind=CASE WHEN item.kind='unclassified' THEN excluded.kind ELSE item.kind END,revision=item.revision+1",(item,kind,json.dumps(data)))
            for alias in aliases:db.execute('INSERT OR IGNORE INTO alias VALUES(?,?)',(alias,item))
            state=db.execute('SELECT state FROM item WHERE id=?',(item,)).fetchone()[0]
        return {'id':item,'state':state,'sync':'pending'}

    def claim_item(self,item,actor,revision=None):
        now=time.time()
        with self.tx() as db:
            row=db.execute('SELECT * FROM item WHERE id=?',(item,)).fetchone()
            if not row or row['state'] in ('submitted','uncertain'):raise Conflict('Item is unavailable or requires reconciliation')
            if '_museTask' in json.loads(row['data']) and row['state']!='queued':raise Conflict('Prepare or resolve this task before claiming it; completed tasks cannot be replayed')
            if '_museTask' in json.loads(row['data']) and (type(revision) is not int or row['revision']!=revision):raise Conflict('Task packet changed; claim with the revision you actually prepared/read')
            if row['owner'] and (row['until'] or 0)>now:raise Conflict('Application is already claimed')
            db.execute('UPDATE item SET owner=?,until=?,revision=revision+1 WHERE id=?',(actor,now+900,item))
            return {'id':item,'owner':actor,'until':now+900,'revision':row['revision']+1}

    def checkpoint(self,item,actor,data):
        required={'schema','canonicalItem','applicant','workspace','phase','artifacts','pendingIntent','nextSafeAction','sync'}
        if not isinstance(data,dict) or not required<=data.keys() or data['canonicalItem']!=item:raise Conflict('Incomplete checkpoint schema')
        with self.tx() as db:
            row=db.execute('SELECT * FROM item WHERE id=?',(item,)).fetchone()
            if not row or row['owner']!=actor or (row['until'] or 0)<time.time():raise Denied('Own the canonical item before checkpointing')
            data=dict(data,actor=actor)
            db.execute('INSERT INTO checkpoint VALUES(?,1,?,?) ON CONFLICT(item) DO UPDATE SET version=version+1,data=excluded.data,updated=excluded.updated',(item,json.dumps(data),time.time()))

    def next_work(self,actor):
        with self.lock:
            rows=self.db.execute("SELECT id,kind,state,data FROM item WHERE state='queued' AND (owner IS NULL OR until<?) ORDER BY rowid LIMIT 50",(time.time(),)).fetchall()
        return [dict(r,data=json.loads(r['data'])) for r in rows]

    def incident(self,key,kind,workspace,data,resolved=False):
        now=time.time()
        with self.tx() as db:db.execute('INSERT INTO incident VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last=excluded.last,state=excluded.state,data=excluded.data WHERE incident.state<>excluded.state OR incident.data<>excluded.data OR excluded.last-incident.last>=60',(key,kind,workspace,now,now,'resolved' if resolved else 'open',json.dumps(data)))

    def health(self):
        with self.lock:
            incidents=[dict(r,data=json.loads(r['data'])) for r in self.db.execute("SELECT * FROM incident WHERE state='open'")]
            uncertain=[dict(r) for r in self.db.execute("SELECT id,actor,workspace,kind,started FROM intent WHERE state='uncertain'")]
            pending=self.db.execute("SELECT count(*) FROM outbox WHERE kind='sheet' AND state!='synced'").fetchone()[0]
            sources=[dict(r) for r in self.db.execute('SELECT * FROM source')]
        return {'incidents':incidents,'uncertain':uncertain,'pendingSync':pending,'sources':sources,'notifications':'stored; delivery not asserted'}

    def source_attempt(self,source,state,cursor=None):
        now=time.time();success=state=='covered'
        with self.tx() as db:
            old=db.execute('SELECT * FROM source WHERE id=?',(source,)).fetchone()
            last_success=now if success else old['last_success'] if old else None
            blocked=None if success else (old['blocked_since'] if old and old['blocked_since'] else now)
            old_cursor=old['cursor'] if old else None
            db.execute('INSERT INTO source VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,last_success=excluded.last_success,blocked_since=excluded.blocked_since,cursor=excluded.cursor,state=excluded.state',(source,now,last_success,blocked,cursor or old_cursor,state))
