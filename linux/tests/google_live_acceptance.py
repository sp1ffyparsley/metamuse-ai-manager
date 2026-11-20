"""Opt-in Google qualification. Writes only to newly created disposable files.

Run with an attended, authorized Desktop token. Never uses canonical tracker
IDs or records. The private report journals created IDs before further writes;
an interrupted run must be inspected, not blindly repeated. Cleanup uses Trash.
"""
import argparse
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from muse_monitor.google_bridge import Google, GoogleUnavailable, Outbox, Sheets, copy_merge_export
from muse_monitor.store import Conflict, Denied, Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--token', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--pdf', required=True)
    args = parser.parse_args()
    path = Path(args.report)
    if path.exists():
        raise SystemExit('Inspect the existing private report before another run.')
    if Path(args.pdf).exists():
        raise SystemExit('Preserve the existing PDF; choose a new output path.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = uuid.uuid4().hex
    report = {'run': run, 'started': time.time(), 'checks': {}, 'created': [],
              'canonicalWrites': False, 'complete': False}

    def save():
        tmp = path.with_name(path.name + '.new')
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
            json.dump(report, f, indent=2)
        tmp.replace(path)

    def created(file_id):
        assert file_id and file_id not in report['created']
        report['created'].append(file_id)
        save()
        return file_id

    def passed(name):
        report['checks'][name] = True
        save()
        print(json.dumps({'check': name, 'passed': True}), flush=True)

    class ObservedAPI:
        def __init__(self):
            self.api = Google(args.token)
            self.mutations = 0
            self.lose_insert_response = False

        def call(self, service, endpoint, method='GET', body=None, raw=False):
            # Data-filter POST lookups are reads, unlike batchUpdate mutations.
            mutating = method != 'GET' and not any(x in endpoint for x in (
                'developerMetadata:search', 'values:batchGetByDataFilter'))
            result = self.api.call(service, endpoint, method, body, raw)
            if mutating:
                self.mutations += 1
            if self.lose_insert_response and service == 'sheets' and any(
                    'insertDimension' in r for r in (body or {}).get('requests', [])):
                self.lose_insert_response = False
                raise GoogleUnavailable('Injected response loss after a verified real commit')
            return result

    api = ObservedAPI()
    save()
    try:
        folder = created(api.call('drive', 'files?fields=id', 'POST', {
            'name': 'Muse Monitor disposable qualification ' + run[:8],
            'mimeType': 'application/vnd.google-apps.folder',
            'appProperties': {'museQualification': run}})['id'])
        result = api.call('sheets', 'spreadsheets', 'POST', {
            'properties': {'title': 'Muse Monitor disposable data ' + run[:8]},
            'sheets': [{'properties': {'sheetId': 7, 'title': 'Data',
                                     'gridProperties': {'rowCount': 30, 'columnCount': 4}}}]})
        sid = created(result['spreadsheetId'])
        api.call('drive', 'files/' + sid + '?addParents=' + folder + '&fields=id', 'PATCH', {})
        headers = ['Record', 'State', 'Human note', 'Formula']
        values = [headers, ['A', 'ready', 'retain human', '=1+1'],
                  ['B', 'ready', 'other', '=2+2']]
        rows = []
        for row in values:
            rows.append({'values': [{'userEnteredValue': {
                'formulaValue' if str(v).startswith('=') else 'stringValue': v}}
                for v in row]})
        api.call('sheets', 'spreadsheets/' + sid + ':batchUpdate', 'POST', {
            'requests': [{'updateCells': {'start': {'sheetId': 7, 'rowIndex': 0, 'columnIndex': 0},
                                         'rows': rows, 'fields': 'userEnteredValue'}}]})
        schema = {'records': {'spreadsheet': sid, 'sheet': 'Data', 'sheetId': 7,
                             'headerRow': 1, 'keyHeader': 'Record', 'headers': headers,
                             'automationColumns': ['State']}}
        sheets = Sheets(api, schema)
        sheets.bind('records', 'A')
        sheets.update('records', 'A', {'State': 'before-sort'})
        api.call('sheets', 'spreadsheets/' + sid + ':batchUpdate', 'POST', {
            'requests': [{'sortRange': {'range': {'sheetId': 7, 'startRowIndex': 1,
                                                'endRowIndex': 3, 'startColumnIndex': 0,
                                                'endColumnIndex': 4},
                                        'sortSpecs': [{'dimensionIndex': 0, 'sortOrder': 'DESCENDING'}]}}]})
        sheets.update('records', 'A', {'State': 'after-sort'})
        row = sheets.row(schema['records'], sheets.metadata(schema['records'], 'muse.canonical', 'A'))
        assert row == ['A', 'after-sort', 'retain human', 2]
        passed('stable_metadata_after_real_sort')

        api.call('sheets', 'spreadsheets/' + sid + ':batchUpdate', 'POST', {
            'requests': [{'insertDimension': {'range': {'sheetId': 7, 'dimension': 'ROWS',
                                                       'startIndex': 1, 'endIndex': 2},
                                              'inheritFromBefore': True}}]})
        sheets.update('records', 'A', {'State': 'after-insert'})
        bounded = api.call('sheets', 'spreadsheets/' + sid +
                          '/values/%27Data%27%21A1%3AD8?valueRenderOption=FORMULA')['values']
        assert next(r for r in bounded if r and r[0] == 'A') == ['A', 'after-insert', 'retain human', '=1+1']
        assert next(r for r in bounded if r and r[0] == 'B') == ['B', 'ready', 'other', '=2+2']
        passed('insert_preserves_human_notes_formulas_and_other_rows')
        count = api.mutations
        try:
            sheets.update('records', 'A', {'Human note': 'MUST NOT WRITE'})
            raise AssertionError('Human field write was admitted')
        except Denied:
            assert api.mutations == count
        passed('human_column_write_denied_before_mutation')

        api.call('sheets', 'spreadsheets/' + sid + '/values/%27Data%27%21B1?valueInputOption=RAW',
                 'PUT', {'values': [['Changed header']]})
        count = api.mutations
        try:
            sheets.update('records', 'A', {'State': 'MUST NOT WRITE'})
            raise AssertionError('Header drift was admitted')
        except Conflict:
            assert api.mutations == count
        api.call('sheets', 'spreadsheets/' + sid + '/values/%27Data%27%21B1?valueInputOption=RAW',
                 'PUT', {'values': [['State']]})
        passed('live_header_drift_blocks_domain_write')

        with tempfile.TemporaryDirectory(prefix='muse-google-fixture-') as tmp:
            store = Store(Path(tmp) / 'journal.sqlite3')
            try:
                outbox = Outbox(store, sheets)
                outbox.enqueue('sandbox-insert-' + run, 'records', 'C', {'Record': 'C', 'State': 'queued'}, insert=True)
                api.lose_insert_response = True
                outbox.step()
                assert store.db.execute('SELECT state FROM outbox').fetchone()[0] == 'uncertain'
                count = api.mutations
                outbox.step()
                assert store.db.execute('SELECT state FROM outbox').fetchone()[0] == 'synced'
                assert api.mutations == count
                bounded = api.call('sheets', 'spreadsheets/' + sid +
                                  '/values/%27Data%27%21A1%3AD10?valueRenderOption=FORMULA')['values']
                assert sum(bool(r) and r[0] == 'C' for r in bounded) == 1
            finally:
                store.db.close()
        passed('injected_lost_response_after_real_insert_reconciles_once')

        template = created(api.call('docs', 'documents', 'POST', {
            'title': 'Muse Monitor disposable template ' + run[:8]})['documentId'])
        api.call('drive', 'files/' + template + '?addParents=' + folder + '&fields=id', 'PATCH', {})
        api.call('docs', 'documents/' + template + ':batchUpdate', 'POST', {
            'requests': [{'insertText': {'location': {'index': 1},
                                         'text': 'Muse Monitor qualification\nRun {{RUN}}\n'}}]})
        exported = copy_merge_export(api, template, folder, 'Muse Monitor disposable export ' + run[:8],
                                     {'{{RUN}}': 'sandbox verified'}, args.pdf)
        created(exported['document'])
        pdf = Path(args.pdf).read_bytes()
        assert pdf.startswith(b'%PDF-') and len(pdf) > 1000
        report['pdf'] = {'bytes': len(pdf), 'sha256': hashlib.sha256(pdf).hexdigest()}
        passed('real_docs_template_copy_merge_and_pdf_export')
        missing = Path(args.report).parent / ('absent-token-' + run + '.json')
        started = time.monotonic()
        try:
            Google(missing).connect()
            raise AssertionError('Missing credential was admitted')
        except GoogleUnavailable:
            assert time.monotonic() - started < 1
        passed('missing_credential_fails_closed')
        reconnect = Google(args.token)
        assert reconnect.call('drive', 'files/' + sid + '?fields=id')['id'] == sid
        passed('new_client_reconnect')
        report['complete'] = True
    except Exception as exc:
        report['errorType'] = type(exc).__name__
        print(json.dumps({'failed': True, 'errorType': type(exc).__name__}), flush=True)
    finally:
        # Only IDs returned by this run's creates/copy are eligible for cleanup.
        report['cleanup'] = {}
        for file_id in reversed(report['created']):
            try:
                result = api.call('drive', 'files/' + file_id + '?fields=id,trashed', 'PATCH', {'trashed': True})
                report['cleanup'][file_id] = result.get('trashed') is True
            except Exception as exc:
                report['cleanup'][file_id] = type(exc).__name__
            save()
        report['finished'] = time.time()
        save()
    return 0 if report['complete'] and all(v is True for v in report['cleanup'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
