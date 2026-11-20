"""Single OAuth data plane with stable-row writes and an uncertainty outbox."""
import argparse,json,math,os,re,tempfile,threading,time
from pathlib import Path
from .store import Conflict,Denied

SCOPES=['https://www.googleapis.com/auth/drive','https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/documents']
MAIL_SCOPES=['https://www.googleapis.com/auth/gmail.readonly']
class GoogleUnavailable(Exception):pass

class Google:
    def __init__(self,token,scopes=None):
        self.token=Path(token);self.session=None;self.scopes=SCOPES if scopes is None else scopes
        self._connect_lock=threading.RLock()
    def connect(self, force_refresh=False):
        if not self.token.is_file():raise GoogleUnavailable('Google Desktop OAuth consent is required')
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession,Request
        from google.auth.exceptions import RefreshError
        with self._connect_lock:
            try:
                creds=Credentials.from_authorized_user_file(self.token,self.scopes)
                if force_refresh or not creds.valid:creds.refresh(Request())
                if creds.granted_scopes and not set(self.scopes)<=set(creds.granted_scopes):
                    raise GoogleUnavailable('Google consent is missing one or more required scopes')
                with tempfile.NamedTemporaryFile(mode='w',dir=self.token.parent,prefix='.oauth-',delete=False) as stream:
                    temp=Path(stream.name)
                    try:
                        stream.write(creds.to_json());stream.flush()
                        os.replace(temp,self.token)
                    finally:temp.unlink(missing_ok=True)
                self.session=AuthorizedSession(creds)
            except (ValueError,OSError,RefreshError) as error:
                if isinstance(error,RefreshError) and getattr(error,'retryable',False):
                    raise GoogleUnavailable('Google refresh is temporarily unavailable') from error
                raise GoogleUnavailable('Google authorization expired or was revoked; reauthorize this client') from error
    def call(self,service,path,method='GET',body=None,raw=False):
        if service=='gmail' and (not set(MAIL_SCOPES)<=set(self.scopes) or method!='GET'):raise Denied('Mailbox reads require separate account consent; Drive sharing does not share Gmail')
        if self.session is None:self.connect()
        host={'sheets':'sheets.googleapis.com/v4','drive':'www.googleapis.com/drive/v3','docs':'docs.googleapis.com/v1','gmail':'gmail.googleapis.com/gmail/v1'}[service]
        response=self.session.request(method,'https://'+host+'/'+path,json=body,timeout=15,allow_redirects=False)
        if response.status_code in (401,403):raise GoogleUnavailable('Google consent/scope/file permission is missing')
        if not 200<=response.status_code<300:raise GoogleUnavailable('Google request failed; reconcile mutation outcome before retrying')
        if len(response.content)>8*1024**2:raise GoogleUnavailable('Google response exceeds bound')
        return response.content if raw else response.json()

    def upload_text(self, file_id, folder, title, content):
        """Use a pre-generated Drive ID so an uncertain create can be reconciled."""
        import uuid
        if not isinstance(content, bytes) or not 1 <= len(content) <= 65536:
            raise ValueError('Drive text upload exceeds the bounded payload')
        if self.session is None:self.connect()
        marker='muse-'+uuid.uuid4().hex
        metadata=json.dumps({'id':file_id,'parents':[folder],'name':title,'mimeType':'text/plain'}).encode()
        body=(f'--{marker}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()+metadata+
              f'\r\n--{marker}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n'.encode()+content+
              f'\r\n--{marker}--\r\n'.encode())
        response=self.session.post('https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id',
                                   headers={'Content-Type':f'multipart/related; boundary={marker}'},data=body,
                                   timeout=15,allow_redirects=False)
        if not 200<=response.status_code<300 or response.json().get('id')!=file_id:
            raise GoogleUnavailable('Drive upload was not acknowledged; reconcile by the generated file ID')

class Sheets:
    def __init__(self,api,schema):self.api=api;self.schema=schema
    @staticmethod
    def column(number):
        result=''
        while number:number,rem=divmod(number-1,26);result=chr(65+rem)+result
        return result
    def grid(self,spec):
        result=self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+'?fields=sheets(properties(sheetId,title,gridProperties))')
        matches=[s['properties'] for s in result.get('sheets',[]) if s['properties']['sheetId']==spec['sheetId'] and s['properties']['title']==spec['sheet']]
        if len(matches)!=1:raise Conflict('Mapped canonical sheet identity changed')
        return matches[0]['gridProperties']
    def values(self,spec,range):
        from urllib.parse import quote
        name="'"+spec['sheet'].replace("'","''")+"'!"+range
        return self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+'/values/'+quote(name,safe='')+'?valueRenderOption=UNFORMATTED_VALUE').get('values',[])
    def headers(self,spec):
        grid=self.grid(spec);last=self.column(grid['columnCount'])
        row=self.values(spec,f"A{spec['headerRow']}:{last}{spec['headerRow']}")
        headers=row[0] if row else []
        if len(headers)!=len(set(headers)) or headers[:len(spec['headers'])]!=spec['headers']:raise Conflict('Live headers changed; preserve draft and review mapping')
        return headers
    def metadata(self,spec,key,value):
        result=self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+'/developerMetadata:search','POST',{'dataFilters':[{'developerMetadataLookup':{'metadataKey':key,'metadataValue':value,'visibility':'DOCUMENT'}}]})
        matches=[m['developerMetadata'] for m in result.get('matchedDeveloperMetadata',[]) if m['developerMetadata'].get('location',{}).get('dimensionRange',{}).get('sheetId')==spec['sheetId']]
        if len(matches)>1:raise Conflict('Canonical row metadata is ambiguous')
        return matches[0] if matches else None
    def row(self,spec,meta):
        result=self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+'/values:batchGetByDataFilter','POST',{'dataFilters':[{'developerMetadataLookup':{'metadataId':meta['metadataId']}}],'valueRenderOption':'UNFORMATTED_VALUE'})
        ranges=result.get('valueRanges',[])
        if len(ranges)!=1:raise Conflict('Stable row missing or ambiguous')
        rows=ranges[0]['valueRange'].get('values',[])
        if len(rows)!=1:raise Conflict('Metadata must identify exactly one canonical row')
        return rows[0]
    def update(self,name,canonical,changes):
        spec=self.schema[name];headers=self.headers(spec)
        if not changes or not set(changes)<=set(spec['automationColumns']):raise Denied('Write outside automation-owned fields')
        meta=self.metadata(spec,'muse.canonical',canonical)
        if not meta:raise Conflict('Canonical row is not enrolled; bind and verify it before writes')
        row=self.row(spec,meta);key=headers.index(spec['keyHeader'])
        if key>=len(row) or str(row[key])!=canonical:raise Conflict('Canonical identity changed; domain writes stopped')
        # null values are skipped by the Sheets API. Human-owned cells and
        # formulas never enter the write payload, even after sorting/insertion.
        values=[None]*len(headers)
        for name,value in changes.items():
            if isinstance(value,(dict,list)) or (isinstance(value,str) and value.startswith('=')):raise Denied('Scalar RAW values only; formulas are not accepted')
            values[headers.index(name)]=value
        result=self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+'/values:batchUpdateByDataFilter','POST',{'valueInputOption':'RAW','includeValuesInResponse':True,'data':[{'dataFilter':{'developerMetadataLookup':{'metadataId':meta['metadataId']}},'majorDimension':'ROWS','values':[values]}]})
        after=self.row(spec,meta)
        if key>=len(after) or str(after[key])!=canonical or any(headers.index(k)>=len(after) or after[headers.index(k)]!=v for k,v in changes.items()):raise Conflict('Write readback differs; reconcile before another domain write')
        return {'verified':True,'metadataId':meta['metadataId']}
    def bind(self,name,canonical):
        """Explicit migration only. Never trust a duplicate display ID."""
        spec=self.schema[name];headers=self.headers(spec)
        if self.metadata(spec,'muse.canonical',canonical):return {'alreadyBound':True}
        col=headers.index(spec['keyHeader']);letter=self.column(col+1)
        rows=self.values(spec,f"{letter}{spec['headerRow']+1}:{letter}{self.grid(spec)['rowCount']}")
        matches=[i+spec['headerRow'] for i,row in enumerate(rows) if row and str(row[0])==canonical]
        if len(matches)!=1:raise Conflict('Canonical key must occur exactly once before metadata binding')
        index=matches[0]
        self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+':batchUpdate','POST',{'requests':[{'createDeveloperMetadata':{'developerMetadata':{'metadataKey':'muse.canonical','metadataValue':canonical,'visibility':'DOCUMENT','location':{'dimensionRange':{'sheetId':spec['sheetId'],'dimension':'ROWS','startIndex':index,'endIndex':index+1}}}}}]})
        meta=self.metadata(spec,'muse.canonical',canonical)
        if not meta or str(self.row(spec,meta)[col])!=canonical:raise Conflict('Binding changed concurrently; no domain values were written')
        return {'bound':True,'metadataId':meta['metadataId']}
    def insert(self,name,canonical,values,operation):
        spec=self.schema[name];headers=self.headers(spec)
        found=self.metadata(spec,'muse.operation',operation)
        if found:return {'reconciled':True,'metadataId':found['metadataId']}
        if self.metadata(spec,'muse.canonical',canonical):raise Conflict('Canonical item already exists; update through its stable metadata')
        if spec['keyHeader'] not in values or values[spec['keyHeader']]!=canonical or not set(values)<=set(headers):raise Conflict('New canonical row has invalid identity or fields')
        if not set(values)<=set(spec['automationColumns'])|{spec['keyHeader']}:raise Denied('New rows may populate only the canonical key and automation-owned fields')
        key=headers.index(spec['keyHeader'])
        letter=self.column(key+1);rows=self.values(spec,f"{letter}{spec['headerRow']+1}:{letter}{self.grid(spec)['rowCount']}")
        if any(row and str(row[0])==canonical for row in rows):raise Conflict('Existing unbound canonical row requires migration, not another append')
        index=spec['headerRow']+len(rows)
        dimension={'sheetId':spec['sheetId'],'dimension':'ROWS','startIndex':index,'endIndex':index+1}
        cells=[]
        for header in headers:
            value=values.get(header)
            if value is None:cells.append({});continue
            if isinstance(value,(list,dict)) or (isinstance(value,str) and value.startswith('=')):raise Denied('New row requires scalar non-formula values')
            if isinstance(value,(int,float)) and not math.isfinite(value):raise Denied('Finite scalar values required')
            cells.append({'userEnteredValue':{'boolValue':value} if isinstance(value,bool) else {'numberValue':value} if isinstance(value,(int,float)) else {'stringValue':str(value)}})
        # Insert and write our new row plus both metadata identities in one
        # atomic batch. No existing human cell is overwritten by guessed row.
        requests=[{'insertDimension':{'range':dimension,'inheritFromBefore':True}},{'updateCells':{'start':{'sheetId':spec['sheetId'],'rowIndex':index,'columnIndex':0},'rows':[{'values':cells}],'fields':'userEnteredValue'}}]
        for key,value in [('muse.canonical',canonical),('muse.operation',operation)]:requests.append({'createDeveloperMetadata':{'developerMetadata':{'metadataKey':key,'metadataValue':value,'visibility':'DOCUMENT','location':{'dimensionRange':dimension}}}})
        self.api.call('sheets','spreadsheets/'+spec['spreadsheet']+':batchUpdate','POST',{'requests':requests})
        found=self.metadata(spec,'muse.operation',operation)
        if not found:raise Conflict('Append outcome not verified; no automatic replay')
        return {'inserted':True,'metadataId':found['metadataId']}

class Outbox:
    def __init__(self,store,sheets):self.store=store;self.sheets=sheets
    def enqueue(self,operation,name,canonical,changes,insert=False):
        from .server import private_data
        private_data(changes)
        if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',operation):raise Conflict('Stable bounded synchronization operation ID required')
        data={'name':name,'canonical':canonical,'values':changes,'insert':insert}
        with self.store.tx() as db:db.execute('INSERT OR IGNORE INTO outbox(id,kind,data) VALUES(?,?,?)',(operation,'sheet',json.dumps(data)))
    def step(self):
        with self.store.lock:rows=self.store.db.execute("SELECT * FROM outbox WHERE kind='sheet' AND state IN ('pending','uncertain') ORDER BY rowid LIMIT 10").fetchall()
        for row in rows:
            data=json.loads(row['data']);spec=self.sheets.schema[data['name']]
            try:
                if row['state']=='uncertain':
                    # A lost insert response can be reconciled by its operation
                    # metadata. Absence does not authorize a replay.
                    if not data['insert'] or not self.sheets.metadata(spec,'muse.operation',row['id']):continue
                    result={'reconciled':True}
                else:
                    with self.store.tx() as db:db.execute("UPDATE outbox SET state='uncertain',attempts=attempts+1 WHERE id=?",(row['id'],))
                    result=self.sheets.insert(data['name'],data['canonical'],data['values'],row['id']) if data['insert'] else self.sheets.update(data['name'],data['canonical'],data['values'])
                with self.store.tx() as db:db.execute("UPDATE outbox SET state='synced',result=? WHERE id=?",(json.dumps(result),row['id']))
            except GoogleUnavailable:
                # Unknown network result stays uncertain. Consent errors before
                # a mutation still require explicit reauthorization, not loops.
                self.store.incident('google-access','sync',None,{'reason':'Google authorization, access or response unavailable'})
                return

def authorize(client,token,mailbox=False):
    from google_auth_oauthlib.flow import InstalledAppFlow
    file=Path(client)
    if not file.is_file():raise GoogleUnavailable('Provide your Google Desktop OAuth client JSON file first')
    flow=InstalledAppFlow.from_client_secrets_file(file,MAIL_SCOPES if mailbox else SCOPES,autogenerate_code_verifier=True)
    credentials=flow.run_local_server(host='127.0.0.1',port=0,open_browser=True,timeout_seconds=300,authorization_prompt_message='Authorize Muse Monitor in the browser opened for you.',success_message='Muse Monitor authorization completed. You may close this tab.')
    dest=Path(token);dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Never create the refresh credential under the desktop's default umask.
    temporary=dest.with_name(dest.name+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:stream.write(credentials.to_json())
        temporary.replace(dest)
    finally:temporary.unlink(missing_ok=True)

def copy_merge_export(api,template,folder,title,replacements,output):
    """Preserve the existing native Google Docs template/rendering path."""
    from urllib.parse import quote
    if not template or not replacements:raise Conflict('Approved existing template and real replacement content required')
    copied=api.call('drive','files/'+template+'/copy?fields=id','POST',{'name':title,'parents':[folder]})
    doc=copied['id']
    api.call('docs','documents/'+doc+':batchUpdate','POST',{'requests':[{'replaceAllText':{'containsText':{'text':k,'matchCase':True},'replaceText':v}} for k,v in replacements.items()]})
    rendered=api.call('docs','documents/'+doc+'?includeTabsContent=true')
    if re.search(r'\{\{[^}]+\}\}',json.dumps(rendered)):raise Conflict('Unreplaced template field remains; do not upload this document')
    pdf=api.call('drive','files/'+doc+'/export?mimeType=application%2Fpdf',raw=True)
    if not pdf.startswith(b'%PDF-'):raise Conflict('Google export is not a PDF')
    Path(output).write_bytes(pdf);Path(output).chmod(0o600)
    return {'document':doc,'bytes':len(pdf)}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--client',required=True);parser.add_argument('--token',required=True);parser.add_argument('--mailbox',action='store_true');args=parser.parse_args()
    authorize(args.client,args.token,args.mailbox)
