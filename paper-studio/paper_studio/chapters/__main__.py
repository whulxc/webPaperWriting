"""Local CLI for the same section and delivery operations exposed through MCP."""
import argparse
import json
from pathlib import Path
from paper_studio.chapters.workflow import dispatch
from paper_studio.chapters.delivery import receive
from paper_studio.provider.jobs import artifact
from paper_studio.security.policy import load_config, select_project


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--project-id', required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    operation = actions.add_parser('section')
    operation.add_argument('--request', required=True)
    transfer = actions.add_parser('receive')
    transfer.add_argument('--build-id', required=True)
    transfer.add_argument('--path', required=True)
    transfer.add_argument('--destination', required=True)
    transfer.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    cfg = select_project(load_config(args.config), args.project_id)
    if args.action == 'section':
        result = dispatch(cfg, json.loads(Path(args.request).read_text()))
    else:
        def call(name, value):
            value = dict(value); value.pop('project_id')
            return artifact(cfg, **value)
        result = receive(call, project_id=args.project_id, build_id=args.build_id,
            path=args.path, destination=args.destination, expected_sha256=args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':main()
