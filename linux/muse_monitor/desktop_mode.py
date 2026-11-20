"""Fixed, reversible local handoff for native file pickers; no remote code."""
import json,re,subprocess
from pathlib import Path
from .legacy_actions import result_value,compact_result

def code(human,**extra):
    return Path(__file__).with_name('desktop-mode.js').read_text().replace('__MUSE_REQUEST__',json.dumps({'human':bool(human),**extra}))

def apply_mode(cli,human):
    result=cli('run-code',code(human))
    if result.returncode and result.returncode!=124 and re.search('file.?chooser',result.stdout,re.I):
        listing=cli('tab-list');selected=re.findall(r'(?m)^- (\d+): \(current\)',listing.stdout)
        if listing.returncode or len(selected)!=1:return result
        opened=cli('tab-new','about:blank')
        if opened.returncode:return opened
        result=cli('run-code',code(human,temporary=True,restoreIndex=int(selected[0])))
    if not result.returncode and result_value(result) is None:
        return subprocess.CompletedProcess(result.args,1,'Human/agent handoff was not confirmed. Workspace remains paused.\n')
    return compact_result(result)

def main():
    import argparse
    from .cli import request
    from .human_login import human
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['human','agent','choose']);parser.add_argument('--workspace',default='all');args=parser.parse_args()
    if args.mode=='choose':
        r=subprocess.run(['zenity','--list','--radiolist','--title=Muse control','--text=Choose who controls the workspace. Close any file window before handing back to Muse.','--column=Select','--column=Mode','TRUE','Use computer myself','FALSE','Let Muse work'],capture_output=True,text=True)
        if r.returncode:return
        choice=r.stdout.strip()
        if choice not in ('Use computer myself','Let Muse work'):return
        args.mode='human' if choice=='Use computer myself' else 'agent'
    config=Path.home()/'.config/muse-monitor/config.json';cfg=json.loads(config.read_text())
    workspaces=list(cfg['workspaces']) if args.workspace=='all' else [args.workspace]
    if any(w not in cfg['workspaces'] for w in workspaces):raise ValueError('Unknown workspace')
    if args.mode=='agent':
        if any((Path(cfg['workspaces'][w]['base'])/'manual-login.json').exists() for w in workspaces):raise ValueError('Finish human sign-in first')
        if request(config,{'op':'health'})['uncertain']:raise ValueError('Resolve uncertain work before resuming')
    for wid in workspaces:
        if args.mode=='human':human(config,wid,'pause')
        else:request(config,{'op':'local-resume','workspace':wid},principal='local-human')
    try:subprocess.run(['notify-send','Muse control','You control the browser; native file pickers are enabled.' if args.mode=='human' else 'Muse can claim the browser again.'],check=False)
    except OSError:pass

if __name__=='__main__':
    try:main()
    except Exception as error:
        subprocess.run(['zenity','--error','--text=Handoff could not complete. Your browser work is preserved; inspect Muse Monitor before retrying.'],check=False)
        raise SystemExit(1)
