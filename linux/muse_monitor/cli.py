"""Compact worker interface over a private Unix socket, no model runtime."""
import argparse,http.client,json,os,socket,sys,time
from pathlib import Path

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self,path):super().__init__('localhost',timeout=35);self.path=path
    def connect(self):self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect(self.path)

def request(config_path,body,principal='main',route='/v1/worker'):
    config=json.loads(Path(config_path).read_text())
    # The principal is selected by the installed forced handler, not by JSON.
    token=Path(config['principalFiles'][principal]).read_text().strip()
    conn=UnixHTTP(str(Path(config['runtime'])/'muse-monitor.sock'))
    started=time.monotonic()
    try:
        conn.request('GET' if body is None else 'POST',route,None if body is None else json.dumps(body),{'Content-Type':'application/json','Authorization':'Bearer '+token,'X-Muse-Protocol':'1'})
        response=conn.getresponse();raw=response.read(2*1024**2+1)
        if len(raw)>2*1024**2:raise ValueError('Response exceeds bound')
        value=json.loads(raw)
        if response.status!=200:raise ValueError(value.get('error','Controller request failed'))
        if body is not None and body.get('measure') is True and isinstance(value,dict):
            header=response.getheader('Server-Timing','')
            try:controller_ms=float(header.split('controller;dur=',1)[1].split(',',1)[0])
            except (ValueError,IndexError):controller_ms=None
            value['timing']={'guestRoundTripMs':round((time.monotonic()-started)*1000,3),'controllerMs':controller_ms,
                             'scope':'Guest Unix API only; excludes cloud proxy/SSH/tool/startup'}
        return value
    finally:conn.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',default=str(Path.home()/'.config/muse-monitor/config.json'))
    connections=parser.add_mutually_exclusive_group()
    connections.add_argument('--connections',action='store_true')
    connections.add_argument('--verify-connection',choices=('google','gmail'))
    parser.add_argument('json',nargs='?');args=parser.parse_args()
    if args.connections or args.verify_connection:
        body={'id':args.verify_connection} if args.verify_connection else None
        route='/v1/connections/verify' if body else '/v1/connections'
        print(json.dumps(request(args.config,body,'local-human',route),ensure_ascii=False,indent=2))
        return
    body=json.loads(args.json or sys.stdin.read(65537))
    if not isinstance(body,dict) or len(json.dumps(body))>65536:raise ValueError('Bounded JSON object required')
    print(json.dumps(request(args.config,body),ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,http.client.HTTPException):print('Controller unavailable or request denied; inspect status and reconcile before retrying.',file=sys.stderr);sys.exit(1)
