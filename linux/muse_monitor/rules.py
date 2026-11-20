"""Shared, versioned grouping preferences; rules never confer tab authority."""
import json,re
from .store import Conflict,Denied

def validate(value):
    if not isinstance(value,list) or len(value)>100:raise Conflict('Use at most 100 grouping rules')
    seen=set();colors={'grey','blue','red','yellow','green','pink','purple','cyan','orange'}
    for rule in value:
        if not isinstance(rule,dict):raise Conflict('Rule object required')
        rid=rule.get('id');domains=rule.get('domains');path=rule.get('path')
        if not isinstance(rid,str) or not re.fullmatch('[a-z0-9_-]{1,64}',rid) or rid in seen:raise Conflict('Unique rule ID required')
        if not isinstance(rule.get('title'),str) or not rule['title'].strip() or len(rule['title'])>100 or rule.get('color') not in colors:raise Conflict('Invalid rule title/color')
        if type(rule.get('priority')) is not int or not isinstance(domains,list) or not 1<=len(domains)<=100:raise Conflict('Invalid rule priority/domains')
        if any(not isinstance(d,str) or len(d)>253 or not re.fullmatch('[a-z0-9]+(?:[.-][a-z0-9]+)*',d) for d in domains):raise Conflict('Use exact host domains')
        if path is not None and (not isinstance(path,str) or not path.startswith('/') or len(path)>1024):raise Conflict('Invalid rule path')
        seen.add(rid)
    return value

def shared(monitor,actor,wid,body):
    monitor.scope(actor,wid)
    key='grouping-rules:'+wid
    with monitor.store.tx() as db:
        row=db.execute('SELECT data FROM setting WHERE id=?',(key,)).fetchone()
        current=json.loads(row['data']) if row else {'revision':0,'rules':[]}
        if 'rules' not in body:return current
        if actor['role'] not in ('human','main'):raise Denied('Only the human or Main may change grouping rules')
        rules=validate(body['rules'])
        if type(body.get('revision')) is not int or body['revision']!=current['revision']:raise Conflict('Grouping preferences changed; reread before editing')
        result={'revision':current['revision']+1,'rules':rules}
        db.execute('INSERT INTO setting(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(key,json.dumps(result)))
        return result
