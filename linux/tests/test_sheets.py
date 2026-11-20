import copy,json,tempfile,unittest
from pathlib import Path
from muse_monitor.google_bridge import Sheets,Outbox,GoogleUnavailable
from muse_monitor.store import Store,Denied,Conflict

class SheetAPI:
    def __init__(self):
        self.rows=[['Record','State','Human note','Formula'],['A','ready','retain human','=1+1'],['B','ready','other','=2+2']]
        self.meta={1:{'metadataId':1,'metadataKey':'muse.canonical','metadataValue':'A','visibility':'DOCUMENT','location':{'dimensionRange':{'sheetId':7,'dimension':'ROWS','startIndex':1,'endIndex':2}}}}
        self.writes=0;self.lose=False;self.next=2
    def call(self,service,path,method='GET',body=None,raw=False):
        if '?fields=sheets' in path:return {'sheets':[{'properties':{'sheetId':7,'title':'Data','gridProperties':{'rowCount':max(10,len(self.rows)),'columnCount':4}}}]}
        if path.endswith('developerMetadata:search'):
            lookup=body['dataFilters'][0]['developerMetadataLookup']
            return {'matchedDeveloperMetadata':[{'developerMetadata':copy.deepcopy(m)} for m in self.meta.values() if m['metadataKey']==lookup['metadataKey'] and m['metadataValue']==lookup['metadataValue']]}
        if ':batchGetByDataFilter' in path:
            mid=body['dataFilters'][0]['developerMetadataLookup']['metadataId'];index=self.meta[mid]['location']['dimensionRange']['startIndex']
            return {'valueRanges':[{'valueRange':{'values':[copy.deepcopy(self.rows[index])]}}]}
        if ':batchUpdateByDataFilter' in path:
            data=body['data'][0];mid=data['dataFilter']['developerMetadataLookup']['metadataId'];index=self.meta[mid]['location']['dimensionRange']['startIndex']
            for col,value in enumerate(data['values'][0]):
                if value is not None:self.rows[index][col]=value
            self.writes+=1;return {}
        if ':batchUpdate' in path:
            for r in body['requests']:
                if 'insertDimension' in r:
                    index=r['insertDimension']['range']['startIndex'];self.rows.insert(index,['']*4)
                    for m in self.meta.values():
                        d=m['location']['dimensionRange']
                        if d['startIndex']>=index:d['startIndex']+=1;d['endIndex']+=1
                if 'updateCells' in r:
                    index=r['updateCells']['start']['rowIndex'];cells=r['updateCells']['rows'][0]['values']
                    self.rows[index]=[next(iter(c['userEnteredValue'].values())) if c else '' for c in cells]
                if 'createDeveloperMetadata' in r:
                    m=copy.deepcopy(r['createDeveloperMetadata']['developerMetadata']);m['metadataId']=self.next;self.meta[self.next]=m;self.next+=1
            self.writes+=1
            if self.lose:self.lose=False;raise GoogleUnavailable('Response lost after commit')
            return {}
        if '/values/' in path:
            from urllib.parse import unquote
            r=unquote(path.split('/values/')[1]).split('?')[0]
            return {'values':copy.deepcopy(self.rows[:1] if '!A1:' in r else self.rows[1:])}
        raise AssertionError(path)

class SheetTests(unittest.TestCase):
    def setUp(self):
        self.api=SheetAPI();self.schema={'records':{'spreadsheet':'fixture','sheet':'Data','sheetId':7,'headerRow':1,'keyHeader':'Record','headers':['Record','State','Human note','Formula'],'automationColumns':['State']}}
        self.s=Sheets(self.api,self.schema)
    def test_sort_and_insert_keep_human_fields_and_formulas(self):
        self.api.rows[1],self.api.rows[2]=self.api.rows[2],self.api.rows[1]
        self.api.meta[1]['location']['dimensionRange'].update(startIndex=2,endIndex=3)
        self.s.update('records','A',{'State':'submitted'})
        self.assertEqual(self.api.rows[2],['A','submitted','retain human','=1+1'])
        self.assertEqual(self.api.rows[1][0],'B')
        self.s.insert('records','C',{'Record':'C','State':'queued'},'op-C')
        self.assertEqual(self.api.rows[2][2:],[ 'retain human','=1+1'])
    def test_human_write_and_header_drift_denied(self):
        with self.assertRaises(Denied):self.s.update('records','A',{'Human note':'overwrite'})
        with self.assertRaises(Denied):self.s.insert('records','C',{'Record':'C','Human note':'agent supplied'},'op-C')
        self.api.rows[0][1]='Changed'
        with self.assertRaises(Conflict):self.s.update('records','A',{'State':'done'})
        self.assertEqual(self.api.writes,0)
    def test_changed_identity_stops_domain_writes(self):
        self.api.rows[1][0]='new-human-identity'
        with self.assertRaises(Conflict):self.s.update('records','A',{'State':'done'})
        self.assertEqual(self.api.writes,0)
    def test_lost_append_response_reconciles_without_duplicate(self):
        with tempfile.TemporaryDirectory() as name:
            store=Store(Path(name)/'db');outbox=Outbox(store,self.s)
            outbox.enqueue('fixture-op-C','records','C',{'Record':'C','State':'queued'},insert=True)
            self.api.lose=True;outbox.step()
            self.assertEqual(store.db.execute('SELECT state FROM outbox').fetchone()[0],'uncertain')
            outbox.step()
            self.assertEqual(store.db.execute('SELECT state FROM outbox').fetchone()[0],'synced')
            self.assertEqual(sum(r[0]=='C' for r in self.api.rows),1);self.assertEqual(self.api.writes,1)
            store.db.close()
    def test_duplicate_canonical_metadata_is_not_authority(self):
        self.api.meta[2]=copy.deepcopy(self.api.meta[1]);self.api.meta[2]['metadataId']=2
        with self.assertRaises(Conflict):self.s.update('records','A',{'State':'done'})

if __name__=='__main__':unittest.main()
