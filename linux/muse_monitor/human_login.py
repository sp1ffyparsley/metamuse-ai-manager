"""Attended human sign-in handoff; no credentials or authentication automation."""
import argparse,json,os,subprocess,time,uuid
from pathlib import Path
from .browser import Browser,BrowserError
from .cli import request,UnixHTTP
from .store import Conflict

def save_json(path,value):
    temporary=path.with_name(path.name+'.muse-new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:json.dump(value,stream)
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)

def owners(profile):
    found=[]
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if process.stat().st_uid!=os.getuid():continue
            args=process.joinpath('cmdline').read_bytes().split(b'\0')
            if args[0]!=b'/opt/google/chrome/chrome' or any(a.startswith(b'--type=') for a in args):continue
            if ('--user-data-dir='+profile).encode() in args:found.append({'pid':int(process.name),'automated':any(a.startswith(b'--remote-debugging') or a==b'--enable-automation' for a in args)})
        except (OSError,IndexError):continue
    return found

def human(config,wid,kind,grant=None):
    s=request(config,{'op':'status'});w=next(w for w in s['workspaces'] if w['id']==wid);p=w['pages'][0] if w['pages'] else {}
    body={'requestId':str(uuid.uuid4()),'generation':w['generation'],'page':p.get('id'),'pageGeneration':p.get('generation'),'grant':grant}
    data=json.loads(config.read_text());token=Path(data['principalFiles']['windows-human']).read_text().strip();c=UnixHTTP(str(Path(data['runtime'])/'muse-monitor.sock'))
    try:
        c.request('POST',f'/v1/workspaces/{wid}/{kind}',json.dumps(body),{'Content-Type':'application/json','Authorization':'Bearer '+token,'X-Muse-Protocol':'1'})
        r=c.getresponse();result=json.loads(r.read(2*1024**2));
        if r.status!=200:raise Conflict('Human handoff was denied; inspect ownership before continuing')
        return result
    finally:c.close()

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('workspace',choices=['jobs','scholarships','research']);parser.add_argument('action',choices=['begin','show','status','finish'],default='show',nargs='?');args=parser.parse_args()
    config=Path.home()/'.config/muse-monitor/config.json';data=json.loads(config.read_text());wid=args.workspace;spec=data['workspaces'][wid];b=Browser(data);profile=spec['profile'];marker=Path(spec['base'])/'manual-login.json'
    if args.action=='show':
        current=owners(profile)
        if current:
            found=subprocess.run(['xdotool','search','--onlyvisible','--all','--pid',str(current[0]['pid']),'.*'],capture_output=True,text=True,timeout=5)
            windows=found.stdout.split()
            if not windows:raise Conflict('Browser is starting; try the desktop shortcut again shortly')
            subprocess.run(['xdotool','windowactivate','--sync',windows[0]],check=True,timeout=5)
            print('Muse '+spec['name']+' is open.'+(' Pause in Muse Monitor before typing while automation is connected.' if current[0]['automated'] else ' Human mode; automation remains paused.'))
            return
        args.action='begin'
    if args.action=='status':print(json.dumps({'workspace':wid,'humanSignIn':marker.exists(),'profile':profile,'browserOwners':owners(profile),'authentication':'human verification required; no cookie inspection'}));return
    if args.action=='begin':
        if wid=='jobs' and not marker.exists() and not spec.get('allowHumanHandoff'):raise Conflict('Main Jobs is preserved. Finish/checkpoint its existing work with maintenance before switching its browser launch mode.')
        if not marker.exists():
            # A new browser resource must be idle and disposable before close.
            s=request(config,{'op':'status'});w=next(w for w in s['workspaces'] if w['id']==wid)
            if w.get('actor') or any(p.get('url') not in ('about:blank','chrome://new-tab-page/') for p in w['pages']):raise Conflict('Save/finish existing browser work before starting a human login handoff')
            human(config,wid,'pause')
            with b.exclusive(wid):
                marker.write_text(json.dumps({'workspace':wid,'mode':'human-sign-in','started':time.time()}));marker.chmod(0o600)
                if any(p['automated'] for p in owners(profile)):
                    b.cli(wid,'close',maintenance=True)
        with b.exclusive(wid):
            if any(p['automated'] for p in owners(profile)):raise Conflict('Automation browser is still closing; no second owner was launched')
            if owners(profile):print('Ordinary '+wid+' Chrome is already open; use that existing window.');return
            # Edit only a CLOSED profile. Atomic replacement retains all other
            # settings and never reads or copies authentication cookies.
            preferences=Path(profile)/'Default/Preferences';prefs=json.loads(preferences.read_text());prefs.setdefault('profile',{})['name']='Muse '+spec['name'];save_json(preferences,prefs)
            state=Path(profile)/'Local State';local=json.loads(state.read_text());local.setdefault('profile',{}).setdefault('info_cache',{}).setdefault('Default',{})['name']='Muse '+spec['name'];save_json(state,local)
            command=['/opt/google/chrome/chrome','--user-data-dir='+profile,'--profile-directory=Default','--class=Muse-'+spec['name'],'--new-window','https://accounts.google.com/']
            subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            until=time.monotonic()+10
            while time.monotonic()<until:
                current=owners(profile)
                if len(current)==1 and not current[0]['automated']:print('Use Muse '+spec['name']+' in the Muse/VMware desktop. Ordinary persistent Chrome is open; agents remain paused.');return
                time.sleep(.2)
        raise Conflict('Normal browser startup not verified; workspace stays paused')
    if not marker.exists():raise Conflict('No human sign-in handoff is active')
    if owners(profile):raise Conflict('Close only the Muse '+spec['name']+' normal Chrome window after saving work, then select Finish human sign-in again. It will not force-close your work.')
    with b.exclusive(wid):marker.unlink()
    b.start_additional(wid)
    until=time.monotonic()+20
    while time.monotonic()<until:
        status=request(config,{'op':'status'});w=next(w for w in status['workspaces'] if w['id']==wid)
        if w['pages']:break
        time.sleep(.3)
    else:raise Conflict('Automation reopened without fresh page observation; it remains paused')
    grant=human(config,wid,'takeover')['grant'];human(config,wid,'resume',grant['token'])
    print('Muse '+spec['name']+' automation reopened the SAME persistent profile and was explicitly resumed. Google can still require a later human challenge.')

if __name__=='__main__':
    try:main()
    except (Conflict,BrowserError,OSError,ValueError) as error:
        print(str(error));raise SystemExit(1)
