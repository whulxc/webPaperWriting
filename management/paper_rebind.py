"""CLI for the common project catalog and reviewed rebind transaction."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'paper-studio'))

from paper_studio.binding.configuration import capture, dump_manifest
from paper_studio.binding.transaction import apply, rollback, compute, load_profile, read
from paper_studio.binding import reconcile
from paper_studio.security.policy import no_links


def main(argv=None):
    parser = argparse.ArgumentParser(description='保留连接能力，加载项目档案，验证并可回滚换绑')
    sub = parser.add_subparsers(dest='action', required=True)
    cap = sub.add_parser('capture', help='将当前项目完整配置登记为新档案，不读取凭据内容')
    cap.add_argument('--config', required=True); cap.add_argument('--output', required=True)
    plan = sub.add_parser('plan', help='只读生成完整换绑计划')
    plan.add_argument('--profile', required=True); plan.add_argument('--project-id', required=True)
    plan.add_argument('--operation-id', required=True); plan.add_argument('--output', required=True)
    app = sub.add_parser('apply'); app.add_argument('--profile', required=True); app.add_argument('--plan', required=True)
    app.add_argument('--authorized', action='store_true')
    repair = sub.add_parser('reconcile-plan', help='保持当前绑定，审阅单项目Drive目录热修复')
    repair.add_argument('--profile', required=True); repair.add_argument('--candidate', required=True)
    repair.add_argument('--operation-id', required=True); repair.add_argument('--output', required=True)
    repair_apply = sub.add_parser('reconcile-apply')
    repair_apply.add_argument('--profile', required=True); repair_apply.add_argument('--plan', required=True)
    repair_apply.add_argument('--authorized', action='store_true')
    for action in ('reconcile-rollback', 'reconcile-recover'):
        command = sub.add_parser(action); command.add_argument('--record', required=True)
        command.add_argument('--authorized', action='store_true')
    for action in ('rollback', 'recover'):
        command = sub.add_parser(action); command.add_argument('--record', required=True)
        command.add_argument('--authorized', action='store_true')
    args = parser.parse_args(argv)
    if args.action in {'capture', 'plan', 'reconcile-plan'}:
        if args.action == 'capture':
            value = capture(json.loads(read(args.config))); raw = dump_manifest(value)
        elif args.action == 'plan':
            value, _, _, _, _ = compute(load_profile(args.profile), args.project_id, args.operation_id)
            raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        else:
            value, _, _, _, _ = reconcile.compute(load_profile(args.profile), args.candidate, args.operation_id)
            raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        path = no_links(Path(args.output))
        # Never overwrite a project catalog entry or reviewed plan implicitly.
        with path.open('xb') as stream:
            stream.write(raw)
        result = {'status': 'captured' if args.action == 'capture' else 'planned', 'path': str(path), 'production_changed': False}
    elif args.action == 'apply':
        result = apply(args.profile, args.plan, authorized=args.authorized)
    elif args.action == 'reconcile-apply':
        result = reconcile.apply(args.profile, args.plan, authorized=args.authorized)
    elif args.action in {'reconcile-rollback', 'reconcile-recover'}:
        result = reconcile.rollback(args.record, authorized=args.authorized, recover=args.action == 'reconcile-recover')
    else:
        result = rollback(args.record, authorized=args.authorized, recover=args.action == 'recover')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
