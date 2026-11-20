"""Bounded official board readers; discovery does not authorize submission."""
import hashlib,json,time,urllib.request,urllib.error,subprocess,sys
from urllib.parse import quote,urlsplit,urlunsplit,parse_qsl,urlencode

def canonical_url(url):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username:raise ValueError('Public HTTP(S) URL required')
    query=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('gclid','fbclid')]
    return urlunsplit((p.scheme,p.netloc.lower(),p.path,urlencode(query),''))

def identity(provider,employer,requisition):
    return 'job-'+hashlib.sha256((provider.lower()+'|'+employer.strip().casefold()+'|'+str(requisition)).encode()).hexdigest()[:32]

class Boards:
    def __init__(self,cache=None,interval=1):self.cache=cache if cache is not None else {};self.interval=interval;self.last={}
    def fetch(self,provider,board):
        if not board or len(board)>100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in board):raise ValueError('Exact board identifier required')
        key=provider+':'+board;now=time.monotonic()
        if key in self.cache and now-self.cache[key][0]<300:return self.cache[key][1]
        if now-self.last.get(provider,0)<self.interval:raise ValueError('Source rate limit; retry later')
        self.last[provider]=now
        urls={'greenhouse':f'https://boards-api.greenhouse.io/v1/boards/{quote(board)}/jobs','lever':f'https://api.lever.co/v0/postings/{quote(board)}?mode=json','ashby':f'https://api.ashbyhq.com/posting-api/job-board/{quote(board)}?includeCompensation=true'}
        if provider not in urls:raise ValueError('Unsupported official source')
        try:
            req=urllib.request.Request(urls[provider],headers={'User-Agent':'MuseMonitor/0.2 (personal job discovery)'})
            with urllib.request.urlopen(req,timeout=15) as response:
                raw=response.read(4*1024**2+1)
                if len(raw)>4*1024**2:raise ValueError('Source response exceeds bound')
            data=json.loads(raw);result={'source':key,'state':'covered','retrievedAt':time.time(),'jobs':self.normalize(provider,board,data)}
        except (OSError,ValueError,urllib.error.URLError):result={'source':key,'state':'unavailable','retrievedAt':time.time(),'jobs':[],'error':'Source could not be read; this is not zero results'}
        self.cache[key]=(now,result);return result

    @staticmethod
    def normalize(provider,board,data):
        rows=data.get('jobs',[]) if isinstance(data,dict) else data
        if not isinstance(rows,list):raise ValueError('Unexpected board schema')
        jobs=[]
        for row in rows[:5000]:
            rid=row.get('id') or row.get('jobUrl')
            title=row.get('title') or row.get('text')
            url=row.get('absolute_url') or row.get('hostedUrl') or row.get('jobUrl')
            if not rid or not title or not url:continue
            loc=row.get('location');loc=loc.get('name') if isinstance(loc,dict) else loc
            description=row.get('content') or row.get('descriptionPlain') or row.get('descriptionHtml')
            jobs.append({'canonicalId':identity(provider,board,rid),'provider':provider,'employer':board,'requisition':str(rid),'title':title,'url':canonical_url(url),'applyUrl':row.get('applyUrl') or row.get('applicationUrl') or url,'location':loc or row.get('categories',{}).get('location'),'workMode':row.get('workplaceType'),'compensation':row.get('compensation') or row.get('salaryRange'),'description':description,'sourceCompleteness':'description' if description else 'index; fetch official detail before readiness','sourceDate':row.get('updated_at') or row.get('publishedAt') or row.get('createdAt'),'eligibility':'unverified; apply configured direction/NC filters before readiness'})
        return jobs

    def detail(self,board,requisition):
        if not str(requisition).isdigit() or not board or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in board):raise ValueError('Exact Greenhouse board/requisition required')
        req=urllib.request.Request(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{requisition}',headers={'User-Agent':'MuseMonitor/0.2'})
        try:
            with urllib.request.urlopen(req,timeout=15) as response:raw=response.read(2*1024**2+1)
            if len(raw)>2*1024**2:raise ValueError('Detail exceeds bound')
            row=json.loads(raw);result=self.normalize('greenhouse',board,{'jobs':[row]})
            return {'state':'covered','jobs':result,'retrievedAt':time.time()}
        except (OSError,ValueError):return {'state':'unavailable','jobs':[],'error':'Official detail unavailable; not ready for execution'}

def jobspy_search(terms,location='North Carolina, USA',limit=20):
    """Optional pinned scraper; isolate its process in the CLI with a deadline."""
    from jobspy import scrape_jobs
    frame=scrape_jobs(site_name=['indeed','linkedin'],search_term=terms,location=location,results_wanted=min(limit,30),country_indeed='USA',verbose=0)
    return json.loads(frame.to_json(orient='records',date_format='iso'))

def bounded_jobspy(terms,location='North Carolina, USA',limit=20):
    if not isinstance(terms,str) or not 1<=len(terms)<=200 or len(location)>150:raise ValueError('Bounded search and location required')
    try:
        p=subprocess.run([sys.executable,'-m','muse_monitor.discovery'],input=json.dumps({'terms':terms,'location':location,'limit':limit}),capture_output=True,text=True,timeout=25)
        if p.returncode:return {'state':'unavailable','jobs':[],'error':'Scraper source failed; no successful coverage claimed'}
        result=json.loads(p.stdout)
        normalized=[]
        for row in result['jobs']:
            url=row.get('job_url_direct') or row.get('job_url')
            if not url:continue
            employer=row.get('company') or 'unverified-employer';rid=row.get('id') or canonical_url(url)
            normalized.append({'canonicalId':identity('jobspy-'+str(row.get('site')),employer,rid),'provider':row.get('site'),'employer':employer,'sourceId':str(rid),'requisition':None,'title':row.get('title'),'url':canonical_url(url),'aliases':[canonical_url(url)],'location':row.get('location'),'workMode':'remote' if row.get('is_remote') else 'unverified','compensation':{'min':row.get('min_amount'),'max':row.get('max_amount'),'unit':row.get('interval'),'currency':row.get('currency')},'description':row.get('description'),'sourceDate':row.get('date_posted'),'eligibility':'unverified; source listing is not proof of NC eligibility'})
        result['jobs']=normalized
        if any(s in p.stderr.lower() for s in ('error','403','429','blocked','captcha')):result['state']='partial' if result['jobs'] else 'unavailable'
        return result
    except (subprocess.TimeoutExpired,ValueError):return {'state':'unavailable','jobs':[],'error':'Scraper deadline or source response failed'}

if __name__=='__main__':
    data=json.loads(sys.stdin.read(4096));rows=jobspy_search(data['terms'],data['location'],data['limit'])
    print(json.dumps({'state':'covered','jobs':rows,'source':'jobspy','retrievedAt':time.time()}))
