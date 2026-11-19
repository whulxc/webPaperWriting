"""Local authorization for byte-preserving, same-project handoff relocation."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))
from paper_studio.bootstrap.handoff import plan_handoff_import, import_handoffs
from paper_studio.bootstrap.workspace import BootstrapError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply'])
    for name in ['source-project-root', 'source-paper-root', 'target-project-root',
                 'target-paper-root', 'project-id', 'expected-index-sha256']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--authorized', action='store_true')
    args = vars(parser.parse_args(argv))
    action = args.pop('action'); authorized = args.pop('authorized')
    expected = args.pop('expected_plan_sha256')
    try:
        result = (plan_handoff_import(**args) if action == 'plan' else
                  import_handoffs(**args, authorized=authorized, expected_plan_sha256=expected))
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2))
        return 0
    except BootstrapError as error:
        print(json.dumps({'ok': False, 'error': {'code': error.code, 'message': error.message}}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({'ok': False, 'error': {'code': 'HANDOFF_IMPORT_FAILED',
            'message': 'Local migration did not complete; preserve files and inspect the local source and target evidence'}}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
