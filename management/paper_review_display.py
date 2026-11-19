"""Show one review PDF in an existing private Drive folder. No manuscript version is required."""
import argparse
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))

from paper_studio.drive import target_display
from paper_studio.security.policy import (
    StudioError, encoded, fail, load_config, locked, path_for, safe_read, select_project, sha,
)


def show(cfg, *, pdf, build_id, expected_sha256, language, project_folder_id, parent_id,
         name=None, verify_media=False, drive=None, source_binding=None):
    """Use an exact project PDF or the primary output of one exact native build."""
    if bool(pdf) == bool(build_id):
        fail('INVALID_ARGUMENT', 'Select one review PDF or one build ID')
    if build_id:
        path, item = target_display.review_build_pdf(cfg, build_id)
        digest, size = item['sha256'], item['bytes']
    else:
        path = path_for(cfg, pdf)
        raw = safe_read(cfg['project_root'], pdf, target_display.MAX_BYTES)
        if not raw.startswith(b'%PDF-') or path.suffix.lower() != '.pdf':
            fail('INVALID_ARGUMENT', 'Select a PDF file')
        digest, size = sha(raw), len(raw)
    if digest != expected_sha256:
        fail('REVISION_CONFLICT', 'The selected PDF differs from the expected SHA256')
    display_name = name or path.name
    if not display_name.lower().endswith('.pdf'):
        fail('INVALID_ARGUMENT', 'The review display name must end in .pdf')
    identity = {'role':'review', 'language':language, 'sha256':digest, 'bytes':size}
    if build_id:
        identity['build_id'] = build_id
    if source_binding is not None:
        identity['review_source'] = source_binding
    args = dict(project_id=cfg['project_id'], project_folder_id=project_folder_id, parent_id=parent_id,
                name=display_name, sha256=digest, bytes=size, role='review', language=language,
                source_identity=identity)
    key = sha(encoded({'source':str(path), **args}))
    journal = Path(cfg['state_root']) / 'review-display' / (key + '.json')
    receipt = target_display.publish_exact(cfg, **args, source_path=path, journal_path=journal,
                                          authorized=True, drive=drive)
    if source_binding is not None:
        from paper_studio.versions.review_deliveries import register
        register(cfg, key)
    if verify_media:
        return target_display.download_exact(cfg, **args, file_id=receipt['file_id'], drive=drive)
    return {**receipt, 'review_receipt_id': key}


def show_review_build(cfg, review):
    """Display one current controlled TeX PDF without making a formal version."""
    with locked(cfg, 'lifecycle') if cfg.get('version_management') else contextlib.nullcontext():
        with locked(cfg):
            source = target_display.review_build_source(cfg, review['build_id'], review['language'], current=True)
            return show(cfg, pdf=None, build_id=review['build_id'], expected_sha256=review['sha256'],
                        language=review['language'], project_folder_id=review['project_folder_id'],
                        parent_id=review['parent_id'], name=review.get('name'),
                        source_binding=source if source['kind'] == 'controlled_tex_review' else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--project-id', required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--pdf', help='Project-relative path to a locally compiled review PDF')
    source.add_argument('--build-id', help='Use the primary PDF where the project actually stored it')
    parser.add_argument('--sha256', required=True, dest='expected_sha256')
    parser.add_argument('--language', required=True, choices=('zh','en','shared'))
    parser.add_argument('--project-folder-id', required=True)
    parser.add_argument('--parent-id', required=True, help='Existing Drive review folder ID')
    parser.add_argument('--name', help='Optional PDF name for the shared review link')
    parser.add_argument('--verify-media', action='store_true', help='Also download and hash the displayed PDF')
    values = vars(parser.parse_args(argv))
    config, project_id = values.pop('config'), values.pop('project_id')
    try:
        initial = load_config(config)
        with locked(initial, 'routing'):
            current = load_config(config)
            if current['_config_revision'] != initial['_config_revision']:
                fail('CONFIG_CHANGED', 'Binding changed before single-PDF delivery')
            if (Path(current['state_root']) / 'routing-unavailable.json').exists():
                fail('BINDING_UNAVAILABLE', 'Project binding recovery is incomplete')
            result = show(select_project(current, project_id), **values)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, StudioError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
