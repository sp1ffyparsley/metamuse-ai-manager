"""Chrome native messaging: derive workspace from the real parent browser."""
import http.client,json,os,struct,sys,time
from pathlib import Path
from .cli import UnixHTTP

def workspace(config):
    expected='chrome-extension://'+config['extensionId']+'/'
    if len(sys.argv)<2 or sys.argv[1]!=expected:raise ValueError('Extension origin is not enrolled')
    pid=os.getppid()
    for _ in range(8):
        args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        for wid,spec in config['workspaces'].items():
            if ('--user-data-dir='+spec['profile']).encode() in args:return wid
        status=Path(f'/proc/{pid}/status').read_text()
        pid=int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
        if pid<=1:break
    raise ValueError('This browser profile is not enrolled')

def main():
    config=json.loads((Path.home()/'.config/muse-monitor/config.json').read_text());wid=workspace(config)
    from .diagnostics import Diagnostics
    trace=Diagnostics(Path(config['state'])/'diagnostics')
    token=Path(config['principalFiles']['extension-'+wid]).read_text().strip()
    while True:
        header=sys.stdin.buffer.read(4)
        if not header:return
        if len(header)!=4:raise ValueError('Incomplete native message')
        length=struct.unpack('<I',header)[0]
        if not 0<length<=65536:raise ValueError('Native message exceeds bound')
        raw=sys.stdin.buffer.read(length)
        if len(raw)!=length:raise ValueError('Incomplete native message')
        started=time.monotonic();command='native';outcome='ok'
        try:
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError('Object required')
            command=body.get('op','native')
            conn=UnixHTTP(str(Path(config['runtime'])/'muse-monitor.sock'))
            try:
                conn.request('POST','/v1/extension',json.dumps(body),{'Content-Type':'application/json','Authorization':'Bearer '+token,'X-Muse-Protocol':'1'})
                response=conn.getresponse();result=json.loads(response.read(1024**2))
                if response.status!=200:outcome='error';result={'error':'Controller denied this action or needs reconciliation','status':response.status}
            finally:conn.close()
        except (ValueError,OSError,http.client.HTTPException):outcome='error';result={'error':'Controller offline; existing work remains protected'}
        elapsed=(time.monotonic()-started)*1000
        if outcome!='ok' or elapsed>=1000:trace.emit('extension_native','extension',workspace=wid,command=command,elapsedMs=elapsed,outcome=outcome)
        answer=json.dumps(result).encode();sys.stdout.buffer.write(struct.pack('<I',len(answer))+answer);sys.stdout.buffer.flush()

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError):sys.exit(1)
