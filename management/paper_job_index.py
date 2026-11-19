"""Explicitly import an existing project's retained job metadata once."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))

from paper_studio.provider.job_index import import_history
from paper_studio.security.policy import StudioError, fail, load_config, locked, select_project


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('action', choices=['import'])
    parser.add_argument('--authorized', action='store_true')
    parser.add_argument('--reconcile', action='store_true', help='Recheck all retained records after any unindexed writer; keep a prior index backup')
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        with locked(cfg, 'routing'):
            current = load_config(args.config)
            if current['_config_revision'] != cfg['_config_revision']:
                fail('CONFIG_CHANGED', 'Binding changed before history import')
            result = import_history(select_project(current, args.project_id), authorized=args.authorized,
                                    reconcile=args.reconcile)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (StudioError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
