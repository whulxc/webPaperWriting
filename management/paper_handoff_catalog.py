"""Explicit local material metadata compatibility and exact handoff lookup.

Enabling parses the legacy JSON metadata once; it neither verifies nor migrates
all historical pack payloads. Lookup never initializes, migrates or scans it.
"""
import argparse
import json

from paper_studio.bootstrap.handoff import (enable_handoff_catalog, lookup_handoff,
                                           plan_handoff_catalog, plan_handoff_incremental_import,
                                           import_handoff_incremental)
from paper_studio.bootstrap.workspace import BootstrapError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'enable', 'lookup', 'import-plan', 'import-apply'])
    parser.add_argument('--paper-root', '--target-paper-root', dest='paper_root', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--expected-index-sha256')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--authorized', action='store_true')
    parser.add_argument('--change-id')
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--source-project-root')
    parser.add_argument('--source-paper-root')
    parser.add_argument('--target-project-root')
    parser.add_argument('--expected-target-previous')
    args = parser.parse_args(argv)
    try:
        if args.action in {'import-plan', 'import-apply'}:
            for field in ('source_project_root', 'source_paper_root', 'target_project_root',
                          'change_id', 'manifest_sha256', 'expected_target_previous'):
                if getattr(args, field) is None:
                    parser.error('--' + field.replace('_', '-') + ' is required for incremental import')
            values = {field: getattr(args, field) for field in ('source_project_root', 'source_paper_root',
                'target_project_root', 'project_id', 'change_id', 'manifest_sha256', 'expected_target_previous')}
            values['target_paper_root'] = args.paper_root
            result = (plan_handoff_incremental_import(**values) if args.action == 'import-plan' else
                import_handoff_incremental(**values, authorized=args.authorized,
                    expected_plan_sha256=args.expected_plan_sha256))
        elif args.action == 'plan':
            result = plan_handoff_catalog(args.paper_root, args.project_id)
        elif args.action == 'enable':
            result = enable_handoff_catalog(args.paper_root, args.project_id,
                expected_index_sha256=args.expected_index_sha256,
                expected_plan_sha256=args.expected_plan_sha256, authorized=args.authorized)
        else:
            result = lookup_handoff(args.paper_root, args.project_id,
                change_id=args.change_id, manifest_sha256=args.manifest_sha256)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except BootstrapError as error:
        print(json.dumps({'ok': False, 'error': {'code': error.code, 'message': error.message}}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
