"""Fixed, project-scoped background wrapper for the existing source sync engine.

Only apply-sync uses a child process. Plans, review import and the synchronous
engine remain unchanged. The public request ID is the durable reconciliation
identity; no caller-selected command, environment or worker path is accepted.
"""
from __future__ import annotations

import argparse,json,os,re,subprocess,sys,time,traceback
from pathlib import Path
if __package__ in (None,''):
    sys.path[:0]=[str(Path(__file__).resolve().parents[2]),str(Path(__file__).resolve().parents[3])]

from paper_studio.contracts import BY_NAME,validate
from paper_studio.drive import publisher,source_sync
from paper_studio.provider.jobs import code_identity,drive_deadline,process_identity
from paper_studio.security.policy import (StudioError,atomic_json,encoded,fail,locked,
    load_config,no_links,project,safe_read,select_project,sha)
from paper_studio.capabilities import require_admitted
from paper_studio.provider import job_index

REQUEST=re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
KEY=re.compile(r'[0-9a-f]{64}\Z')
ACTIVE={'queued','running','recovery_required'}


class LaunchUncertain(StudioError):
    def __init__(self,identity):
        self.identity=identity
        super().__init__('SOURCE_SYNC_WORKER_UNKNOWN','Worker may have started but its launch acknowledgement was not persisted; query this same request before recovery')


def _error(error):
    code=error.code if isinstance(error,StudioError) else 'SOURCE_SYNC_WORKER_FAILED'
    return {'code':code,'message':'Controlled source synchronization did not complete; retain this request_id and reconcile its local engine receipt'}


def _request_id(value):
    if not isinstance(value,str) or not REQUEST.fullmatch(value):
        fail('INVALID_ARGUMENT','Source synchronization requires a bounded stable request_id')
    return value


def _private(cfg):
    path=no_links(Path(cfg['state_root'])/'drive-source-sync'/'tasks')
    if path.exists() and not path.is_dir():fail('TASK_TAMPERED','Source task index is not a directory')
    return path


def _directory(cfg,request_id):
    return _private(cfg)/sha(_request_id(request_id).encode())


def _read(path):
    return json.loads(safe_read(path.parent,path.name,4*1024*1024))


def _config_unchanged(cfg,expected):
    path=no_links(Path(cfg['_config_path']))
    if sha(safe_read(path.parent,path.name,2*1024*1024))!=expected:
        fail('CONFIG_CHANGED','Project configuration bytes changed during background synchronization')


def _task_record(cfg,directory):
    record=_read(directory/'request.json')
    if (record.get('schema')!='paper-editor.source-sync-task.v1' or record.get('project_id')!=cfg['project_id']
        or sha(_request_id(record.get('request_id')).encode())!=directory.name
        or record.get('task_id')!='ss_'+sha(encoded([cfg['project_id'],record['request_id']]))[:24]):
        fail('PROJECT_DENIED','Background sync task identity differs from the selected project')
    if sha(encoded(record['arguments']))!=record.get('request_sha256'):
        fail('TASK_TAMPERED','Background sync request identity changed')
    return record


def _engine_receipt(cfg,request_id):
    path=no_links(Path(cfg['state_root'])/'drive-source-sync/requests'/
                  (sha(_request_id(request_id).encode())+'.json'))
    if not path.exists():return None
    value=_read(path)
    if value.get('project_id')!=cfg['project_id'] or value.get('request_id')!=request_id:
        fail('PROJECT_DENIED','Source receipt belongs to another project or request')
    return value


def _completed_receipt(cfg,args):
    value=_engine_receipt(cfg,args['request_id'])
    if value is None:return None
    if value.get('snapshot_sha256')!=args['expected_snapshot_sha256']:
        fail('IDEMPOTENCY_CONFLICT','Request ID belongs to another immutable source snapshot')
    if value.get('status')!='completed' or value.get('sync_complete') is not True:return None
    return {**source_sync.public_sync(value),'asynchronous':False,'accepted':False,
            'receipt_only':True,'remote_revalidated':False,
            'receipt_scope':'last_completed_engine_receipt; no new remote metadata verification'}


def _attempt(cfg,directory):
    pointer=_read(directory/'current.json');number=pointer.get('attempt')
    if type(number) is not int or not 1<=number<=1000:
        fail('TASK_TAMPERED','Background sync attempt identity is invalid')
    attempt=no_links(directory/('attempt-'+str(number).zfill(6)))
    state=_read(attempt/'status.json')
    if (state.get('project_id')!=cfg['project_id'] or state.get('attempt')!=number
        or state.get('task_id')!=_task_record(cfg,directory)['task_id']):
        fail('PROJECT_DENIED','Background sync attempt belongs to another project')
    if state.get('status') not in ACTIVE|{'completed','failed'}:
        fail('TASK_TAMPERED','Background sync attempt has an unknown state')
    return number,attempt,state


def _observed_state(attempt,state):
    identity=state.get('worker_identity')
    if identity is None and (attempt/'launch.json').exists():identity=_read(attempt/'launch.json')
    if state.get('status')=='failed' and identity is None and (attempt/'launch-intent.json').exists():
        return {**state,'status':'recovery_required','error':{'code':'SOURCE_SYNC_WORKER_UNKNOWN',
                'message':'Failed launch has no reliable worker identity; local reconciliation is required'}}
    if state.get('status') not in {'queued','running'}:return state
    live=process_identity(identity['pid']) if isinstance(identity,dict) and type(identity.get('pid')) is int else None
    lost=(identity is not None and (live is None or live.get('state')=='Z'
          or live.get('start_ticks')!=identity.get('start_ticks')))
    if lost or identity is None and time.time()-state.get('created_at',0)>15:
        code='WORKER_LOST' if lost else ('SOURCE_SYNC_WORKER_UNKNOWN' if (attempt/'launch-intent.json').exists() else 'SCHEDULE_INTERRUPTED')
        return {**state,'status':'recovery_required','error':{'code':code,
                'message':'Worker state needs reconciliation; an unacknowledged launch cannot be blindly repeated'}}
    return state


def read_task(cfg,request_id):
    directory=_directory(cfg,request_id)
    if not (directory/'request.json').exists():return None
    record=_task_record(cfg,directory)
    if not (directory/'current.json').exists():
        uncertain=any(directory.glob('attempt-*/launch-intent.json')) or any(directory.glob('attempt-*/launch.json'))
        return {'project_id':cfg['project_id'],'request_id':request_id,'task_id':record['task_id'],
                'snapshot_sha256':record['arguments']['expected_snapshot_sha256'],'attempt':0,
                'status':'recovery_required','worker_status':'recovery_required','asynchronous':True,
                'accepted':False,'sync_complete':False,'receipt_only':False,'remote_revalidated':False,
                'error':{'code':'SOURCE_SYNC_WORKER_UNKNOWN' if uncertain else 'SCHEDULE_INTERRUPTED',
                         'message':'Task request was saved without its active-attempt pointer; reconcile launch state before another process'}}
    number,attempt,state=_attempt(cfg,directory)
    state=_observed_state(attempt,state);status=state.get('status')
    receipt=_engine_receipt(cfg,request_id)
    if receipt and receipt.get('snapshot_sha256')!=record['arguments']['expected_snapshot_sha256']:
        fail('TASK_TAMPERED','Engine receipt and background request have different source snapshots')
    if status=='completed' and (receipt is None or receipt.get('sync_complete') is not True):
        fail('SOURCE_SYNC_RECEIPT_MISSING','Completed background task has no completed engine receipt')
    result=source_sync.public_sync(receipt) if receipt and status in {'completed','failed','recovery_required'} else {}
    result.update(project_id=cfg['project_id'],request_id=request_id,task_id=record['task_id'],
                  snapshot_sha256=record['arguments']['expected_snapshot_sha256'],
                  status=status,worker_status=status,attempt=number,asynchronous=True,
                  accepted=status in {'queued','running'},sync_complete=status=='completed',
                  receipt_only=status=='completed',remote_revalidated=False,
                  worker_remote_revalidated=state.get('remote_revalidated',False))
    if receipt:
        result['engine_status']=receipt.get('status')
        result['progress']={'verified_files':len(receipt.get('files',[])),
                            'expected_files':len(receipt.get('source_files',[]))}
    if state.get('error'):result['error']=publisher.public_receipt(state['error'])
    return result


def active_tasks(cfg):
    """Local-only process reconciliation for runtime, deployment and rebind."""
    if job_index.enabled(cfg):
        result=[]
        for row in job_index.operation_ids(cfg,'sync_task'):
            observed=read_task(cfg,row['operation_id'])
            if observed is None:fail('SOURCE_SYNC_WORKER_UNKNOWN','Indexed source task has no durable request; reconcile the interrupted scheduling')
            job_index.track_operation(cfg,'sync_task',row['operation_id'],row['record_name'],observed['status'])
            if observed['status'] in ACTIVE:
                result.append({'request_id':row['operation_id'],'status':observed['status']})
        return result
    directory=_private(cfg)
    if not directory.exists():return []
    paths=sorted(directory.glob('*/request.json'))
    if len(paths)>4096:fail('INPUT_LIMIT','Source synchronization task history exceeds its bounded index')
    result=[]
    for path in paths:
        record=_task_record(cfg,path.parent);observed=read_task(cfg,record['request_id'])
        if observed.get('status') in ACTIVE:
            result.append({'request_id':record['request_id'],'status':observed['status']})
    return result

def _save_status(cfg,directory,attempt,status,exclusive=False):
    atomic_json(attempt/'status.json',status,exclusive)
    record=_task_record(cfg,directory)
    job_index.track_operation(cfg,'sync_task',record['request_id'],directory.name,status['status'])


def _launch(cfg,directory,number,attempt):
    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--config',cfg['_config_path'],
             '--project-id',cfg['project_id'],'--task-key',directory.name,'--attempt',str(number)]
    atomic_json(attempt/'launch-intent.json',{'created_at':time.time(),'parent_identity':process_identity(os.getpid())},True)
    with (attempt/'worker.log').open('xb') as log:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
            start_new_session=True,close_fds=True,
            env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    try:identity=process_identity(process.pid)
    except Exception:raise LaunchUncertain(None) from None
    if identity is None:raise LaunchUncertain(None)
    try:atomic_json(attempt/'launch.json',identity,True)
    except Exception:raise LaunchUncertain(identity) from None


def _restart_attempt(cfg,directory):
    if not (directory/'current.json').exists():
        numbers=[]
        for path in directory.glob('attempt-*'):
            if not re.fullmatch(r'attempt-[0-9]{6}',path.name):fail('TASK_TAMPERED','Unknown background attempt directory')
            no_links(path);numbers.append(int(path.name[-6:]))
            if (path/'launch-intent.json').exists() or (path/'launch.json').exists():
                fail('SOURCE_SYNC_WORKER_UNKNOWN','Interrupted scheduling has a launch intent; local worker identity must be reconciled before another launch')
        return max(numbers,default=0)+1
    previous,attempt,state=_attempt(cfg,directory)
    known=state.get('worker_identity')
    if known is None and (attempt/'launch.json').exists():known=_read(attempt/'launch.json')
    if known is None and (attempt/'launch-intent.json').exists():
        fail('SOURCE_SYNC_WORKER_UNKNOWN','A recorded launch has no reliable worker identity; refusing a second process')
    if _observed_state(attempt,state).get('status')=='recovery_required':
        identity=state.get('worker_identity')
        if identity is None and (attempt/'launch.json').exists():identity=_read(attempt/'launch.json')
        if identity is None and (attempt/'launch-intent.json').exists():
            fail('SOURCE_SYNC_WORKER_UNKNOWN','A recorded launch has no reliable worker identity; refusing a second process')
        live=process_identity(identity['pid']) if identity else None
        if live and live.get('state')!='Z' and live.get('start_ticks')==identity.get('start_ticks'):
            fail('SOURCE_SYNC_BUSY','Original source worker is still alive; do not launch another one')
    return previous+1


def submit(cfg,args):
    project(cfg,args.get('project_id'));require_admitted(cfg,'paper_source_sync')
    validate(args,BY_NAME['paper_source_sync']['inputSchema'])
    allowed={'project_id','action','request_id','dry_run','authorized','expected_snapshot_sha256'}
    if args.get('action')!='sync' or set(args)-allowed or args.get('dry_run',True) is not False:
        fail('INVALID_ARGUMENT','Background source sync accepts only the existing apply-sync arguments')
    policy=source_sync.config(cfg)
    if policy['enabled'] is not True or args.get('authorized') is not True:
        fail('AUTHORIZATION_REQUIRED','Source sync apply requires enabled project policy and authorized=true')
    request_id=_request_id(args.get('request_id'));expected=args.get('expected_snapshot_sha256')
    if not isinstance(expected,str) or not KEY.fullmatch(expected):
        fail('REVISION_REQUIRED','Sync requires the exact immutable snapshot SHA256 from plan')
    arguments=dict(args);body_sha=sha(encoded(arguments));directory=_directory(cfg,request_id)
    with locked(cfg,'source-sync-submit'):
        old=None
        if (directory/'request.json').exists():
            old=_task_record(cfg,directory)
            if old['request_sha256']!=body_sha:
                fail('IDEMPOTENCY_CONFLICT','Background request_id is already bound to different source sync arguments')
            current=read_task(cfg,request_id)
            if current['status']=='completed':return {**current,'worker_started':False,'idempotent_replay':True}
            if old['config_revision']!=cfg['_config_revision']:
                fail('CONFIG_CHANGED','Project configuration changed; the old background task cannot be restarted under another binding')
            if old['code_sha256']!=code_identity():
                fail('CODE_CHANGED','Background task code changed; inspect the retained receipt before a controlled new request')
            if current['status'] in {'queued','running'}:return {**current,'worker_started':False,'idempotent_replay':True}
        else:
            completed=_completed_receipt(cfg,args)
            if completed is not None:return completed
        busy=[row for row in active_tasks(cfg) if row['request_id']!=request_id]
        if busy:fail('SOURCE_SYNC_BUSY','Another project source task needs completion or reconciliation',request_id=busy[0]['request_id'],status=busy[0]['status'])
        if old is None:
            source_sync.state_root(cfg)  # Create the existing engine parent privately first.
            publisher._private_state_directory(_private(cfg));publisher._private_state_directory(directory)
            old={'schema':'paper-editor.source-sync-task.v1','project_id':cfg['project_id'],'request_id':request_id,
                 'task_id':'ss_'+sha(encoded([cfg['project_id'],request_id]))[:24],
                 'arguments':arguments,'request_sha256':body_sha,'config_revision':cfg['_config_revision'],
                 'policy_sha256':sha(encoded(policy)),'code_sha256':code_identity(),'created_at':time.time()}
            job_index.track_operation(cfg,'sync_task',request_id,directory.name,'preparing')
            atomic_json(directory/'request.json',old,True);number=1
        else:
            number=_restart_attempt(cfg,directory)
            if number>1000:fail('INPUT_LIMIT','Background source sync attempt limit reached')
        attempt=directory/('attempt-'+str(number).zfill(6));publisher._private_state_directory(attempt)
        status={'project_id':cfg['project_id'],'task_id':old['task_id'],'attempt':number,'status':'queued','created_at':time.time()}
        _save_status(cfg,directory,attempt,status,True);atomic_json(directory/'current.json',{'attempt':number})
        try:_launch(cfg,directory,number,attempt)
        except Exception as error:
            latest=_read(attempt/'status.json')
            if latest.get('status') not in {'completed','failed'}:
                uncertain=isinstance(error,LaunchUncertain) or (attempt/'launch-intent.json').exists()
                if uncertain:
                    error=error if isinstance(error,LaunchUncertain) else LaunchUncertain(None)
                    latest.update(status='recovery_required',error=_error(error),worker_identity=error.identity)
                else:latest.update(status='failed',error=_error(error),finished_at=time.time())
                _save_status(cfg,directory,attempt,latest)
            if isinstance(error,LaunchUncertain):raise error from None
            raise
        return {**read_task(cfg,request_id),'worker_started':True,'idempotent_replay':False}


def worker(cfg,key,number):
    if not isinstance(key,str) or not KEY.fullmatch(key) or type(number) is not int or not 1<=number<=1000:
        fail('INVALID_ARGUMENT','Invalid fixed background task identity')
    directory=no_links(_private(cfg)/key);record=_task_record(cfg,directory)
    attempt=no_links(directory/('attempt-'+str(number).zfill(6)));state=_read(attempt/'status.json')
    try:
        current,_,_=_attempt(cfg,directory)
        if current!=number:fail('TASK_SUPERSEDED','A later source sync attempt owns this request')
        if record['config_revision']!=cfg['_config_revision']:fail('CONFIG_CHANGED','Project configuration changed before background execution')
        _config_unchanged(cfg,record['config_revision'])
        if record['code_sha256']!=code_identity():fail('CODE_CHANGED','Source synchronization code changed before background execution')
        require_admitted(cfg,'paper_source_sync');policy=source_sync.config(cfg)
        if sha(encoded(policy))!=record['policy_sha256']:fail('CONFIG_CHANGED','Project synchronization policy changed before execution')
        state.update(status='running',started_at=time.time(),worker_identity=process_identity(os.getpid()))
        _save_status(cfg,directory,attempt,state)
        completed=_completed_receipt(cfg,record['arguments'])
        if completed is None:
            with drive_deadline(policy['deadline_seconds']):result=source_sync.dispatch(cfg,record['arguments'])
            revalidated=True
        else:result=completed;revalidated=False
        if result.get('sync_complete') is not True:fail('DRIVE_SYNC_INCOMPLETE','Source engine did not produce a completed verified receipt')
        _config_unchanged(cfg,record['config_revision'])
        if record['code_sha256']!=code_identity():fail('CODE_CHANGED','Source synchronization code changed during background execution')
        state.update(status='completed',finished_at=time.time(),remote_revalidated=revalidated)
        _save_status(cfg,directory,attempt,state)
    except Exception as error:
        detail=_error(error)
        # Transport exceptions can embed capabilities in their message. Keep
        # useful stack locations while excluding raw exception text/locals.
        print(json.dumps({'error':detail,'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                         for frame in traceback.extract_tb(error.__traceback__)]}),file=sys.stderr)
        state.update(status='failed',finished_at=time.time(),error=detail,worker_identity=process_identity(os.getpid()))
        _save_status(cfg,directory,attempt,state)
    return state


def dispatch(cfg,args):
    # Preserve old plan/dry-run/import behavior and error contracts. Status is
    # local-only; never start a worker or perform Drive GETs just to poll.
    if args.get('action')=='sync' and args.get('dry_run',True) is False:return submit(cfg,args)
    if args.get('action')=='status' and args.get('request_id') is not None:
        _request_id(args['request_id'])
        if set(args)-{'project_id','action','request_id'}:fail('INVALID_ARGUMENT','Unsupported source status arguments')
        project(cfg,args.get('project_id'));value=read_task(cfg,args['request_id'])
        if value is not None:return value
    value=source_sync.dispatch(cfg,args)
    if args.get('action')=='status' and value.get('sync_complete') is True:
        return {**value,'asynchronous':False,'receipt_only':True,'remote_revalidated':False}
    return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--project-id',required=True)
    parser.add_argument('--task-key',required=True);parser.add_argument('--attempt',type=int,required=True);args=parser.parse_args()
    cfg=select_project(load_config(args.config),args.project_id);result=worker(cfg,args.task_key,args.attempt)
    print(json.dumps({'task_id':result.get('task_id'),'attempt':args.attempt,'status':result['status']}))


if __name__=='__main__':main()
