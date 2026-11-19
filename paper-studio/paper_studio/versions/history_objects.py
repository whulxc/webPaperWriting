"""Private, append-only historical object catalogue. Queries never enumerate files.

Import is an explicit local management operation, independent of target_index and
legacy save/publish. Source bytes and exact manifest membership remain authority.
"""
from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat

from paper_studio.security.policy import builds_root, encoded, fail, no_links, path_for, relpath, safe_read, sha

SCHEMA = 'paper-studio.history-object.v1'
ROLES = frozenset({'manuscript_pdf','manuscript_tex','figure_pdf','figure_image','figure_source',
                  'table_pdf','table_image','table_source','section_tex','bibliography','named_source','review','unknown'})
LANGUAGES = frozenset({'en','zh','shared','unknown'})
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
BUILD = re.compile(r'rb_[0-9a-f]{24}\Z')
FILE_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
MAX_BYTES = 128 * 1024**2
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_IMPORT_ROWS = 100000
MAX_CANDIDATES = 16


def _label(value):
    if not isinstance(value,str) or not LABEL.fullmatch(value) or value in ('.','..','~'):
        fail('HISTORY_ARGUMENT','An explicit bounded version/revision is required')
    return value


def _hash(value):
    if not isinstance(value,str) or not HASH.fullmatch(value):
        fail('HISTORY_ARGUMENT','An exact lowercase SHA256 is required')
    return value


def path(cfg):
    return no_links(Path(cfg['state_root'])/'history-objects'/'index.sqlite3')


def _identity(cfg):
    roots={key:str(no_links(Path(cfg[key]))) for key in ('project_root','artifact_root','state_root')}
    state=Path(roots['state_root'])
    if any(state.is_relative_to(Path(roots[key])) or Path(roots[key]).is_relative_to(state)
           for key in ('project_root','artifact_root')):
        fail('HISTORY_STATE_UNSAFE','Catalogue state must be separate from project and artifacts')
    return encoded({'schema':SCHEMA,'project_id':cfg['project_id'],'native_project':cfg.get('native_project'),
                    'native_artifact_project':cfg.get('native_artifact_project'),**roots}).decode()


@contextlib.contextmanager
def _db(cfg,create=False):
    identity=_identity(cfg);p=path(cfg)
    if create:
        p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not p.exists():
            try:fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
            except FileExistsError:pass
    if not p.exists():
        fail('HISTORY_NOT_CONFIGURED','No historical objects have been explicitly imported for this project')
    directory=p.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_mode&0o077 or directory.st_uid!=os.getuid():
        fail('HISTORY_STATE_UNSAFE','Catalogue directory must be private and owned')
    info=p.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode&0o077 or info.st_uid!=os.getuid():
        fail('HISTORY_STATE_UNSAFE','Catalogue must be private, owned, regular and single-link')
    db=None
    try:
        # Runtime reads cannot create a DB, journal or schema as a side effect.
        db=sqlite3.connect(str(p) if create else p.as_uri()+'?mode=ro',uri=not create,timeout=20)
        db.row_factory=sqlite3.Row
        if create:
            db.execute('PRAGMA busy_timeout=20000')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),document TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS objects(object_id TEXT PRIMARY KEY,version TEXT NOT NULL,revision TEXT NOT NULL,
                language TEXT NOT NULL,role TEXT NOT NULL,figure_number INTEGER,target_key TEXT,document TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS history_selector ON objects(version,language,role,revision,figure_number,target_key);
              CREATE INDEX IF NOT EXISTS history_figure ON objects(version,language,role,figure_number,target_key,revision);
              CREATE TABLE IF NOT EXISTS imports(request_id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO identity VALUES(1,?)',(identity,));db.commit()
        row=db.execute('SELECT document FROM identity WHERE singleton=1').fetchone()
        if row is None or row['document']!=identity:
            fail('PROJECT_DENIED','Historical catalogue belongs to a different project or physical root')
        yield db
    except sqlite3.Error:
        fail('HISTORY_DATABASE_INVALID','Historical catalogue cannot be safely read or updated')
    finally:
        if db is not None:db.close()


def _json(raw):
    try:value=json.loads(raw)
    except (ValueError,RecursionError):fail('HISTORY_SOURCE_INVALID','Historical proof is not valid JSON')
    if not isinstance(value,dict):fail('HISTORY_SOURCE_INVALID','Historical proof must be an object')
    return value


def _member(rows,path):
    if not isinstance(rows,list):fail('HISTORY_SOURCE_INVALID','Historical manifest has no member list')
    matches=[r for r in rows if isinstance(r,dict) and r.get('path')==path]
    if len(matches)!=1:fail('HISTORY_MEMBERSHIP','Source must occur exactly once in its historical manifest')
    return matches[0]


def _source(cfg,source):
    """Return named root, relative path, proof; never traverse an unselected tree."""
    if not isinstance(source,dict):fail('HISTORY_ARGUMENT','A structured source is required')
    kind=source.get('kind');relative=relpath(source.get('path')).as_posix()
    if kind=='project':
        if set(source)!={'kind','path'}:fail('HISTORY_ARGUMENT','Project source needs kind and path only')
        path_for(cfg,relative)
        return Path(cfg['project_root']),relative,{},32*1024**2
    if kind=='version':
        if set(source)!={'kind','version','path'}:fail('HISTORY_ARGUMENT','Version source needs exact version and member')
        version=_label(source['version'])
        root=no_links(Path(cfg['artifact_root'])/'versions'/version)
        raw=safe_read(root,'manifest.json',MAX_MANIFEST_BYTES);manifest=_json(raw)
        if manifest.get('schema')!='paper-studio.version.v1' or manifest.get('project_id')!=cfg['project_id'] or manifest.get('version')!=version:
            fail('PROJECT_DENIED','Historical version does not belong to selected project')
        member=_member(manifest.get('files'),relative)
        return root,relative,{'manifest_sha256':sha(raw),'member_sha256':member.get('sha256'),'member_bytes':member.get('bytes')},MAX_BYTES
    if kind=='build':
        if set(source)!={'kind','build_id','path'} or not isinstance(source.get('build_id'),str) or not BUILD.fullmatch(source['build_id']):
            fail('HISTORY_ARGUMENT','Build source needs an exact provider build ID and member')
        parts=Path(relative).parts
        if len(parts)<2 or parts[0] not in ('source','output'):
            fail('HISTORY_MEMBERSHIP','Build reads admit only registered source/output members')
        root=no_links(builds_root(cfg)/source['build_id'])
        filename='request.json' if parts[0]=='source' else 'manifest.json'
        raw=safe_read(root,filename,MAX_MANIFEST_BYTES);manifest=_json(raw)
        expected='paper-studio.build-request.v1' if parts[0]=='source' else 'paper-studio.build-manifest.v1'
        if manifest.get('schema')!=expected or manifest.get('project_id')!=cfg['project_id'] or manifest.get('build_id')!=source['build_id']:
            fail('PROJECT_DENIED','Build proof does not belong to selected project')
        rows=manifest.get('snapshot',{}).get('files') if parts[0]=='source' else manifest.get('artifacts')
        member=_member(rows,Path(*parts[1:]).as_posix())
        return root,relative,{'manifest_sha256':sha(raw),'member_sha256':member.get('sha256'),'member_bytes':member.get('bytes')},MAX_BYTES
    fail('HISTORY_ARGUMENT','Unknown historical source kind')


def _read_source(cfg,source,expected_sha,expected_bytes,proof=None):
    root,relative,current,limit=_source(cfg,source)
    if expected_bytes>limit:fail('HISTORY_INPUT_LIMIT','Historical object exceeds its existing source read limit')
    if proof is not None and current!=proof:fail('HISTORY_PROOF_CHANGED','Historical manifest identity changed')
    if current and (current['member_sha256']!=expected_sha or current['member_bytes']!=expected_bytes):
        fail('HISTORY_MEMBERSHIP','Selected hash/size is not the exact historical manifest member')
    raw=safe_read(root,relative,limit)
    if len(raw)!=expected_bytes or sha(raw)!=expected_sha:
        fail('HISTORY_OBJECT_CHANGED','Historical source bytes differ from imported identity')
    return raw,current


def _pointer(value,pointer):
    if not isinstance(pointer,str) or len(pointer)>1024 or pointer and not pointer.startswith('/'):
        fail('HISTORY_RECEIPT_INVALID','Receipt locations must be bounded JSON pointers')
    try:
        for part in pointer.split('/')[1:] if pointer else []:
            if re.search(r'~(?![01])',part):raise ValueError()
            key=part.replace('~1','/').replace('~0','~')
            value=value[int(key)] if isinstance(value,list) and re.fullmatch(r'0|[1-9][0-9]*',key) else value[key]
        return value
    except (KeyError,IndexError,TypeError,ValueError):fail('HISTORY_RECEIPT_INVALID','Receipt pointer has no exact value')


def _drive(cfg,row,importing=False):
    ref=row.get('drive_ref')
    if ref is None:return None
    fields={'file_id','receipt_source','receipt_sha256','entry_pointer','id_pointer','sha256_pointer','bytes_pointer'}
    if not isinstance(ref,dict) or set(ref)-fields-{'receipt_bytes','receipt_proof'} or not fields<=set(ref):
        fail('HISTORY_RECEIPT_INVALID','Drive reference requires an exact local receipt proof')
    if not isinstance(ref['file_id'],str) or not FILE_ID.fullmatch(ref['file_id']):fail('HISTORY_RECEIPT_INVALID','Invalid Drive file identity')
    _hash(ref['receipt_sha256'])
    if importing:
        root,relative,proof,limit=_source(cfg,ref['receipt_source'])
        raw=safe_read(root,relative,min(limit,MAX_MANIFEST_BYTES))
        ref['receipt_bytes']=len(raw);ref['receipt_proof']=proof
        if sha(raw)!=ref['receipt_sha256']:fail('HISTORY_RECEIPT_INVALID','Receipt SHA does not match')
        if proof and (proof['member_sha256']!=sha(raw) or proof['member_bytes']!=len(raw)):
            fail('HISTORY_RECEIPT_INVALID','Receipt is not its exact manifest member')
    else:
        raw,_=_read_source(cfg,ref['receipt_source'],ref['receipt_sha256'],ref['receipt_bytes'],ref['receipt_proof'])
    entry=_pointer(_json(raw),ref['entry_pointer'])
    if (_pointer(entry,ref['id_pointer'])!=ref['file_id'] or _pointer(entry,ref['sha256_pointer'])!=row['sha256']
            or type(_pointer(entry,ref['bytes_pointer'])) is not int or _pointer(entry,ref['bytes_pointer'])!=row['bytes']):
        fail('HISTORY_RECEIPT_INVALID','Receipt ID/hash/size do not identify this exact historical object')
    return {'file_id':ref['file_id'],'url':'https://drive.google.com/file/d/'+ref['file_id']+'/view',
            'verification':'local_receipt_sha_verified','remote_verification':'not_probed'}


def _normalize(cfg,value):
    if not isinstance(value,dict):fail('HISTORY_ARGUMENT','Historical row must be an object')
    required={'schema','project_id','version','revision','language','role','source','sha256','bytes'}
    optional={'figure_number','target_key','figure_version','pages','provenance','drive_ref'}
    if not required<=set(value) or set(value)-required-optional:fail('HISTORY_ARGUMENT','Unexpected or missing historical row fields')
    try:row=json.loads(json.dumps(value,allow_nan=False))
    except (ValueError,TypeError,RecursionError):fail('HISTORY_ARGUMENT','Historical row is not bounded JSON')
    if row['schema']!=SCHEMA or row['project_id']!=cfg['project_id']:fail('PROJECT_DENIED','Import row belongs to another project')
    for name in ('version','revision'):_label(row[name])
    if isinstance(row['source'],dict) and row['source'].get('kind')=='version' and row['source'].get('version')!=row['version']:
        fail('HISTORY_ARGUMENT','A frozen version member must retain its exact archive version label')
    if not isinstance(row['language'],str) or not isinstance(row['role'],str) or row['language'] not in LANGUAGES or row['role'] not in ROLES:fail('HISTORY_ARGUMENT','Unknown historical role/language')
    _hash(row['sha256'])
    if type(row['bytes']) is not int or not 0<=row['bytes']<=MAX_BYTES:fail('HISTORY_ARGUMENT','Invalid historical byte size')
    if 'figure_number' in row and (type(row['figure_number']) is not int or not 1<=row['figure_number']<=999999):fail('HISTORY_ARGUMENT','Invalid figure number')
    if 'target_key' in row and (not isinstance(row['target_key'],str) or not KEY.fullmatch(row['target_key'])):fail('HISTORY_ARGUMENT','Invalid named target')
    if 'figure_number' in row and 'target_key' in row:fail('HISTORY_ARGUMENT','Choose numeric or named target')
    if 'figure_version' in row:_label(row['figure_version'])
    if 'pages' in row and (type(row['pages']) is not int or not 1<=row['pages']<=10000):fail('HISTORY_ARGUMENT','Invalid page count')
    if len(encoded(row.get('provenance',{})))>16384:fail('HISTORY_ARGUMENT','Provenance is too large')
    raw,proof=_read_source(cfg,row['source'],row['sha256'],row['bytes'])
    row['source_proof']=proof
    _drive(cfg,row,importing=True)
    row['object_id']=_object_id(row)
    return row


def import_rows(cfg,rows,*,request_id):
    """Explicit local append import; batch CAS/idempotence and SQLite transaction."""
    _label(request_id)
    if not isinstance(rows,list) or not 1<=len(rows)<=MAX_IMPORT_ROWS:fail('HISTORY_ARGUMENT','A bounded nonempty explicit import list is required')
    # Import may inspect declared manifest rows; it never discovers additional paths.
    normalized=[_normalize(cfg,row) for row in rows]
    digest=sha(encoded(normalized))
    with _db(cfg,create=True) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            prior=db.execute('SELECT digest,result FROM imports WHERE request_id=?',(request_id,)).fetchone()
            if prior:
                if prior['digest']!=digest:fail('HISTORY_IMPORT_CONFLICT','Request ID was already used for another import')
                result=json.loads(prior['result']);db.rollback();return {**result,'idempotent_replay':True}
            inserted=0
            for row in normalized:
                old=db.execute('SELECT document FROM objects WHERE object_id=?',(row['object_id'],)).fetchone()
                document=encoded(row).decode()
                if old:
                    if old['document']!=document:fail('HISTORY_IMPORT_CONFLICT','Existing object metadata differs; history is never silently overwritten')
                    continue
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?,?)',(row['object_id'],row['version'],row['revision'],row['language'],row['role'],row.get('figure_number'),row.get('target_key'),document));inserted+=1
            result={'schema':SCHEMA,'project_id':cfg['project_id'],'request_id':request_id,'input_sha256':digest,'imported':inserted,'reused':len(normalized)-inserted,'objects':[row['object_id'] for row in normalized],'idempotent_replay':False}
            db.execute('INSERT INTO imports VALUES(?,?,?)',(request_id,digest,encoded(result).decode()));db.commit();return result
        except BaseException:
            db.rollback();raise



def _object_id(row):
    return sha(encoded({key:row.get(key) for key in ('project_id','version','revision','language','role','figure_number','target_key','source','sha256','bytes')}))


def _stored(cfg,stored):
    row=_json(stored['document'])
    fields=('object_id','version','revision','language','role','figure_number','target_key')
    if (row.get('schema')!=SCHEMA or row.get('project_id')!=cfg['project_id']
            or any(row.get(key)!=stored[key] for key in fields) or _object_id(row)!=row.get('object_id')):
        fail('HISTORY_DATABASE_INVALID','Historical row identity differs from its protected selector')
    return row


def _public(row):
    return {key:row[key] for key in ('object_id','version','revision','language','role','sha256','bytes','figure_number','target_key','figure_version','pages') if key in row}


def resolve(cfg,*,version,language,role,revision=None,figure_number=None,target_key=None,object_id=None,offset=None,limit=None):
    _label(version)
    if revision is not None:_label(revision)
    if not isinstance(language,str) or not isinstance(role,str) or language not in LANGUAGES or role not in ROLES:fail('HISTORY_ARGUMENT','Unknown historical role/language')
    if figure_number is not None and (type(figure_number) is not int or not 1<=figure_number<=999999):fail('HISTORY_ARGUMENT','Invalid figure number')
    if target_key is not None and (not isinstance(target_key,str) or not KEY.fullmatch(target_key)):fail('HISTORY_ARGUMENT','Invalid target key')
    if figure_number is not None and target_key is not None:fail('HISTORY_ARGUMENT','Choose numeric or named selector')
    if object_id is not None:_hash(object_id)
    if (offset is None)!=(limit is None) or offset is not None and (type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=262144):
        fail('HISTORY_ARGUMENT','Content requires one bounded offset and limit')
    conditions=['version=?','language=?','role=?'];values=[version,language,role]
    for key,val in [('revision',revision),('figure_number',figure_number),('target_key',target_key),('object_id',object_id)]:
        if val is not None:conditions.append(key+'=?');values.append(val)
    with _db(cfg) as db:
        rows=db.execute('SELECT * FROM objects WHERE '+' AND '.join(conditions)+' LIMIT ?',values+[MAX_CANDIDATES+1]).fetchall()
    common={'project_id':cfg['project_id'],'config_revision':cfg.get('_config_revision'),'query':{k:v for k,v in zip(('version','language','role'),(version,language,role))},'historical':True,'current_working_source':False,'directory_enumeration_attempts':0}
    if not rows:return {**common,'status':'not_found','search_fallback':False}
    if len(rows)>1:
        return {**common,'status':'ambiguous','candidates':[_public(_stored(cfg,row)) for row in rows[:MAX_CANDIDATES]],'has_more':len(rows)>MAX_CANDIDATES,'candidate_bytes_verified':False,'guidance':'Specify revision, numeric/named target or one returned object_id; no latest object is guessed.'}
    row=_stored(cfg,rows[0])
    raw,_=_read_source(cfg,row['source'],row['sha256'],row['bytes'],row['source_proof'])
    drive=_drive(cfg,row)
    result={**common,**_public(row),'status':'found','sha256_verified':True,'mime_type':mimetypes.guess_type(row['source']['path'])[0] or 'application/octet-stream','source':row['source'],'provenance':row.get('provenance',{}),'drive_receipt':drive}
    if offset is not None:
        chunk=raw[offset:offset+limit]
        result.update(offset=offset,chunk_bytes=len(chunk),chunk_base64=base64.b64encode(chunk).decode(),eof=offset+len(chunk)>=len(raw))
    return result
