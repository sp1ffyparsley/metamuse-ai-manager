"""Bounded application-document import from the authorized Google Drive.

No arbitrary URLs, filesystem reads, shell access, or credential transfer.
"""
import hashlib,io,zipfile
import json
import os
import re
import tempfile
import stat
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .store import Conflict, Denied
from .google_bridge import GoogleUnavailable

LIMIT = 8 * 1024**2

def drive_id(value):
    if not isinstance(value,str):raise Conflict('Use an explicit Google Drive file ID or link')
    if re.fullmatch(r'[A-Za-z0-9_-]{10,150}',value):return value
    p=urlsplit(value)
    if p.scheme=='https' and p.hostname in ('drive.google.com','docs.google.com') and not p.username and not p.password:
        found=re.search(r'/(?:file|document)/d/([A-Za-z0-9_-]{10,150})(?:/|$)',p.path)
        candidate=found[1] if found else parse_qs(p.query).get('id',[''])[0] if p.path=='/open' else ''
        if re.fullmatch(r'[A-Za-z0-9_-]{10,150}',candidate):return candidate
    raise Conflict('Use an explicit Google Drive file ID or link')

def safe_filename(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,110}\.(?i:pdf|docx)',value))

def valid_artifact(filename,data):
    if not safe_filename(filename) or len(data)>LIMIT:return False
    if filename.lower().endswith('.pdf'):return len(data)>=20 and data.startswith(b'%PDF-')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos=z.infolist();names={i.filename.lower() for i in infos}
            return len(infos)<=500 and sum(i.file_size for i in infos)<=50*1024**2 and {'[content_types].xml','word/document.xml'}<=names and not any('vbaproject' in n for n in names)
    except (OSError,ValueError,zipfile.BadZipFile):return False


def list_artifacts(root):
    """Bounded inventory of admitted PDFs/DOCX, never arbitrary paths or contents."""
    folder = Path(root) / 'artifacts'
    manifests = Path(root) / 'artifact-sources'
    if not folder.exists():
        return {'artifacts': [], 'limit': 50, 'truncated': False}
    if folder.is_symlink() or folder.resolve().parent != Path(root).resolve():
        raise Denied('Private artifact directory changed')
    rows = []
    scanned = 0
    # scandir bounds work even when unrelated or malicious entries exist.
    with os.scandir(folder) as entries:
        for entry in entries:
            scanned += 1
            if scanned > 500:
                break
            if not safe_filename(entry.name):
                continue
            fd = None
            try:
                fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not 20 <= info.st_size <= LIMIT:
                    continue
                # Imported files are immutable, private artifacts. Check bytes,
                # rather than claiming an old source manifest proves current data.
                with os.fdopen(fd, 'rb') as stream:
                    fd = None
                    data = stream.read(LIMIT + 1)
                if not valid_artifact(entry.name,data) or len(data) != info.st_size:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                row = {'artifact': entry.name, 'bytes': len(data), 'sha256': sha}
                source_path = manifests / (sha + '.json')
                if not manifests.is_symlink() and not source_path.is_symlink() and source_path.is_file() and source_path.stat().st_size <= 4096:
                    source = json.loads(source_path.read_text())
                    if source.get('artifact') == entry.name and source.get('sha256') == sha:
                        row['source'] = source.get('source')
                rows.append(row)
                if len(rows) == 50:
                    break
            except (OSError, ValueError, TypeError):
                continue
            finally:
                if fd is not None:
                    os.close(fd)
    return {'artifacts': sorted(rows, key=lambda x: x['artifact']), 'limit': 50,
            'truncated': len(rows) == 50 or scanned > 500}


def import_drive_pdf(api, root, file_id, filename=None, expected_sha=None):
    file_id=drive_id(file_id)
    if filename is not None and not safe_filename(filename):
        raise Conflict('Use a safe PDF/DOCX filename, not a filesystem path')
    if expected_sha is not None and (not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)):
        raise Conflict('Expected SHA-256 must be a complete lowercase digest')
    meta = api.call('drive', 'files/' + file_id + '?fields=id,name,mimeType,size,trashed,modifiedTime')
    if meta.get('id') != file_id or meta.get('trashed'):
        raise Denied('Select an existing, non-trashed approved Drive file')
    mime = meta.get('mimeType')
    word='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    if mime not in ('application/pdf',word,'application/vnd.google-apps.document'):
        raise Denied('Use an existing PDF, DOCX, or Google Doc exported as PDF')
    extension='.docx' if mime==word else '.pdf'
    if filename is None:
        stem=re.sub(r'\.(pdf|docx)$','',str(meta.get('name') or 'Muse_File_'+file_id),flags=re.I)
        stem=re.sub(r'[^A-Za-z0-9_.-]+','_',stem).strip('_.-')[:90] or 'Muse_File'
        filename=stem+extension
    if not filename.lower().endswith(extension):raise Conflict('Filename extension must match the Drive file type')
    if mime != 'application/vnd.google-apps.document' and int(meta.get('size', LIMIT + 1)) > LIMIT:
        raise Denied('File exceeds the 8 MiB transfer bound')
    if api.session is None:
        api.connect()
    endpoint = ('https://www.googleapis.com/drive/v3/files/' + file_id +
                ('/export?mimeType=application%2Fpdf' if mime != 'application/pdf' else '?alt=media'))
    folder = Path(root) / 'artifacts'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = folder / filename
    if folder.is_symlink() or folder.resolve().parent != Path(root).resolve():
        raise Denied('Private artifact directory changed')
    count = 0
    digest = hashlib.sha256()
    with api.session.get(endpoint, stream=True, timeout=15, allow_redirects=False) as response:
        if response.status_code != 200:
            raise GoogleUnavailable('Drive file download unavailable; check account and file access')
        declared = response.headers.get('Content-Length')
        if declared is not None and int(declared) > LIMIT:
            raise Denied('File exceeds the 8 MiB transfer bound')
        with tempfile.NamedTemporaryFile(dir=folder, prefix='.import-', delete=True) as temp:
            for chunk in response.iter_content(65536):
                count += len(chunk)
                if count > LIMIT:
                    raise Denied('File exceeds the 8 MiB transfer bound')
                digest.update(chunk)
                temp.write(chunk)
            temp.flush()
            temp.seek(0)
            if not valid_artifact(filename,temp.read(LIMIT+1)):
                raise Denied('Drive did not return a supported PDF/DOCX file')
            sha = digest.hexdigest()
            if expected_sha is not None and sha != expected_sha:
                raise Conflict('Source bytes differ from the expected file; do not upload')
            # link is atomic, exclusive and never overwrites an existing artifact.
            try:
                os.link(temp.name, dest)
            except FileExistsError:
                if dest.is_symlink() or not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != sha:
                    raise Conflict('Artifact filename already exists with different contents')
    record = {'artifact': filename, 'bytes': count, 'sha256': sha,
              'source': {'provider': 'google-drive', 'fileId': file_id,
                         'modifiedTime': meta.get('modifiedTime'), 'mimeType': mime}}
    manifests = Path(root) / 'artifact-sources'
    manifests.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=manifests, prefix='.pending-') as pending:
        temp = Path(pending) / 'source.json'
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            json.dump(record, stream)
        temp.replace(manifests / (sha + '.json'))
    return record
