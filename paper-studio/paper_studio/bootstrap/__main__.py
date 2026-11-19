"""One-command, local-only Paper Editor initialization."""
import argparse
import json
from pathlib import Path
import sys
if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from paper_studio.bootstrap.workspace import BootstrapError, initialize


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--project-name')
    parser.add_argument('--paper-root')
    parser.add_argument('--manuscript-dir')
    parser.add_argument('--source', action='append', dest='sources')
    parser.add_argument('--tunnel-registry')
    parser.add_argument('--gateway-config')
    parser.add_argument('--target-venue')
    parser.add_argument('--max-files', type=int, default=10000)
    parser.add_argument('--max-hash-bytes', type=int, default=256 * 1024 * 1024)
    parser.add_argument('--library-root')
    parser.add_argument('--library-manifest-sha256')
    parser.add_argument('--library-release-id')
    parser.add_argument('--library-venue', choices=['ieee-journal', 'ieee-compsoc', 'generic'])
    parser.add_argument('--handoff-spec', help='Optional evidence-bearing first handoff JSON, with no manuscript overwrites')
    parser.add_argument('--expected-previous', help='absent for first handoff, or exact latest material_revision SHA256')
    try:
        options = vars(parser.parse_args(argv))
        handoff_spec = options.pop('handoff_spec'); expected_previous = options.pop('expected_previous')
        if bool(handoff_spec) != bool(expected_previous):
            raise BootstrapError('HANDOFF_PREDECESSOR_REQUIRED', '--handoff-spec and --expected-previous must be supplied together')
        handoff_input = None
        if handoff_spec:
            from paper_studio.bootstrap.handoff import validate_spec, SHA
            from paper_studio.bootstrap.workspace import _json, no_links
            handoff_input = validate_spec(_json(no_links(Path(handoff_spec))), options['project_id'])
            if expected_previous != 'absent' and not SHA.fullmatch(expected_previous):
                raise BootstrapError('HANDOFF_PREDECESSOR_REQUIRED', 'Use absent or the exact previous material_revision SHA256')
        library = {k: options.pop(k) for k in ('library_root', 'library_manifest_sha256',
                                               'library_release_id', 'library_venue')}
        if library['library_root']:
            from paper_studio.bootstrap.library_templates import initialize_with_library
            result = initialize_with_library(
                **options, library_root=library['library_root'],
                expected_library_manifest_sha256=library['library_manifest_sha256'],
                library_release_id=library['library_release_id'],
                library_venue=library['library_venue'])
        else:
            if any(library[k] for k in ('library_manifest_sha256', 'library_release_id', 'library_venue')):
                raise BootstrapError('LIBRARY_SOURCE_REQUIRED', 'Library options require an explicit local library root')
            result = initialize(**options)
        if handoff_spec:
            from paper_studio.bootstrap.handoff import create_handoff
            result['material_handoff'] = create_handoff(
                project_root=options['project_root'], paper_root=result['paper_writing_root'],
                project_id=options['project_id'], spec=handoff_input,
                expected_previous=expected_previous)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except BootstrapError as error:
        print(json.dumps({'ok': False, 'error': {'code': error.code, 'message': error.message}}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({'ok': False, 'error': {'code': 'BOOTSTRAP_FAILED', 'message': 'Local initialization failed; existing files were not intentionally replaced'}}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
