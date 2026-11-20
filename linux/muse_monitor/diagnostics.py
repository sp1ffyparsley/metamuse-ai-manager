"""Small private rotating metadata-only logs; logging cannot fail an action."""
import collections,fcntl,json,os,re,statistics,time
from pathlib import Path

NUMERIC={'elapsedMs','lockWaitMs','status','generation','memoryAvailableMiB','swapFreeMiB','load1','daemonConnectMs','guestRpcMs','browserOperationMs','navigationMs','guestAdapterMs','workerTotalMs','guestRoundTripMs'}
FIELDS={'event','component','workspace','actor','runId','command','outcome','errorType','transport'}|NUMERIC
WORDS=re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
class Diagnostics:
    def __init__(self,root,max_bytes=1024*1024,backups=2):
        self.root=Path(root);self.path=self.root/'events.jsonl';self.max_bytes=max_bytes;self.backups=backups
    def emit(self,event,component,**fields):
        try:
            row={'utc':round(time.time(),3)}
            if event.startswith('lease_') or fields.get('elapsedMs',0)>=1000 or fields.get('outcome') in ('error','uncertain'):
                try:
                    mem={p[0].rstrip(':'):int(p[1]) for line in Path('/proc/meminfo').read_text().splitlines() if len(p:=line.split())>=2 and p[0] in ('MemAvailable:','SwapFree:')}
                    fields=dict(fields,memoryAvailableMiB=mem.get('MemAvailable',0)/1024,swapFreeMiB=mem.get('SwapFree',0)/1024,load1=os.getloadavg()[0])
                except (OSError,ValueError):pass
            for key,value in dict(event=event,component=component,**fields).items():
                if key not in FIELDS:continue
                if isinstance(value,(int,float)) and not isinstance(value,bool):
                    if key in NUMERIC and 0<=value<=1e12:row[key]=round(value,3)
                elif isinstance(value,str) and WORDS.fullmatch(value):row[key]=value
            self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
            if self.root.is_symlink() or self.root.stat().st_uid!=os.getuid() or self.root.stat().st_mode&0o077:return
            fd=os.open(self.root/'events.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'a') as gate:
                try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return
                if self.path.exists() and self.path.stat().st_size>=self.max_bytes:
                    for n in range(self.backups,0,-1):
                        src=self.path if n==1 else self.path.with_suffix('.jsonl.'+str(n-1))
                        dest=self.path.with_suffix('.jsonl.'+str(n))
                        if src.exists():src.replace(dest)
                fd=os.open(self.path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'a') as stream:stream.write(json.dumps(row,separators=(',',':'))+'\n')
        except (OSError,ValueError,TypeError):pass
    def summary(self):
        counts=collections.Counter();times=[];errors=[];bytes_total=0;resources={};layers=collections.defaultdict(list)
        for path in reversed([self.path]+[self.path.with_suffix('.jsonl.'+str(n)) for n in range(1,self.backups+1)]):
            try:
                size=path.stat().st_size;bytes_total+=size
                if size>self.max_bytes+4096:continue
                for line in path.read_text().splitlines():
                    try:row=json.loads(line)
                    except ValueError:continue
                    counts[row.get('event','unknown')]+=1
                    if row.get('event')=='command' and isinstance(row.get('elapsedMs'),(int,float)):times.append(row['elapsedMs'])
                    for name in ('daemonConnectMs','guestRpcMs','browserOperationMs','navigationMs','guestAdapterMs','workerTotalMs','guestRoundTripMs'):
                        if isinstance(row.get(name),(int,float)):layers[name].append(row[name])
                    if 'memoryAvailableMiB' in row:resources={k:row[k] for k in ('utc','memoryAvailableMiB','swapFreeMiB','load1') if k in row}
                    if row.get('outcome') in ('error','uncertain'):
                        errors=(errors+[row])[-10:]
            except OSError:pass
        ordered=sorted(times)
        return {'schema':1,'events':dict(counts),'commandCount':len(times),'medianMs':round(statistics.median(times),2) if times else None,'p95Ms':ordered[min(len(ordered)-1,int(len(ordered)*.95))] if ordered else None,'recentErrors':errors,'lastResources':resources,'bytes':bytes_total,'maxBytes':self.max_bytes*(self.backups+1),'contentsLogged':False,
                'layers':{k:{'samples':len(v),'medianMs':round(statistics.median(v),2)} for k,v in layers.items()},
                'timingScope':'Guest RPC includes browser/site waits. Navigation measures browser navigation wait, not pure server latency. Full SSH/proxy/tool wall time requires a worker measurement; it is not inferred.'}
