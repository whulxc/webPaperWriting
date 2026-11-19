"""Plan/apply one local empty-folder checkpoint; never upload to Drive."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))
from paper_studio.drive import folder_recovery
from paper_studio.security.policy import StudioError, load_config, select_project


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply'])
    for name in ['config', 'project-id', 'version', 'resume-build-id', 'folder-id', 'expected-manifest-sha256']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--authorized', action='store_true')
    args = vars(parser.parse_args(argv)); action = args.pop('action')
    config = args.pop('config'); project = args.pop('project_id')
    authorized = args.pop('authorized'); expected = args.pop('expected_plan_sha256')
    try:
        cfg = select_project(load_config(config), project)
        result = folder_recovery.prepare(cfg, **args) if action == 'plan' else folder_recovery.apply(
            cfg, **args, authorized=authorized, expected_plan_sha256=expected)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2)); return 0
    except StudioError as error:
        print(json.dumps({'ok': False, 'error': error.as_dict()}, ensure_ascii=False)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
