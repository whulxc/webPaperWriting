"""Explicit local import / readonly exact history query. No implicit discovery."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'paper-studio'))
from paper_studio.security.policy import StudioError,load_config,select_project
from paper_studio.versions.history_objects import import_rows
from paper_studio.gateway.history_lookup import read_locator


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--project-id',required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    imp=sub.add_parser('import-jsonl');imp.add_argument('--input',required=True);imp.add_argument('--request-id',required=True);imp.add_argument('--authorized',action='store_true')
    query=sub.add_parser('get');query.add_argument('--uri',required=True)
    args=parser.parse_args(argv)
    try:
        cfg=select_project(load_config(args.config),args.project_id)
        if args.action=='get':result=read_locator(cfg,{'items':[{'path':args.uri}]})
        else:
            if not args.authorized:raise StudioError('AUTHORIZATION_REQUIRED','Local history import requires explicit --authorized; lookup never imports')
            path=Path(args.input)
            if not path.is_absolute() or not path.is_file() or path.stat().st_size>256*1024**2:raise StudioError('HISTORY_ARGUMENT','A bounded absolute JSONL input is required')
            rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
            result=import_rows(cfg,rows,request_id=args.request_id)
        print(json.dumps(result,ensure_ascii=False));return 0
    except StudioError as error:
        print(json.dumps({'error':error.as_dict()},ensure_ascii=False));return 2
    except (OSError,ValueError,RecursionError):
        print(json.dumps({'error':{'code':'HISTORY_INPUT_INVALID','message':'History input could not be parsed safely'}}));return 2

if __name__=='__main__':raise SystemExit(main())
