"""Fast client for the existing pinned Playwright daemon's private Unix RPC.

No new listener or process. Fall back only before dispatch; never replay after
a timeout/disconnect. Remote authority remains in the existing forced handler.
"""
import json,os,re,socket,stat,subprocess,time,uuid
from pathlib import Path

PINNED_VERSION='1.66.0-alpha-1794252756000'
LIMIT=8*1024**2
class DispatchUncertain(OSError):pass

def session(base,name):
    if os.environ.get('MUSE_DISABLE_DIRECT_CLI')=='1':return None
    root=Path(base)/'cache/ms-playwright/daemon'
    matches=[]
    for path in list(root.glob('*/'+name+'.session'))[:32]:
        try:
            s=path.stat()
            if path.is_symlink() or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_size>262144:continue
            data=json.loads(path.read_text())
            if data.get('name')!=name or data.get('version')!=PINNED_VERSION:continue
            endpoint=Path(data['socketPath']);sock=endpoint.stat()
            if endpoint.is_symlink() or not stat.S_ISSOCK(sock.st_mode) or sock.st_uid!=os.getuid() or sock.st_mode&0o077:continue
            matches.append(data)
        except (OSError,ValueError,KeyError,TypeError):continue
    return matches[0] if len(matches)==1 else None

def execute(base,name,cwd,command,seconds=25):
    data=session(base,name)
    if data is None:return None
    # Socket liveness alone does not prove that Chrome has open pages. Probe
    # the actual browser with a bounded read-only command, then discard titles.
    wire_command=('tab-list',) if command[0]=='list' else command
    args={'_':[]}
    for index,value in enumerate(wire_command):
        if index and value.startswith('--'):
            key,sep,v=value[2:].partition('=')
            if not sep:return None # an unknown CLI option must use the real client
            args[key]=v
        else:args['_'].append(value)
    deadline=time.monotonic()+seconds
    client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    connected=time.monotonic()
    try:
        client.settimeout(min(seconds,.5))
        try:client.connect(data['socketPath'])
        except OSError:return None # no request bytes were sent
        connect_ms=(time.monotonic()-connected)*1000
        rid=uuid.uuid4().hex
        payload={'id':rid,'method':'run','params':{'args':args,'cwd':str(cwd),'raw':False,'json':False}}
        encoded=(json.dumps(payload,ensure_ascii=True)+'\n').encode()
        if len(encoded)>2*1024**2:raise ValueError('Daemon request exceeds bound')
        try:
            client.settimeout(max(.001,deadline-time.monotonic()));client.sendall(encoded)
            result=bytearray()
            while b'\n' not in result:
                if time.monotonic()>=deadline:raise DispatchUncertain('Daemon deadline exceeded; inspect before retrying')
                client.settimeout(max(.001,deadline-time.monotonic()))
                part=client.recv(min(65536,LIMIT+1-len(result)))
                if not part:raise DispatchUncertain('Daemon disconnected after dispatch; inspect before retrying')
                result.extend(part)
                if len(result)>LIMIT:raise DispatchUncertain('Daemon response exceeded bound; inspect before retrying')
            reply=json.loads(result.split(b'\n',1)[0])
            if reply.get('id')!=rid:raise DispatchUncertain('Daemon response identity changed; inspect before retrying')
            if 'error' in reply:return subprocess.CompletedProcess(command,1,str(reply['error'])+'\n')
            value=reply.get('result')
            if not isinstance(value,dict) or not isinstance(value.get('text'),str):raise DispatchUncertain('Invalid daemon response after dispatch; inspect before retrying')
            result=subprocess.CompletedProcess(command,1 if value.get('isError') else 0,value['text'])
            result.muse_timing={'daemonConnectMs':connect_ms,'guestRpcMs':(time.monotonic()-connected)*1000}
            if command[0]=='list':
                opened=not value.get('isError') and bool(re.search(r'(?m)^- \d+:',value['text']))
                from .legacy_actions import native_inventory
                inventory=native_inventory(result)
                if inventory:result.muse_current_url=next(r['url'] for r in inventory['tabs'] if r['current'])
                result.returncode=0;result.stdout='- '+name+':\n  - status: '+('open' if opened else 'closed')+'\n'
            return result
        except (OSError,ValueError,KeyError,TypeError) as exc:
            if isinstance(exc,DispatchUncertain):raise
            raise DispatchUncertain('Daemon outcome uncertain; inspect before retrying') from None
    finally:client.close()
