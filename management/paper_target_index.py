"""Local management for the protected target index; never a Chat command runner.

python -m management.paper_target_index --config /ABS/instance.json \
  --project-id demo --spec /ABS/seed.json seed

The JSON spec has the same keyword arguments as the corresponding backend API.
Formal publication additionally requires both explicit command-line acknowledgments.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))

from paper_studio.security.policy import StudioError, fail, load_config, locked, select_project
from paper_studio.versions import target_index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--spec', required=True, help='Bounded JSON keyword arguments, no credentials')
    parser.add_argument('--authorized', action='store_true')
    parser.add_argument('--user-requested', action='store_true')
    parser.add_argument('action', choices=['seed', 'revise', 'get', 'head', 'register-drive', 'register-build', 'verify-release', 'publish'])
    args = parser.parse_args(argv)
    try:
        path = Path(args.spec)
        if path.stat().st_size > 256 * 1024:
            parser.error('spec exceeds256KiB')
        spec = json.loads(path.read_bytes())
        if (not isinstance(spec, dict) or any(k.startswith('_') for k in spec)
                or {'cfg', 'drive', 'authorized', 'user_requested'} & set(spec)):
            parser.error('spec must contain only API keyword arguments')
        method = {'seed': 'seed_revision', 'revise': 'create_revision',
                  'get': 'resolve', 'head': 'get_head', 'register-drive': 'register_drive_receipt',
                  'register-build': 'register_build_proof', 'verify-release': 'verify_release', 'publish': 'publish'}[args.action]
        action = getattr(target_index, method)
        if args.action == 'publish':
            spec.update(authorized=args.authorized, user_requested=args.user_requested)
        elif args.authorized or args.user_requested:
            parser.error('publication acknowledgments apply only to publish')
        initial = load_config(args.config)
        with locked(initial, 'routing'):
            current = load_config(args.config)
            if (initial.get('_config_revision') != current.get('_config_revision')
                    or initial['state_root'] != current['state_root']):
                fail('CONFIG_CHANGED', 'The project binding changed while waiting for its routing lock')
            if (Path(current['state_root']) / 'routing-unavailable.json').exists():
                fail('BINDING_UNAVAILABLE', 'Project binding recovery is incomplete')
            cfg = select_project(current, args.project_id)
            result = action(cfg, **spec)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except StudioError as error:
        print(json.dumps({'error': {'code': error.code, 'message': error.message, 'details': error.details}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError) as error:
        print(json.dumps({'error': {'code': 'INDEX_CLI_ARGUMENT', 'message': str(error)}},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
