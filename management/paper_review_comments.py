"""Read or collect hash-bound Drive PDF comments through the common project API."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))
from paper_studio.gateway.main import Gateway
from paper_studio.security.policy import StudioError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--project-id', required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--version')
    selection.add_argument('--review-receipt-id')
    parser.add_argument('--action', choices=['plan', 'collect', 'status', 'apply'], default='plan')
    parser.add_argument('--request-id')
    parser.add_argument('--details', type=Path,
                        help='JSON with source_bindings, collection_request_id, '
                             'expected_snapshot_sha256, or confirmations from this review')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--authorized', action='store_true')
    args = parser.parse_args(argv)
    try:
        target = {'version':args.version} if args.version else {'review_receipt_id':args.review_receipt_id}
        request = {'project_id':args.project_id, **target, 'action':args.action,
                   'dry_run':not args.apply, 'authorized':args.authorized}
        if args.request_id:
            request['request_id'] = args.request_id
        if args.details:
            details = json.loads(args.details.read_bytes())
            allowed = {'source_bindings', 'collection_request_id', 'expected_snapshot_sha256', 'confirmations'}
            if not isinstance(details, dict) or not details.keys() <= allowed:
                parser.error('--details must contain only review binding or author confirmation fields')
            request.update(details)
        result = Gateway(args.config).call('paper_review_comments', request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StudioError as error:
        print(json.dumps({'error': error.as_dict()}, ensure_ascii=False))
        return 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
