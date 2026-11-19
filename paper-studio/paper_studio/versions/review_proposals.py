"""Protected, review-only proposals followed by one confirmed atomic revision.

Draft bytes, comments and previews live outside the target object database.
Only apply can create an index revision. A numeric revision is a working
revision, never a formal release. All file intake and exports are named.
"""
from __future__ import annotations

import base64
import contextlib
import difflib
import fcntl
import html
import importlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sys
import threading
import time

from paper_studio.security.policy import (StudioError, atomic_json, builds_root, encoded, fail,
    no_links, path_for, relpath, safe_read, safe_write_new, sha)
from paper_studio.versions import target_index as ix

SCHEMA = 'paper-studio.review-proposal.v1'
RENDER_VERSION = 'review-preview-v1'
MAX_ARTIFACT_BYTES = 128 * 1024**2
_ENGINE_LOCK = threading.RLock()


def need(value, code, message, **details):
    if not value:
        fail(code, message, **details)


def validate_config(cfg):
    policy = cfg.get('review_proposals')
    if policy is not None:
        need(isinstance(policy, dict) and set(policy) == {'enabled'}
             and type(policy['enabled']) is bool, 'CONFIG_INVALID', 'review_proposals requires only explicit enabled')


def _enabled(cfg):
    validate_config(cfg)
    need(cfg.get('review_proposals', {}).get('enabled') is True, 'REVIEW_NOT_CONFIGURED', 'Review proposals are not enabled for this project')
    ix._policy(cfg)


def _directory(cfg, proposal_id, create=False):
    _enabled(cfg);ix._label(proposal_id)
    root = no_links(Path(cfg['state_root']) / 'review-proposals')
    ix._private(Path(cfg['state_root']), create)
    ix._private(root, create)
    target = root / proposal_id
    ix._private(target, create)
    return target


@contextlib.contextmanager
def _lock(cfg, proposal_id, create=False):
    directory = _directory(cfg, proposal_id, create)
    path = no_links(directory / '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def _json(root, name):
    return json.loads(safe_read(root, name, 16 * 1024**2))


def _put(root, raw):
    digest = sha(raw);directory = root / 'objects';directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / digest
    if target.exists():
        need(safe_read(directory, digest, ix.MAX_BYTES) == raw, 'REVIEW_TAMPERED', 'Proposal object differs from its hash')
    else:
        safe_write_new(directory, digest, raw)
    return {'sha256': digest, 'bytes': len(raw)}


def _blob(root, identity):
    raw = safe_read(root / 'objects', identity['sha256'], ix.MAX_BYTES)
    need(len(raw) == identity['bytes'] and sha(raw) == identity['sha256'], 'REVIEW_TAMPERED', 'Proposal snapshot changed')
    return raw


def _selector(value):
    need(isinstance(value, dict) and {'role', 'language'} <= set(value)
         and not set(value) - {'role', 'language', 'figure_number', 'target_key'},
         'REVIEW_ARGUMENT', 'An exact role/language/figure/target selector is required')
    ix._key(value)
    return dict(value)


def _key(selector):
    return ix._key(selector)


def _text(raw):
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        fail('REVIEW_TEXT_ENCODING', 'Text proposals require actual UTF-8 source bytes')


def _anchor(value):
    need(isinstance(value, dict) and not set(value) - {'page', 'rect', 'quote'},
         'REVIEW_ARGUMENT', 'Anchor accepts only an explicit page, rectangle or quote')
    if 'page' in value:
        need(type(value['page']) is int and value['page'] >= 1, 'REVIEW_ARGUMENT', 'Anchor page is one-based')
    if 'rect' in value:
        rect = value['rect']
        need('page' in value and isinstance(rect, list) and len(rect) == 4
             and all(type(item) in {int, float} and math.isfinite(item) and item >= 0 for item in rect)
             and rect[0] < rect[2] and rect[1] < rect[3], 'REVIEW_ARGUMENT', 'Anchor rectangle needs a page and finite ordered coordinates')
    if 'quote' in value:
        need(isinstance(value['quote'], str) and value['quote'], 'REVIEW_ARGUMENT', 'Anchor quote must contain actual readable text')
    return value


def _capture_path(cfg, root, value):
    need(isinstance(value, dict) and set(value) == {'path', 'sha256'}, 'REVIEW_ARGUMENT', 'Candidate needs exact path and SHA')
    ix._hash(value['sha256']);path = relpath(value['path'])
    need(any(path.is_relative_to(ix._safe_intake_root(cfg, allowed))
             for allowed in ix._policy(cfg)['ingest_roots']), 'INDEX_INTAKE_DENIED', 'Candidate is outside the configured intake roots')
    path_for(cfg, value['path'])
    raw = safe_read(cfg['project_root'], value['path'], ix._policy(cfg).get('max_file_bytes', ix.MAX_BYTES))
    need(raw and sha(raw) == value['sha256'], 'REVISION_CONFLICT', 'Candidate changed after the supplied read identity')
    return {**_put(root, raw), 'path': value['path']}


def _load(root, cfg, proposal_id):
    raw = safe_read(root, 'manifest.json', 16 * 1024**2)
    manifest = json.loads(raw);identity = sha(raw)
    wrapper = _json(root, 'state.json');state = wrapper['state']
    need(wrapper['sha256'] == sha(encoded(state)) and state['proposal_sha256'] == identity
         and manifest['project_id'] == cfg['project_id'] and manifest['proposal_id'] == proposal_id,
         'REVIEW_TAMPERED', 'Proposal identity or review state changed')
    return manifest, state, identity, wrapper['sha256']


def _save_state(root, state):
    digest = sha(encoded(state));atomic_json(root / 'state.json', {'state': state, 'sha256': digest})
    return digest


def _applied(cfg, proposal_id):
    with ix._db(cfg) as (connection, _):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_proposal_apply'").fetchone()
        if not exists:
            return None
        row = connection.execute('SELECT document,digest FROM review_proposal_apply WHERE proposal_id=?', (proposal_id,)).fetchone()
        if row is None:
            return None
        need(sha(row['document'].encode()) == row['digest'], 'REVIEW_TAMPERED', 'Apply receipt changed')
        return json.loads(row['document'])


def _summary(cfg, manifest, state, proposal_sha, review_sha):
    result = {'schema': SCHEMA, 'project_id': cfg['project_id'], 'proposal_id': manifest['proposal_id'],
        'proposal_sha256': proposal_sha, 'review_sha256': review_sha, 'baseline': manifest['baseline'],
        'expected_head': manifest['expected_head'], 'stage': 'review_only', 'work_revision_created': False,
        'changes': [{key: change[key] for key in ('change_id', 'kind', 'selector', 'title', 'reason')}
                    | {'decision': state['decisions'][change['change_id']]} for change in manifest['changes']],
        'all_items_confirmed': all(row['confirmed'] and row['decision'] in {'accept', 'reject', 'adjust'}
                                   for row in state['decisions'].values()),
        'comments': state['events'], 'applied': _applied(cfg, manifest['proposal_id'])}
    if result['applied']:
        result['stage'] = 'applied' if result['applied']['work_revision_created'] else 'closed_without_changes'
    return result


def _capture_bundle(cfg, root, bundle, targets):
    if bundle is None:
        return None
    need(isinstance(bundle, dict) and {'main_paths', 'bindings', 'export_root'} <= set(bundle)
         and not set(bundle) - {'main_paths', 'inputs', 'bindings', 'export_root'},
         'REVIEW_ARGUMENT', 'build_bundle requires main_paths, inputs, bindings and export_root')
    need(set(bundle['main_paths']) == {'en', 'zh'}, 'REVIEW_ARGUMENT', 'Build bundle needs actual bilingual main paths')
    export = relpath(bundle['export_root']);_validate_export_root(cfg, export.as_posix())
    need(any(export.is_relative_to(ix._safe_intake_root(cfg, allowed)) for allowed in ix._policy(cfg)['ingest_roots']),
         'INDEX_INTAKE_DENIED', 'Review export root must be in an approved intake root')
    # Recover stable logical names from an already approved parent export.
    # The next mirror is never nested inside the previous physical mirror.
    logical = {};lineage_only = set();parent_apply = {}
    for language, main in bundle['main_paths'].items():
        prior = _target_record(cfg, main)
        if prior:
            receipt, document, _, apply_sha = prior
            need(document['export_root'] == export.as_posix(), 'REVIEW_EXPORT_DENIED', 'A review series must retain its approved export namespace')
            for item in document['files']:
                logical[item['path']] = item.get('logical_path', item['original_path'])
            lineage_only.add(receipt['build_export']['manifest_path'])
            parent_apply[language] = {'proposal_id': receipt['proposal_id'], 'apply_sha256': apply_sha}
    from paper_studio import execution
    plans = {language: execution.build_plan(cfg, main) for language, main in bundle['main_paths'].items()}
    named = bundle.get('inputs')
    if named is None:
        discovered = {}
        for plan in plans.values():
            for item in plan['files']:
                need(item['path'] not in discovered or discovered[item['path']] == item['sha256'],
                     'REVIEW_BUILD_INCOMPLETE', 'Bilingual plans disagree about a named input')
                discovered[item['path']] = item['sha256']
        named = [{'path': path, 'sha256': digest} for path, digest in discovered.items()]
    inputs = {}
    for row in named:
        need(isinstance(row, dict) and set(row) == {'path', 'sha256'}, 'REVIEW_ARGUMENT', 'Build input needs path and SHA')
        relative = relpath(row['path']).as_posix();ix._hash(row['sha256'])
        need(relative not in inputs and (not PurePosixPath(relative).is_relative_to(export)
             or relative in logical or relative in lineage_only),
             'REVIEW_ARGUMENT', 'Export root must not contain or overlap its baseline inputs')
        path_for(cfg, relative)
        raw = safe_read(cfg['project_root'], relative, ix.MAX_BYTES)
        need(sha(raw) == row['sha256'], 'REVISION_CONFLICT', 'Named build input changed', path=relative)
        inputs[relative] = _put(root, raw)
        need(sum(item['bytes'] for item in inputs.values()) <= cfg.get('limits', {}).get('max_snapshot_bytes', 32 * 1024**2),
             'INPUT_LIMIT', 'Frozen named build closure exceeds the configured snapshot byte budget')
    bindings = {}
    for row in bundle['bindings']:
        need(isinstance(row, dict) and set(row) == {'selector', 'path'}, 'REVIEW_ARGUMENT', 'Build binding needs selector and path')
        selector = _selector(row['selector']);key = _key(selector)
        need(key in targets and row['path'] in inputs and key not in bindings,
             'REVIEW_BUILD_BINDING', 'Each binding must identify a captured target and a named input')
        need(inputs[row['path']]['sha256'] == targets[key]['identity']['sha256'],
             'REVIEW_BUILD_BINDING', 'Named input differs from the actual baseline target')
        bindings[key] = {'selector': selector, 'path': row['path']}
    recipes = {};material = {}
    for language, main in bundle['main_paths'].items():
        need(main in inputs, 'REVIEW_BUILD_BINDING', 'Main source is absent from the frozen bundle')
        main_binding = bindings.get(_key({'role': 'manuscript_tex', 'language': language}))
        need(main_binding is not None and main_binding['path'] == main, 'REVIEW_BUILD_BINDING',
             'Each actual TeX entry must match its indexed manuscript_tex binding; body sections use section_tex')
        _, target = execution.target_for(cfg, main)
        plan = plans[language]
        for row in plan['files']:
            need(row['path'] in inputs and inputs[row['path']]['sha256'] == row['sha256'],
                 'REVIEW_BUILD_INCOMPLETE', 'Frozen input bundle omitted a real named build dependency', path=row['path'])
        prior = _target_record(cfg, main)
        recipes[language] = json.loads(json.dumps(prior[1]['recipes'][language] if prior else target))
        if prior:
            material.update(prior[1]['material'])
        if target.get('material_binding'):
            material[language] = {'binding': target['material_binding'],
                'files': execution.material_provenance(cfg, target['material_binding'])}
    need(all(binding['path'] != export.as_posix() for binding in bindings.values()),
         'REVIEW_EXPORT_DENIED', 'Cannot replace a baseline input with the export root')
    return {'main_paths': dict(bundle['main_paths']), 'inputs': inputs, 'bindings': bindings,
            'logical_paths': {path: logical.get(path, path) for path in inputs if path not in lineage_only},
            'lineage_only': sorted(lineage_only), 'parent_apply': parent_apply,
            'export_root': export.as_posix(), 'recipes': recipes, 'material': material}


def propose(cfg, *, proposal_id, baseline, expected_head, changes, request_id, build_bundle=None):
    _enabled(cfg);ix._label(request_id)
    need(isinstance(baseline, dict) and set(baseline) == {'version', 'revision'}, 'REVIEW_ARGUMENT', 'Exact current baseline is required')
    need(isinstance(changes, list) and changes, 'REVIEW_ARGUMENT', 'Proposal needs explicit changes')
    intent = {'baseline': baseline, 'expected_head': expected_head, 'changes': changes, 'build_bundle': build_bundle}
    need(len(changes) <= ix.MAX_CHANGES_PER_REQUEST and len(encoded(intent)) <= 256 * 1024,
         'REVIEW_LIMIT', 'Review control request exceeds 1024 changes or 256 KiB')
    intent_sha = sha(encoded(intent))
    with _lock(cfg, proposal_id, True) as root:
        if (root / 'manifest.json').exists():
            manifest, state, digest, review_sha = _load(root, cfg, proposal_id)
            need(manifest['intent_sha256'] == intent_sha and manifest['request_id'] == request_id,
                 'IDEMPOTENCY_CONFLICT', 'Proposal ID already identifies another draft')
            return {**_summary(cfg, manifest, state, digest, review_sha), 'idempotent_replay': True}
        targets = {};normalized = [];ids = set();ranges = {}
        with ix._db(cfg) as (connection, index_root):
            head = ix._head(connection, baseline['version'])
            need(head['head_sha256'] == expected_head and head['head_revision'] == baseline['revision'],
                 'REVIEW_BASE_CHANGED', 'Proposal must use the actual current working head')
            document, base_sha = ix._revision(connection, **baseline)
            selections = ix._selections(connection, document)
            def capture(selector):
                key = _key(selector)
                if key not in targets:
                    entry = selections.get(key)
                    need(entry is not None and entry['current_for_revision'], 'REVIEW_TARGET_MISSING', 'Baseline target is absent or stale')
                    raw = ix._object_bytes(connection, index_root, entry['object_id'])
                    targets[key] = {'selector': selector, 'identity': _put(root, raw), 'entry': entry}
                return key, targets[key]
            for item in changes:
                need(isinstance(item, dict) and {'change_id', 'kind', 'selector'} <= set(item), 'REVIEW_ARGUMENT', 'Each change needs ID, kind and selector')
                cid = item['change_id'];ix._label(cid);need(cid not in ids, 'REVIEW_ARGUMENT', 'Duplicate change ID');ids.add(cid)
                selector = _selector(item['selector']);key, target = capture(selector)
                row = {'change_id': cid, 'kind': item['kind'], 'selector': selector,
                       'title': item.get('title', cid), 'reason': item.get('reason', '')}
                if item.get('anchor') is not None:row['anchor'] = _anchor(item['anchor'])
                need(all(isinstance(row[key], str) for key in ('title', 'reason')), 'REVIEW_ARGUMENT', 'Change labels must be text')
                if item['kind'] == 'text':
                    before, after = item.get('before'), item.get('after')
                    need(isinstance(before, str) and before and isinstance(after, str), 'REVIEW_ARGUMENT', 'Text change needs exact nonempty before and an after string')
                    text = _text(_blob(root, target['identity']));matches = list(re.finditer(re.escape(before), text))
                    occurrence = item.get('occurrence')
                    need(matches and ((occurrence is None and len(matches) == 1)
                         or type(occurrence) is int and 1 <= occurrence <= len(matches)),
                         'REVIEW_TEXT_AMBIGUOUS', 'Before text is missing or needs an exact one-based occurrence')
                    match = matches[(occurrence or 1) - 1]
                    span = (match.start(), match.end())
                    need(not any(a < span[1] and span[0] < b for a, b in ranges.get(key, [])), 'REVIEW_OVERLAP', 'Text changes overlap on the baseline')
                    ranges.setdefault(key, []).append(span)
                    row.update(start=span[0], end=span[1], before=before, after=after)
                elif item['kind'] == 'figure':
                    need(selector['role'] == 'figure_pdf', 'REVIEW_ARGUMENT', 'Figure replacement must select a real figure PDF')
                    need(not any(other['kind'] == 'figure' and _key(other['selector']) == key for other in normalized), 'REVIEW_OVERLAP', 'Figure has multiple replacements')
                    candidate = _capture_path(cfg, root, item['candidate'])
                    need(_blob(root, candidate).startswith(b'%PDF-'), 'REVIEW_ARGUMENT', 'Replacement figure is not a PDF')
                    row['candidate'] = candidate
                    if item.get('source'):
                        source_selector = {**selector, 'role': 'figure_source'}
                        capture(source_selector)
                        row['source'] = _capture_path(cfg, root, item['source'])
                else:
                    fail('REVIEW_ARGUMENT', 'Unsupported change kind')
                normalized.append(row)
            if build_bundle:
                for binding in build_bundle['bindings']:
                    capture(_selector(binding['selector']))
            # Capture only real current PDFs. A stale PDF is not silently
            # presented as the baseline layout of changed manuscript sources.
            baseline_pdfs = {}
            for language in {'en', 'zh'}:
                selector = {'role': 'manuscript_pdf', 'language': language}
                entry = selections.get(_key(selector))
                if entry and entry['current_for_revision']:
                    _, captured = capture(selector)
                    baseline_pdfs[language] = captured['identity']
        bundle = _capture_bundle(cfg, root, build_bundle, targets)
        if bundle:
            need(all(_key(row['selector']) in bundle['bindings'] for row in normalized), 'REVIEW_BUILD_BINDING', 'Every proposed target needs an exact build-file binding')
        manifest = {'schema': SCHEMA, 'project_id': cfg['project_id'], 'proposal_id': proposal_id,
            'request_id': request_id, 'intent_sha256': intent_sha, 'baseline': baseline,
            'expected_head': expected_head, 'baseline_sha256': base_sha, 'targets': targets,
            'changes': normalized, 'build_bundle': bundle, 'baseline_pdfs': baseline_pdfs, 'created_at': time.time()}
        digest = sha(encoded(manifest))
        state = {'proposal_sha256': digest, 'sequence': 0, 'events': [],
            'decisions': {row['change_id']: {'decision': 'pending', 'confirmed': False, 'reviewer': None}
                          for row in normalized}}
        safe_write_new(root, 'manifest.json', encoded(manifest));review_sha = _save_state(root, state)
        return _summary(cfg, manifest, state, digest, review_sha)


def decide(cfg, *, proposal_id, change_id, expected_proposal_sha256, expected_review_sha256,
           decision, reviewer, request_id, comment='', confirmed=False, adjusted_after=None,
           adjusted_candidate=None, adjusted_source=None):
    ix._label(request_id)
    need(decision in {'accept', 'reject', 'adjust', 'comment'} and isinstance(reviewer, str) and reviewer.strip()
         and len(reviewer) <= 200 and isinstance(comment, str) and type(confirmed) is bool,
         'REVIEW_ARGUMENT', 'Decision needs a named reviewer and explicit confirmation state')
    intent = {key: value for key, value in locals().copy().items() if key not in {'cfg'}}
    intent_sha = sha(encoded(intent))
    with _lock(cfg, proposal_id) as root:
        request_path = root / ('decision-' + request_id + '.json')
        if request_path.exists():
            prior = _json(root, request_path.name)
            need(prior['intent_sha256'] == intent_sha, 'IDEMPOTENCY_CONFLICT', 'Decision request was reused with other values')
            return {**prior['result'], 'idempotent_replay': True}
        manifest, state, digest, review_sha = _load(root, cfg, proposal_id)
        replay = next((event for event in state['events'] if event.get('request_id') == request_id), None)
        if replay:
            need(replay['intent_sha256'] == intent_sha, 'IDEMPOTENCY_CONFLICT', 'Decision request was reused with other values')
            result = _summary(cfg, manifest, state, digest, review_sha)
            safe_write_new(root, request_path.name, encoded({'intent_sha256': intent_sha, 'result': result}))
            return {**result, 'idempotent_replay': True}
        need(digest == expected_proposal_sha256 and review_sha == expected_review_sha256,
             'REVIEW_CONFLICT', 'Proposal or decision state changed; reread before deciding')
        need(_applied(cfg, proposal_id) is None, 'REVIEW_ALREADY_APPLIED', 'Applied review decisions are immutable')
        need(not (root / 'export-pending.json').exists(), 'REVIEW_EXPORT_PENDING',
             'A confirmed export is pending recovery; resume its original apply request before changing decisions or create a new proposal')
        change = next((row for row in manifest['changes'] if row['change_id'] == change_id), None)
        need(change is not None, 'REVIEW_ARGUMENT', 'Unknown change ID')
        selected = dict(state['decisions'][change_id])
        if decision == 'accept':
            # Accept means the original proposal, not an earlier adjustment.
            for key in ('adjusted_after', 'adjusted_candidate', 'adjusted_source'):
                selected.pop(key, None)
        if decision == 'adjust':
            if change['kind'] == 'text':
                need(isinstance(adjusted_after, str), 'REVIEW_ARGUMENT', 'Adjusted text must be explicit')
                selected['adjusted_after'] = adjusted_after
            else:
                selected['adjusted_candidate'] = _capture_path(cfg, root, adjusted_candidate)
                need(_blob(root, selected['adjusted_candidate']).startswith(b'%PDF-'), 'REVIEW_ARGUMENT', 'Adjusted figure is not PDF')
                if adjusted_source:
                    selected['adjusted_source'] = _capture_path(cfg, root, adjusted_source)
        if decision != 'comment':
            selected.update(decision=decision, reviewer=reviewer, confirmed=confirmed)
            state['decisions'][change_id] = selected
        event = {'change_id': change_id, 'decision': decision, 'confirmed': confirmed,
                 'reviewer': reviewer, 'comment': comment, 'at': time.time(),
                 'request_id': request_id, 'intent_sha256': intent_sha,
                 'identity_claim': 'caller_attributed_explicit_review; not biometric authentication'}
        state['events'].append(event);state['sequence'] += 1
        review_sha = _save_state(root, state)
        result = _summary(cfg, manifest, state, digest, review_sha)
        safe_write_new(root, request_path.name, encoded({'intent_sha256': intent_sha, 'result': result}))
        return result


def _selected_text(root, manifest, state, key, view):
    text = _text(_blob(root, manifest['targets'][key]['identity']))
    replacements = []
    for change in manifest['changes']:
        if change['kind'] != 'text' or _key(change['selector']) != key:
            continue
        decision = state['decisions'][change['change_id']]
        if view == 'baseline' or view == 'selected' and not (decision['confirmed'] and decision['decision'] in {'accept', 'adjust'}):
            continue
        after = decision.get('adjusted_after', change['after'])
        replacements.append((change['start'], change['end'], after))
    for start, end, after in sorted(replacements, reverse=True):
        text = text[:start] + after + text[end:]
    return text


def get(cfg, *, proposal_id, part='summary', selector=None, view='proposed', offset=0, limit=65536,
        render_id=None, artifact_path=None):
    need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 262144,
         'REVIEW_ARGUMENT', 'Use a bounded nonnegative content range')
    with _lock(cfg, proposal_id) as root:
        manifest, state, digest, review_sha = _load(root, cfg, proposal_id)
        if part == 'summary':
            return _summary(cfg, manifest, state, digest, review_sha)
        if part == 'content':
            need(view in {'baseline', 'proposed', 'selected'}, 'REVIEW_ARGUMENT', 'Unknown text view')
            key = _key(_selector(selector));need(key in manifest['targets'], 'REVIEW_ARGUMENT', 'Unknown captured text target')
            text = _selected_text(root, manifest, state, key, view)
            need(offset <= len(text), 'REVIEW_ARGUMENT', 'Text offset exceeds character count')
            chunk = text[offset:offset + limit]
            return {'proposal_id': proposal_id, 'proposal_sha256': digest, 'review_sha256': review_sha,
                'selector': selector, 'view': view, 'text': chunk, 'offset_unit': 'unicode_characters',
                'offset': offset, 'total': len(text), 'sha256': sha(text.encode()),
                'next_offset': offset + len(chunk) if offset + len(chunk) < len(text) else None}
        if part == 'artifact':
            return _read_artifact(cfg, proposal_id, render_id, artifact_path, offset, limit)
        fail('REVIEW_ARGUMENT', 'Unsupported proposal read part')


def _table_exists(connection, name):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _export_owner(cfg, relative):
    try:
        with ix._db(cfg) as (connection, _):
            if not _table_exists(connection, 'review_export_roots'):
                return None
            row = connection.execute('SELECT nonce FROM review_export_roots WHERE path=?', (relative,)).fetchone()
            return row['nonce'] if row else None
    except StudioError as error:
        if error.code == 'INDEX_NOT_FOUND':
            return None
        raise


def is_protected_export(cfg, relative_path):
    """Exact ancestor keys only; never enumerate a directory or index history."""
    if not cfg.get('review_proposals', {}).get('enabled'):
        return False
    path = relpath(relative_path)
    try:
        with ix._db(cfg) as (connection, _):
            if not _table_exists(connection, 'review_export_roots'):
                return False
            for ancestor in (path, *path.parents):
                if ancestor.as_posix() == '.':
                    continue
                if connection.execute('SELECT 1 FROM review_export_roots WHERE path=?', (ancestor.as_posix(),)).fetchone():
                    return True
    except StudioError as error:
        if error.code == 'INDEX_NOT_FOUND':
            return False
        raise
    return False


def _validate_export_root(cfg, relative):
    path = relpath(relative)
    need(any(path.is_relative_to(ix._safe_intake_root(cfg, allowed)) for allowed in ix._policy(cfg)['ingest_roots']),
         'REVIEW_EXPORT_DENIED', 'Export root is outside the configured candidate intake')
    actual = no_links(Path(cfg['project_root']) / path)
    owner = _export_owner(cfg, relative)
    if actual.exists():
        need(owner is not None, 'REVIEW_EXPORT_DENIED', 'An existing baseline or user directory cannot become an export root')
        # This is a private trusted exporter. The ordinary public write guard
        # stays active for every registered export and all its descendants.
        path_for(cfg, (path.parent / '.review-export-scope-check').as_posix(), True)
    else:
        need(owner is None, 'REVIEW_EXPORT_CHANGED', 'A registered export root has been removed')
        path_for(cfg, relative, True)
    return actual, owner


def _numeric_next(revision, bump, major_authorized, major_reason):
    need(isinstance(revision, str) and re.fullmatch(r'[1-9][0-9]*\.[1-9][0-9]*', revision),
         'REVIEW_VERSION_POLICY', 'Apply requires an explicitly established numeric working revision')
    major, minor = map(int, revision.split('.'))
    need(bump in {'minor', 'major'}, 'REVIEW_VERSION_POLICY', 'Choose minor or explicitly authorized major promotion')
    if bump == 'major':
        need(major_authorized is True and isinstance(major_reason, str) and major_reason.strip(),
             'REVIEW_MAJOR_AUTH_REQUIRED', 'Major promotion needs explicit authorization and its reason')
        return f'{major + 1}.1'
    return f'{major}.{minor + 1}'


def _ensure_apply_tables(connection):
    # No executescript: it would implicitly commit the index transaction.
    connection.execute('CREATE TABLE IF NOT EXISTS review_proposal_apply(proposal_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, intent_sha TEXT NOT NULL, document TEXT NOT NULL, digest TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS review_export_roots(path TEXT PRIMARY KEY, nonce TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS review_apply_targets(tex_path TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, apply_sha TEXT NOT NULL, language TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS review_export_revisions(path TEXT, revision TEXT, proposal_id TEXT NOT NULL, apply_sha TEXT NOT NULL, PRIMARY KEY(path,revision))')


def _approved_files(root, manifest, state):
    if all(row['decision'] == 'reject' for row in state['decisions'].values()):
        return {}, {}, []
    bundle = manifest['build_bundle']
    need(bundle is not None, 'REVIEW_BUILD_BUNDLE_REQUIRED', 'Confirmed changes need a frozen named-input build export')
    files = {path: _blob(root, identity) for path, identity in bundle['inputs'].items()
             if path not in bundle.get('lineage_only', [])}
    changed = {};accepted = []
    for change in manifest['changes']:
        decision = state['decisions'][change['change_id']]
        if decision['decision'] == 'reject':
            continue
        key = _key(change['selector']);binding = bundle['bindings'].get(key)
        need(binding is not None, 'REVIEW_BUILD_BINDING', 'Confirmed target has no exact named build binding')
        if change['kind'] == 'text':
            raw = _selected_text(root, manifest, state, key, 'selected').encode()
            files[binding['path']] = raw
            changed[key] = {'selector': change['selector'], 'path': binding['path'], 'raw': raw}
        else:
            candidate = decision.get('adjusted_candidate', change['candidate'])
            raw = _blob(root, candidate);files[binding['path']] = raw
            figure_version = 'review-' + sha(raw)[:24]
            changed[key] = {'selector': change['selector'], 'path': binding['path'], 'raw': raw, 'figure_version': figure_version}
            source = decision.get('adjusted_source', change.get('source'))
            if source:
                selector = {**change['selector'], 'role': 'figure_source'};source_key = _key(selector)
                source_binding = bundle['bindings'].get(source_key)
                need(source_binding is not None, 'REVIEW_BUILD_BINDING', 'Editable figure source needs its own named binding')
                raw_source = _blob(root, source);files[source_binding['path']] = raw_source
                changed[source_key] = {'selector': selector, 'path': source_binding['path'], 'raw': raw_source, 'figure_version': figure_version}
        accepted.append(change['change_id'])
    # Do not create a new member when an accepted edit is byte-identical.
    changed = {key: row for key, row in changed.items()
               if sha(row['raw']) != manifest['targets'][key]['identity']['sha256']}
    return files, changed, accepted


def _export_plan(manifest, state, files, revision):
    bundle = manifest['build_bundle'];prefix = (PurePosixPath(bundle['export_root']) / revision).as_posix()
    logical = bundle['logical_paths']
    need(len(set(logical[path] for path in files)) == len(files), 'REVIEW_EXPORT_CONFLICT', 'Two named inputs map to the same logical file')
    records = [{'path': (PurePosixPath(prefix) / logical[path]).as_posix(), 'original_path': path,
                'logical_path': logical[path],
                'sha256': sha(raw), 'bytes': len(raw)} for path, raw in sorted(files.items())]
    return {'schema': SCHEMA + '.export', 'proposal_id': manifest['proposal_id'],
        'project_id': manifest['project_id'], 'proposal_sha256': sha(encoded(manifest)),
        'review_sha256': sha(encoded(state)), 'baseline': manifest['baseline'], 'revision': revision,
        'export_root': bundle['export_root'], 'root': prefix, 'files': records,
        'main_paths': {language: (PurePosixPath(prefix) / logical[main]).as_posix()
                       for language, main in bundle['main_paths'].items()},
        'recipes': bundle['recipes'], 'material': bundle['material'], 'parent_apply': bundle.get('parent_apply', {})}


def _verify_export(cfg, export):
    root = Path(cfg['project_root'])
    manifest_path = export['manifest_path']
    raw = safe_read(root, manifest_path, 16 * 1024**2)
    need(sha(raw) == export['manifest_sha256'], 'REVIEW_EXPORT_CHANGED', 'Frozen build export manifest changed')
    document = json.loads(raw)
    for item in document['files']:
        raw = safe_read(root, item['path'], ix.MAX_BYTES)
        need(len(raw) == item['bytes'] and sha(raw) == item['sha256'],
             'REVIEW_EXPORT_CHANGED', 'Approved build input changed', path=item['path'])
    from paper_studio import execution
    for proof in document['material'].values():
        need(execution.material_provenance(cfg, proof['binding']) == proof['files'],
             'REVIEW_EXPORT_CHANGED', 'Original material provenance changed')
    return document


def _materialize(cfg, root, manifest, state, files, revision, connection):
    bundle = manifest['build_bundle']
    base = no_links(Path(cfg['project_root']) / relpath(bundle['export_root']))
    owner_row = connection.execute('SELECT nonce FROM review_export_roots WHERE path=?', (bundle['export_root'],)).fetchone()
    owner = owner_row['nonce'] if owner_row else None
    pending_path = root / 'export-pending.json'
    plan = _export_plan(manifest, state, files, revision);plan_sha = sha(encoded(plan))
    if pending_path.exists():
        pending = _json(root, pending_path.name)
        need(pending['plan_sha256'] == plan_sha, 'REVIEW_EXPORT_CONFLICT', 'Another approved export intent occupies this proposal')
    else:
        _validate_export_root(cfg, bundle['export_root'])
        pending = {'plan_sha256': plan_sha, 'nonce': owner or secrets.token_hex(24)}
        safe_write_new(root, pending_path.name, encoded(pending))
    if owner is None:
        # A fresh root or this proposal's exact retained failed export only.
        if base.exists():
            # Only the exact retained proposal journal can resume an orphaned
            # filesystem export after the SQLite transaction rolled back.
            path_for(cfg, (PurePosixPath(bundle['export_root']).parent / '.review-export-scope-check').as_posix(), True)
            marker = _json(base, '.review-export-root.json')
            need(marker == {'project_id': cfg['project_id'], 'path': bundle['export_root'], 'nonce': pending['nonce']},
                 'REVIEW_EXPORT_CONFLICT', 'Existing export root lacks the retained approval identity')
        else:
            base.mkdir(parents=True, mode=0o700)
            safe_write_new(base, '.review-export-root.json', encoded({'project_id': cfg['project_id'],
                'path': bundle['export_root'], 'nonce': pending['nonce']}))
        connection.execute('INSERT INTO review_export_roots VALUES(?,?)', (bundle['export_root'], pending['nonce']))
    else:
        need(owner == pending['nonce'], 'REVIEW_EXPORT_CONFLICT', 'Export namespace ownership differs')
    directory = no_links(Path(cfg['project_root']) / plan['root'])
    if not directory.exists():directory.mkdir(mode=0o700)
    if (directory / 'EXPORT.json').exists():
        need(safe_read(directory, 'EXPORT.json', 16 * 1024**2) == encoded(plan),
             'REVIEW_EXPORT_CONFLICT', 'Existing revision is immutable')
    else:
        # Resume only explicit journal members; existing bytes must match.
        # This also covers a process crash between two named file writes.
        for item in plan['files']:
            relative = PurePosixPath(item['path']).relative_to(plan['root']).as_posix()
            actual = no_links(directory / relative)
            if actual.exists():
                need(safe_read(directory, relative, ix.MAX_BYTES) == files[item['original_path']],
                     'REVIEW_EXPORT_CONFLICT', 'Retained partial export input changed')
            else:safe_write_new(directory, relative, files[item['original_path']])
        safe_write_new(directory, 'EXPORT.json', encoded(plan))
    export = {'root': plan['root'], 'export_root': bundle['export_root'],
        'manifest_path': (PurePosixPath(plan['root']) / 'EXPORT.json').as_posix(),
        'manifest_sha256': plan_sha, 'main_paths': plan['main_paths'],
        'files': plan['files'], 'working_directory': plan['root']}
    _verify_export(cfg, export)
    # Freeze only paths we explicitly created; no recursive directory discovery.
    directories = {directory}
    for item in [*plan['files'], {'path': export['manifest_path']}]:
        path = Path(cfg['project_root']) / item['path'];path.chmod(0o444)
        cursor = path.parent
        while cursor.is_relative_to(directory):
            directories.add(cursor)
            if cursor == directory:break
            cursor = cursor.parent
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):path.chmod(0o555)
    return export


def apply(cfg, *, proposal_id, expected_proposal_sha256, expected_review_sha256, expected_head,
          request_id, authorized=False, bump='minor', major_authorized=False, major_reason=''):
    need(authorized is True, 'AUTHORIZATION_REQUIRED', 'Apply requires explicit authorization of the confirmed selections')
    ix._label(request_id)
    intent = {'proposal_id': proposal_id, 'proposal_sha256': expected_proposal_sha256,
        'review_sha256': expected_review_sha256, 'expected_head': expected_head, 'bump': bump,
        'major_authorized': major_authorized, 'major_reason': major_reason}
    intent_sha = sha(encoded(intent))
    with _lock(cfg, proposal_id) as root:
        prior = _applied(cfg, proposal_id)
        if prior:
            need(prior['request_id'] == request_id and prior['intent_sha256'] == intent_sha,
                 'REVIEW_ALREADY_APPLIED', 'Proposal has already been applied; reuse its original request')
            return {**prior, 'idempotent_replay': True}
        manifest, state, digest, review_sha = _load(root, cfg, proposal_id)
        need(digest == expected_proposal_sha256 and review_sha == expected_review_sha256,
             'REVIEW_CONFLICT', 'The displayed proposal or confirmed selections changed')
        need(expected_head == manifest['expected_head'], 'REVIEW_BASE_CHANGED', 'Apply must retain the exact displayed baseline head')
        need(all(row['confirmed'] and row['decision'] in {'accept', 'reject', 'adjust'}
                 for row in state['decisions'].values()), 'REVIEW_UNCONFIRMED', 'Every proposed item needs an explicit confirmed accept/reject/adjust decision')
        revision = _numeric_next(manifest['baseline']['revision'], bump, major_authorized, major_reason)
        files, changed, accepted = _approved_files(root, manifest, state)
        need(sum(map(len, files.values())) <= cfg.get('limits', {}).get('max_snapshot_bytes', 32 * 1024**2),
             'INPUT_LIMIT', 'Confirmed build closure exceeds the configured snapshot byte budget')
        with ix._db(cfg, True) as (connection, index_root):
            _ensure_apply_tables(connection)
            ix._expect_head(connection, manifest['baseline']['version'], expected_head)
            current, base_sha = ix._revision(connection, **manifest['baseline'])
            need(base_sha == manifest['baseline_sha256'], 'REVIEW_BASE_CHANGED', 'Baseline revision changed')
            export = None;index_result = None
            if changed:
                need(len(changed) <= ix.MAX_CHANGES_PER_REQUEST, 'REVIEW_LIMIT', 'Confirmed members exceed the 1024-change request budget')
                export = _materialize(cfg, root, manifest, state, files, revision, connection)
                members = [];captured = {}
                for row in changed.values():
                    path = (PurePosixPath(export['root']) / manifest['build_bundle']['logical_paths'][row['path']]).as_posix()
                    member = {**row['selector'], 'path': path, 'sha256': sha(row['raw'])}
                    if row.get('figure_version'):member['figure_version'] = row['figure_version']
                    members.append(member);captured[path] = row['raw']
                # Unchanged selections inherit their immutable index entries.
                # Native proof uses only the protected logical->physical map.
                index_result = ix.create_revision_in_transaction(cfg, connection, index_root,
                    version=manifest['baseline']['version'], revision=revision, baseline=manifest['baseline'],
                    members=members, expected_head=expected_head, request_id='review-' + sha(request_id.encode())[:40],
                    captured_bytes=captured)
            result = {'schema': SCHEMA + '.apply', 'project_id': cfg['project_id'], 'proposal_id': proposal_id,
                'request_id': request_id, 'intent_sha256': intent_sha, 'proposal_sha256': digest,
                'review_sha256': review_sha, 'baseline': manifest['baseline'], 'bump': bump,
                'major_authorized': major_authorized, 'major_reason': major_reason,
                'work_revision_created': bool(changed), 'formal_published': False,
                'revision': revision if changed else manifest['baseline']['revision'],
                'accepted_changes': accepted, 'index_result': index_result, 'build_export': export,
                'build_executed': False, 'created_at': time.time(), 'idempotent_replay': False}
            raw = encoded(result);apply_sha = sha(raw)
            connection.execute('INSERT INTO review_proposal_apply VALUES(?,?,?,?,?)',
                (proposal_id, request_id, intent_sha, raw.decode(), apply_sha))
            if export:
                connection.execute('INSERT INTO review_export_revisions VALUES(?,?,?,?)',
                    (export['export_root'], revision, proposal_id, apply_sha))
                for language, main in export['main_paths'].items():
                    connection.execute('INSERT INTO review_apply_targets VALUES(?,?,?,?)', (main, proposal_id, apply_sha, language))
        return result


def _target_record(cfg, tex_path):
    if not cfg.get('review_proposals', {}).get('enabled'):
        return None
    _enabled(cfg);relpath(tex_path)
    try:
        with ix._db(cfg) as (connection, _):
            if not _table_exists(connection, 'review_apply_targets'):
                return None
            row = connection.execute('SELECT proposal_id,apply_sha,language FROM review_apply_targets WHERE tex_path=?', (tex_path,)).fetchone()
            if row is None:return None
            saved = connection.execute('SELECT document,digest FROM review_proposal_apply WHERE proposal_id=?', (row['proposal_id'],)).fetchone()
            need(saved and saved['digest'] == row['apply_sha'] and sha(saved['document'].encode()) == saved['digest'],
                 'REVIEW_TAMPERED', 'Trusted apply binding changed')
            receipt = json.loads(saved['document'])
    except StudioError as error:
        if error.code == 'INDEX_NOT_FOUND':return None
        raise
    export = receipt['build_export'];document = _verify_export(cfg, export)
    proposal_root = _directory(cfg, receipt['proposal_id'])
    _, _, proposal_sha, review_sha = _load(proposal_root, cfg, receipt['proposal_id'])
    need(proposal_sha == receipt['proposal_sha256'] and review_sha == receipt['review_sha256'],
         'REVIEW_TAMPERED', 'Approved proposal or confirmed review state changed')
    need(document['main_paths'].get(row['language']) == tex_path and document['proposal_id'] == row['proposal_id'],
         'REVIEW_TAMPERED', 'Trusted main is not the approved exported main')
    return receipt, document, row['language'], row['apply_sha']


def trusted_target(cfg, tex_path):
    record = _target_record(cfg, tex_path)
    if record is None:return None
    receipt, document, language, apply_sha = record
    original = document['recipes'][language];prefix = PurePosixPath(document['root'])
    def rebase(path):return prefix.as_posix() if path == '.' else (prefix / relpath(path)).as_posix()
    target = {key: value for key, value in original.items() if key != 'material_binding'}
    target.update(tex_path=tex_path, working_directory=rebase(original.get('working_directory', '.')),
        tex_search_paths=[rebase(path) for path in original.get('tex_search_paths', [])],
        _review_binding={'proposal_id': receipt['proposal_id'], 'apply_sha256': apply_sha,
                         'export_manifest_sha256': receipt['build_export']['manifest_sha256']})
    return 'review-' + sha(tex_path.encode())[:24], target


def provenance_paths(cfg, tex_path):
    record = _target_record(cfg, tex_path)
    if record is None:return []
    receipt, document, _, _ = record
    return [item['path'] for item in document['files']] + [receipt['build_export']['manifest_path']]


def approved_build_mapping(cfg, tex_path, *, expected_binding, original_paths=None):
    """Only protected apply receipts authorize relocation of inherited objects."""
    record = _target_record(cfg, tex_path)
    need(record is not None and isinstance(expected_binding, dict), 'REVIEW_BUILD_BINDING', 'An exact protected review binding is required')
    receipt, document, _, apply_sha = record
    actual = {'proposal_id': receipt['proposal_id'], 'apply_sha256': apply_sha,
              'export_manifest_sha256': receipt['build_export']['manifest_sha256']}
    need(expected_binding == actual, 'REVIEW_BUILD_BINDING', 'Native job belongs to a different approved review export')
    direct = {};logical = {}
    for item in document['files']:
        direct[item['original_path']] = item['path'];direct[item['path']] = item['path']
        logical[item['logical_path']] = item['path']
        direct.setdefault(item['logical_path'], item['path'])
    if original_paths is None:
        return direct
    need(isinstance(original_paths, list), 'REVIEW_ARGUMENT', 'Original input paths must be explicit')
    result = {}
    namespace = PurePosixPath(document['export_root'])
    with ix._db(cfg) as (connection, _):
        for original in original_paths:
            path = relpath(original)
            if original in direct:
                result[original] = direct[original];continue
            need(path.is_relative_to(namespace), 'REVIEW_BUILD_BINDING', 'Input is absent from the approved build mapping', path=original)
            tail = path.relative_to(namespace)
            need(len(tail.parts) >= 2, 'REVIEW_BUILD_BINDING', 'Input is not an approved revision member')
            revision, named = tail.parts[0], PurePosixPath(*tail.parts[1:]).as_posix()
            old = connection.execute('SELECT proposal_id,apply_sha FROM review_export_revisions WHERE path=? AND revision=?',
                                     (namespace.as_posix(), revision)).fetchone()
            need(old and named in logical, 'REVIEW_BUILD_BINDING', 'Historical relocation lacks its protected export receipt')
            saved = connection.execute('SELECT document,digest FROM review_proposal_apply WHERE proposal_id=?', (old['proposal_id'],)).fetchone()
            need(saved and saved['digest'] == old['apply_sha'] and sha(saved['document'].encode()) == old['apply_sha'],
                 'REVIEW_TAMPERED', 'Historical export receipt changed')
            prior = _verify_export(cfg, json.loads(saved['document'])['build_export'])
            need(any(item['path'] == original and item['logical_path'] == named for item in prior['files']),
                 'REVIEW_BUILD_BINDING', 'Historical path was never an approved input')
            result[original] = logical[named]
    return result


def _engine(cfg):
    with _ENGINE_LOCK:
        addons = cfg.get('toolchain', {}).get('python_addons')
        if addons and addons not in sys.path:
            sys.path.insert(0, addons)
        try:
            return importlib.import_module('pymupdf')
        except ImportError:
            fail('PDF_BACKEND_UNAVAILABLE', 'The configured PyMuPDF backend is needed for actual review previews')


def _artifact_root(cfg, proposal_id, render_id):
    ix._label(proposal_id);ix._label(render_id)
    return no_links(builds_root(cfg) / 'review-proposals' / proposal_id / render_id)


def _read_artifact(cfg, proposal_id, render_id, artifact_path, offset, limit):
    need(render_id is not None and artifact_path is not None, 'REVIEW_ARGUMENT', 'Artifact requires exact render ID and path')
    root = _artifact_root(cfg, proposal_id, render_id)
    # The trusted catalog resides outside the editable project artifact tree.
    catalog = _json(_directory(cfg, proposal_id), 'render-' + render_id + '.json')
    item = next((item for item in catalog['artifacts'] if item['path'] == artifact_path), None)
    need(item is not None, 'REVIEW_ARTIFACT_MISSING', 'Only this render catalog member may be read')
    raw = safe_read(root, artifact_path, MAX_ARTIFACT_BYTES)
    need(len(raw) == item['bytes'] and sha(raw) == item['sha256'], 'REVIEW_TAMPERED', 'Rendered artifact changed')
    need(offset <= len(raw), 'REVIEW_ARGUMENT', 'Artifact offset exceeds byte count')
    chunk = raw[offset:offset + min(limit, 131072)]
    return {'proposal_id': proposal_id, 'render_id': render_id, 'path': artifact_path,
        'mime_type': item['mime_type'], 'sha256': item['sha256'], 'total': len(raw),
        'offset': offset, 'offset_unit': 'bytes', 'data_base64': base64.b64encode(chunk).decode(),
        'chunk_sha256': sha(chunk), 'next_offset': offset + len(chunk) if offset + len(chunk) < len(raw) else None}


def _diff_markup(before, after):
    parts = []
    # Character-level diff retains precise selectable source, including TeX.
    for tag, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == 'equal':parts.append(html.escape(before[a:b]))
        else:
            if tag in {'delete', 'replace'}:parts.append('<del>' + html.escape(before[a:b]) + '</del>')
            if tag in {'insert', 'replace'}:parts.append('<ins>' + html.escape(after[c:d]) + '</ins>')
    return ''.join(parts)


def render(cfg, *, proposal_id, format='both', expected_proposal_sha256=None, expected_review_sha256=None):
    need(format in {'html', 'pdf', 'both'}, 'REVIEW_ARGUMENT', 'Preview format must be html, pdf or both')
    with _lock(cfg, proposal_id) as root:
        manifest, state, digest, review_sha = _load(root, cfg, proposal_id)
        need(expected_proposal_sha256 in {None, digest} and expected_review_sha256 in {None, review_sha},
             'REVIEW_CONFLICT', 'Preview request refers to a changed review state')
        render_id = sha(encoded([RENDER_VERSION, digest, review_sha, format]))[:32]
        catalog_path = root / ('render-' + render_id + '.json')
        if catalog_path.exists():
            result = _json(root, catalog_path.name)
            for item in result['artifacts']:_read_artifact(cfg, proposal_id, render_id, item['path'], 0, 1)
            return {**result, 'idempotent_replay': True}
        output = _artifact_root(cfg, proposal_id, render_id);output.mkdir(parents=True, exist_ok=True)
        engine = _engine(cfg);assets = {};sections = []
        text_keys = set()
        for change in manifest['changes']:
            decision = state['decisions'][change['change_id']]
            label = f"{change['change_id']} · {decision['decision']} · " + ('已确认' if decision['confirmed'] else '待确认')
            header = '<h2>' + html.escape(change['title']) + '</h2><p>' + html.escape(label) + '</p>'
            header += '<p>' + html.escape(change['reason']) + '</p>'
            key = _key(change['selector'])
            if change['kind'] == 'text':
                before = _text(_blob(root, manifest['targets'][key]['identity']))
                start = before.rfind('\n\n', 0, change['start']) + 2
                if start == 1:start = 0
                stop = before.find('\n\n', change['end'])
                if stop == -1:stop = len(before)
                context = before[start:stop]
                replacement = decision.get('adjusted_after', change['after'])
                after = before[start:change['start']] + replacement + before[change['end']:stop]
                content = header + '<p>该条修改所在的实际原段落；删除线为原文，绿色高亮为提议文字。</p>'
                content += '<div class="source">' + _diff_markup(context, after) + '</div>'
                if key not in text_keys:
                    selected = _selected_text(root, manifest, state, key, 'selected')
                    content += '<details><summary>当前原文（完整可选取）</summary><pre>' + html.escape(before) + '</pre></details>'
                    content += '<details><summary>已确认采用的正文源</summary><pre>' + html.escape(selected) + '</pre></details>'
                    text_keys.add(key)
                sections.append(content)
            else:
                original = _blob(root, manifest['targets'][key]['identity'])
                candidate = _blob(root, decision.get('adjusted_candidate', change['candidate']))
                pictures = []
                for kind, raw in [('old', original), ('new', candidate)]:
                    pdf_name = change['change_id'] + '-' + kind + '.pdf';assets[pdf_name] = (raw, 'application/pdf')
                    with engine.open(stream=raw, filetype='pdf') as doc:
                        need(len(doc) > 0, 'REVIEW_ARGUMENT', 'Figure PDF has no pages')
                        page = doc[0]
                        if kind == 'old':
                            rect = page.rect
                            page.draw_line(rect.tl, rect.br, color=(0.8, 0.08, 0.08), width=3, overlay=True)
                            page.draw_line(rect.tr, rect.bl, color=(0.8, 0.08, 0.08), width=3, overlay=True)
                        png = page.get_pixmap(matrix=engine.Matrix(1.5, 1.5), alpha=False).tobytes('png')
                    name = change['change_id'] + '-' + kind + '.png';assets[name] = (png, 'image/png')
                    pictures.append('<figure><img src="' + name + '"><figcaption>' + ('原图（划去）' if kind == 'old' else '提议新图')
                        + ' · <a href="' + pdf_name + '">原始矢量 PDF</a></figcaption></figure>')
                sections.append(header + '<div class="figures">' + ''.join(pictures) + '</div>')
        comments = ''.join('<li>' + html.escape(event['change_id'] + ' · ' + event['reviewer'] + ': ' + event['comment']) + '</li>' for event in state['events'])
        style = '''body{font-family: sans-serif;color:#172b40;background:#f5f3ee;max-width:1080px;margin:auto;padding:24px;line-height:1.6}h1,h2{font-family:serif}section{background:white;padding:24px;margin:18px 0;border:1px solid #d9dfdf;border-radius:8px}h2{font-size:20px}pre,.source{white-space:pre-wrap;overflow-wrap:anywhere;font-family:monospace;font-size:12px}del{background:#ffe0db;color:#8a2424;text-decoration:line-through}ins{background:#dcf3da;color:#174b27;text-decoration:none}.figures{display:flex;gap:14px}figure{margin:0;flex:1}img{max-width:100%;height:auto}a{color:#126d85}@media(max-width:600px){body{padding:10px}section{padding:14px}.figures{display:block}figure{margin-bottom:20px}}'''
        document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>审阅草案</title><style>' + style + '</style><body>'
        document += '<h1>审阅草案 · ' + html.escape(proposal_id) + '</h1><p>基线 ' + html.escape(str(manifest['baseline'])) + '；本展示不创建工作版本或正式发布。</p><p>展示范围：涉及文字目标的完整捕获源与更换图件；不是整稿排版校样。图件展示第一页，原始矢量PDF另附。</p>'
        document += ''.join('<section>' + section + '</section>' for section in sections) + '<section><h2>逐项人工意见</h2><ul>' + comments + '</ul></section><p>冻结提案SHA：' + digest + '；审阅SHA：' + review_sha + '</p></body></html>'
        assets['preview.html'] = (document.encode(), 'text/html')
        for name, (raw, _) in assets.items():
            target = output / name
            if target.exists():need(safe_read(output, name, ix.MAX_BYTES) == raw, 'REVIEW_TAMPERED', 'Existing preview differs')
            else:safe_write_new(output, name, raw)
        if format in {'pdf', 'both'}:
            # MuPDF HTML layout embeds actual local figure pixels; no CDN/network.
            pdf = engine.Story(html=document, user_css=style, archive=engine.Archive(str(output)))
            pdf_path = output / 'preview.pdf'
            if pdf_path.exists():pdf_path.unlink()  # incomplete unregistered render only
            writer = engine.DocumentWriter(str(pdf_path))
            more = True
            while more:
                device = writer.begin_page(engine.Rect(0, 0, 595, 842))
                more, _ = pdf.place(engine.Rect(36, 36, 559, 806));pdf.draw(device);writer.end_page()
            writer.close()
            assets['preview.pdf'] = (safe_read(output, 'preview.pdf', ix.MAX_BYTES), 'application/pdf')
        layout_evidence = {};layout_assets = {}
        if format in {'pdf', 'both'}:
            from paper_studio.versions.review_pdf_overlay import render_baseline_pdf
            for language, identity in manifest.get('baseline_pdfs', {}).items():
                entries = []
                for change in manifest['changes']:
                    if change['selector']['language'] not in {language, 'shared'}:continue
                    selected = state['decisions'][change['change_id']]
                    item = {'change_id': change['change_id'], 'kind': change['kind'],
                            'anchor': change.get('anchor', {}), 'reason': change['reason']}
                    if change['kind'] == 'text':
                        item.update(old_text=change['before'], new_text=selected.get('adjusted_after', change['after']))
                    else:
                        candidate = selected.get('adjusted_candidate', change['candidate'])
                        item.update(replacement_pdf=_blob(root, candidate), replacement_sha256=candidate['sha256'])
                    entries.append(item)
                if not entries:continue
                raw, evidence = render_baseline_pdf(_blob(root, identity), entries,
                    expected_sha256=identity['sha256'], language=language, engine=engine)
                layout_assets['preview_' + language + '.pdf'] = (raw, 'application/pdf')
                layout_evidence[language] = evidence
            if layout_assets:
                # Keep the complete source comparison, while the single-click
                # default preserves the real baseline paper's layout.
                assets['source-diff.pdf'] = assets['preview.pdf']
                preferred = 'preview_zh.pdf' if 'preview_zh.pdf' in layout_assets else 'preview_en.pdf'
                assets['preview.pdf'] = layout_assets[preferred]
                assets.update(layout_assets)
                for name in ['source-diff.pdf', 'preview.pdf', *layout_assets]:
                    raw, _ = assets[name]
                    if name == 'preview.pdf':
                        # The initial comparison PDF was this unregistered
                        # render's own file; replace it before catalog freeze.
                        (output / name).unlink()
                    safe_write_new(output, name, raw)
        artifacts = [{'path': name, 'absolute_path': str(output / name), 'mime_type': mime,
                      'bytes': len(raw), 'sha256': sha(raw),
                      'uri': 'paper-review://' + cfg['project_id'] + '/' + proposal_id + '/' + render_id + '/' + name}
                     for name, (raw, mime) in assets.items()]
        result = {'proposal_id': proposal_id, 'proposal_sha256': digest, 'review_sha256': review_sha,
            'render_id': render_id, 'stage': 'review_only', 'work_revision_created': False,
            'scope': 'baseline layout plus explicit revision sheets where a trusted current PDF exists; complete captured changed text sources and original/replacement figure PDFs in HTML',
            'presentation': 'baseline_pdf_with_revision_sheets' if layout_assets else 'source_diff_only',
            'default_preview': 'preview.pdf' if format != 'html' else 'preview.html',
            'baseline_layout': layout_evidence,
            'artifacts': artifacts, 'idempotent_replay': False}
        safe_write_new(root, catalog_path.name, encoded(result))
        return result


def dispatch(cfg, args):
    need(isinstance(args, dict), 'REVIEW_ARGUMENT', 'Review operation arguments must be an object')
    values = dict(args);action = values.pop('action', None)
    functions = {'propose': propose, 'get': get, 'render': render, 'decide': decide, 'apply': apply}
    need(action in functions, 'REVIEW_ARGUMENT', 'Unknown review action')
    try:return functions[action](cfg, **values)
    except TypeError as error:fail('REVIEW_ARGUMENT', 'Invalid review operation fields', detail=str(error))
