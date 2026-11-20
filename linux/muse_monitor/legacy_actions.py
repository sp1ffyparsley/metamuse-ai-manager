"""Fixed internal browser operations; never executes remotely supplied code."""
import json,re,subprocess
from pathlib import Path
from urllib.parse import urlsplit


def script(request):
    allowed = {'tabs', 'modal-tabs', 'select', 'focused-input', 'read-value'}
    if request.get('kind') not in allowed:
        raise ValueError('Internal browser operation denied.')
    return Path(__file__).with_name('legacy-actions.js').read_text().replace('__MUSE_REQUEST__',json.dumps(request,ensure_ascii=True))


def result_value(result):
    if result.returncode:
        return None
    try:
        raw=result.stdout.split('### Result\n',1)[1].strip()
        value,_=json.JSONDecoder().raw_decode(raw)
        return value if isinstance(value,dict) else None
    except (ValueError,IndexError):return None


def native_inventory(result):
    """Parse the pinned CLI's full, modal-safe tab result, never its diff header.

    Ambiguous/multiline markdown falls back to the existing structured callback.
    Native listing does not inspect chooser state; unknown must stay unknown.
    """
    if result.returncode or '### Result\n' not in result.stdout:return None
    body=result.stdout.split('### Result\n',1)[1].split('\n### ',1)[0].strip()
    lines=body.splitlines()
    if not 1<=len(lines)<=500:return None
    rows=[]
    for index,line in enumerate(lines):
        if line.count('](')!=1:return None
        m=re.fullmatch(r'- (\d+):( \(current\))? \[(.*)\]\((.*)\)',line)
        if not m or int(m[1])!=index:return None
        rows.append({'index':index,'current':bool(m[2]),'title':m[3][:300],
                     'url':m[4],'chooserOpen':None})
    selected=[row['index'] for row in rows if row['current']]
    if len(selected)!=1:return None
    return {'schema':1,'scope':'all-open-tabs-in-assigned-browser',
            'selectedIndex':selected[0],'tabs':rows,'chooserState':'not-inspected'}


def inventory(cli,index=None):
    if index is not None:
        # The native tab operation is modal-safe; execute it exactly once.
        selected=cli('tab-select',str(index))
        if selected.returncode:return selected
    # The native result is already a complete list. Unlike run-code it has no
    # 500 ms action-settle delay and stays available with a chooser open.
    result=cli('tab-list')
    value=native_inventory(result)
    if value is None and not result.returncode:
        result=cli('run-code',script({'kind':'tabs'}))
        value=result_value(result)
    if value is None and result.returncode and result.returncode!=124 and ('file chooser' in result.stdout.lower() or 'filechooser' in result.stdout.lower()):
        # run-code is modal-blocked, but native listing/new-tab are allowed.
        # Inspect through one own blank tab and restore the exact selected index;
        # the fixed callback never clears the original chooser and closes only
        # its own temporary page, including on failure.
        listing=cli('tab-list')
        selected_rows=re.findall(r'(?m)^- (\d+): \(current\)',listing.stdout)
        if listing.returncode==0 and len(selected_rows)==1:
            opened=cli('tab-new','about:blank')
            if opened.returncode==0:
                result=cli('run-code',script({'kind':'modal-tabs','restoreIndex':int(selected_rows[0])}))
                value=result_value(result)
    if value is None:
        if index is not None and result.returncode!=124:
            return subprocess.CompletedProcess(result.args,0,'### Result\n'+json.dumps({'selectedIndex':index,'selected':True,'inventoryAvailable':False,'next':'Inspect snapshot or resolve open chooser; do not repeat the selection blindly.'})+'\n')
        return result if result.returncode else subprocess.CompletedProcess(result.args,1,'MUSE_ERROR [TAB_INVENTORY_UNAVAILABLE]: No complete tab inventory returned; inspect before retrying.\n')
    if (not isinstance(value.get('tabs'),list) or len(value['tabs'])>500 or
        any(not isinstance(row,dict) or type(row.get('index')) is not int or type(row.get('current')) is not bool or not isinstance(row.get('title'),str) or not isinstance(row.get('url'),str) for row in value['tabs'])):
        return subprocess.CompletedProcess(result.args,1,'MUSE_ERROR [TAB_INVENTORY_INVALID]: Refresh the assigned workspace.\n')
    # Full inventory every time, independent of incremental CLI header output.
    lines=['### Result',json.dumps(value,ensure_ascii=True),'### Open tabs']
    for row in value['tabs']:
        title=re.sub(r'[\r\n]+',' ',row['title']).replace('[','\\[').replace(']','\\]')
        lines.append('- '+str(row['index'])+': '+('(current) ' if row['current'] else '')+'['+title+']('+row['url']+')'+(' [file chooser open]' if row.get('chooserOpen') else ''))
    return subprocess.CompletedProcess(result.args,0,'\n'.join(lines)+'\n')


def dialog_summary(text):
    """Surface visible dialog controls already present in the native snapshot.

    This is formatting only: no DOM calls, new actions, refs, or inferred steps.
    """
    if '### Snapshot\n' not in text:return ''
    body=text.split('### Snapshot\n',1)[1].split('\n### ',1)[0]
    lines=[];dialog_indent=None;scalar_indent=None;truncated=False
    for line in body.splitlines():
        indent=len(line)-len(line.lstrip(' '))
        if scalar_indent is not None:
            if not line.strip() or indent>scalar_indent:continue
            scalar_indent=None
        if re.search(r':\s*[|>][-+0-9]*\s*$',line):
            scalar_indent=indent;continue
        if dialog_indent is not None and re.match(r'\s*- ',line) and indent<=dialog_indent:
            dialog_indent=None
        if (re.match(r'''\s*- ['"]?(?:dialog|alertdialog)\b''',line) and
            re.search(r'\[ref=(?:f\d+)?e\d+\]',line)):
            dialog_indent=indent;include=True
        else:
            include=dialog_indent is not None and bool(re.match(r'''\s*- ['"]?(?:heading|button|link|combobox|checkbox|radio|alert|status)\b''',line))
        if include:
            if len(lines)>=30 or sum(map(len,lines))+len(line)>4096:
                truncated=True;break
            lines.append(line.strip())
    if not lines:return ''
    return '### Page dialogs\n'+'\n'.join(lines)+('\n(summary truncated; full snapshot follows)' if truncated else '')+'\n'


def decorate_snapshot(result,tabs):
    if result.returncode:return result
    summary=dialog_summary(result.stdout)
    text=result.stdout.replace('### Snapshot\n',summary+'### Snapshot\n',1) if summary else result.stdout
    if tabs.returncode or '### Open tabs\n' not in tabs.stdout:
        return subprocess.CompletedProcess(result.args,0,text.rstrip()+'\nMUSE_WARNING [TAB_INVENTORY_UNAVAILABLE]: Snapshot returned; refresh tab-list before selecting another tab.\n')
    text=re.sub(r'(?ms)^### Open tabs\n.*?(?=^### |\Z)','',text)
    return subprocess.CompletedProcess(result.args,0,text.rstrip()+'\n### Open tabs\n'+tabs.stdout.split('### Open tabs\n',1)[1])


def focus_input(cli,operation,ref,text):
    return compact_result(cli('run-code',script({'kind':'focused-input','operation':operation,'ref':ref,'text':text})))


def read_value(cli,ref):
    return compact_result(cli('run-code',script({'kind':'read-value','ref':ref})))


def guard_site(command, values, selected_url, cli, check):
    """Use already-read tab identity; recovery/read operations remain available."""
    if command in {'tab-list','tab-select','snapshot','screenshot','find','read-value','dialog-dismiss'}:
        return
    if (command=='upload' and not values) or (command=='press' and values==['Escape']):
        return
    if command in {'goto','tab-new'}:
        if command=='goto':
            # An independent target must not replace an unresolved form. Open
            # a separate tab instead; retain the original page for the human.
            current=selected_url
            if current is None:
                listing=result_value(inventory(cli))
                if not listing:raise ValueError('Current form was not established; inspect tab-list before navigating.')
                current=next((t['url'] for t in listing['tabs'] if t['current']),None)
            if current and current.startswith(('http://','https://')):
                check(urlsplit(current)._replace(path='',query='',fragment='').geturl())
        target=values[0] if values else None
    else:
        target=selected_url
        if target is None:
            listing=result_value(inventory(cli))
            if not listing:raise ValueError('Inspect tab-list before writing; current site was not established.')
            target=next((t['url'] for t in listing['tabs'] if t['current']),None)
            if target is None:raise ValueError('Current site was not established; no action dispatched.')
    if target and target.startswith(('http://','https://')):
        # Routing needs only the origin; never forward a form/auth query or
        # fragment into the metadata gate.
        check(urlsplit(target)._replace(path='',query='',fragment='').geturl())


def compact_result(result):
    # Keep the bounded result; omit the internal implementation echoed by the
    # native code runner. Errors/deadlines retain their exact failure status.
    value=result_value(result)
    if value is None:return result
    return subprocess.CompletedProcess(result.args,0,'### Result\n'+json.dumps(value,ensure_ascii=True)+'\n')


def describe_error(command,result):
    if not result.returncode:return result
    if result.returncode==124:
        detail='Outcome uncertain after deadline/disconnect. Inspect current page or receipt before retrying.'
        code='OUTCOME_UNCERTAIN'
    elif command=='upload':
        detail='Attachment not confirmed. Inspect the current page first. If no chooser was opened, click the intended upload control once, then upload NAME.pdf. Use upload without a filename to cancel.'
        code='UPLOAD_NOT_CONFIRMED'
    else:
        detail='Action failed; inspect the current page before retrying.'
        code='BROWSER_ACTION_FAILED'
    return subprocess.CompletedProcess(result.args,result.returncode,'MUSE_ERROR ['+code+']: '+detail+'\n'+result.stdout)
