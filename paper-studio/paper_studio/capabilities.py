"""Read-only project capability discovery over the shared logical contracts.

Admission is a connection ceiling, inherited by every project on that connection.
It never supplies filesystem, Zotero, Drive or operation authorization. Discovery
uses configuration only: it neither opens credential files nor probes services.
The existing operation validators remain authoritative for each actual request.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from paper_studio import __version__
from paper_studio.contracts import BY_NAME
from paper_studio.security.policy import fail


SCHEMA = 'paper-studio.capabilities.v1'
DISCOVERY_TOOLS = frozenset({'paper_capabilities', 'paper_runtime_status'})
# This is an explicit, versioned catalog, not a wildcard for future contracts.
# Admission exposes a route; the existing per-project operation guards still
# require trusted TeX targets, exact filesystem paths and scoped Zotero grants.
PAPER_EDITOR_PROFILE = 'paper-editor.v1'
ADMISSION_PROFILES = {PAPER_EDITOR_PROFILE: frozenset({
    'paper_capabilities', 'paper_runtime_status', 'paper_project_context',
    'paper_project_inventory', 'paper_read_files', 'paper_edit_files',
    'paper_build_plan', 'paper_tex_build_submit', 'paper_build_status',
    'paper_build_artifact', 'paper_pdf_review', 'paper_manuscript_build',
    'paper_figure_build', 'paper_pptx_inspect', 'paper_pptx_edit',
    'paper_version_list', 'paper_version_save', 'paper_version_compare',
    'paper_version_derive', 'paper_publish_to_drive', 'paper_source_sync', 'paper_drive_verify',
    'paper_reference_manage', 'paper_zotero_read', 'paper_zotero_manage',
    'paper_zotero_add_reference', 'paper_zotero_verify_citations',
    'paper_zotero_sync_bibtex', 'paper_literature_search',
    'paper_reference_resolve', 'paper_citation_graph',
    'paper_open_access_lookup', 'paper_evidence_read', 'paper_research_state',
})}
# Existing extension instances without a profile retain their old ceiling.
# Keep that compatibility policy here so Gateway/extension registration uses
# one admission resolver; opting into the standard profile is a reviewed edit.
ADMISSION_PROFILES['paper-editor.v2'] = ADMISSION_PROFILES[PAPER_EDITOR_PROFILE] | {
    'paper_section', 'paper_section_delivery', 'paper_review_comments', 'paper_pdf_preview',
}
_V3_INDEX_TOOLS = frozenset({'paper_index_get', 'paper_index_revise'})
REVIEW_TOOLS = frozenset({'paper_review_proposal', 'paper_material_get'})
INDEX_TOOLS = _V3_INDEX_TOOLS | REVIEW_TOOLS
ADMISSION_PROFILES['paper-editor.v3'] = ADMISSION_PROFILES['paper-editor.v2'] | _V3_INDEX_TOOLS
ADMISSION_PROFILES['paper-editor.v4'] = ADMISSION_PROFILES['paper-editor.v3'] | REVIEW_TOOLS
LEGACY_EXTENSION_ADMISSION = frozenset({
    'paper_runtime_status', 'paper_project_context', 'paper_project_inventory',
    'paper_read_files', 'paper_edit_files', 'paper_tex_build_submit',
    'paper_figure_build', 'paper_pptx_inspect', 'paper_pptx_edit',
    'paper_pdf_review', 'paper_build_status', 'paper_build_artifact',
    'paper_version_list', 'paper_version_save', 'paper_publish_to_drive',
    'paper_source_sync', 'paper_build_plan',
})
HOST_TOOLS = REVIEW_TOOLS | frozenset({
    'paper_index_get', 'paper_index_revise',
    'paper_pdf_preview',
    'paper_section','paper_section_delivery','paper_review_comments',
    'paper_capabilities', 'paper_runtime_status', 'paper_build_plan',
    'paper_read_files', 'paper_edit_files', 'paper_manuscript_build',
    'paper_zotero_read', 'paper_zotero_add_reference', 'paper_zotero_manage',
    'paper_zotero_sync_bibtex', 'paper_zotero_verify_citations',
    'paper_literature_search', 'paper_reference_resolve', 'paper_citation_graph',
    'paper_open_access_lookup', 'paper_evidence_read', 'paper_research_state',
    'paper_source_sync',
})
LITERATURE_TOOLS = frozenset({'paper_literature_search', 'paper_reference_resolve',
                            'paper_citation_graph', 'paper_open_access_lookup'})
EDIT_KINDS = frozenset({'create', 'edit', 'rename', 'delete'})


def validate_admission(cfg):
    """Validate optional connection restrictions without reading any files."""
    if 'admission_profile' in cfg:
        profile = cfg['admission_profile']
        if not isinstance(profile, str) or profile not in ADMISSION_PROFILES:
            fail('CONFIG_INVALID', 'admission_profile must name an installed versioned Paper Studio profile')
    if 'admitted_tools' in cfg:
        names = cfg['admitted_tools']
        if (not isinstance(names, list) or len(names) > len(BY_NAME)
                or any(not isinstance(name, str) or name not in BY_NAME for name in names)
                or len(set(names)) != len(names)):
            fail('CONFIG_INVALID', 'admitted_tools must be a unique list of installed logical paper tools')
    if 'allowed_edit_kinds' in cfg:
        kinds = cfg['allowed_edit_kinds']
        if (not isinstance(kinds, list) or len(kinds) > len(EDIT_KINDS)
                or any(not isinstance(kind, str) or kind not in EDIT_KINDS for kind in kinds)
                or len(set(kinds)) != len(kinds)):
            fail('CONFIG_INVALID', 'allowed_edit_kinds must be a unique list of supported edit operations')


def admitted_tool_names(cfg):
    """Metadata discovery stays available even on a read-only empty ceiling."""
    validate_admission(cfg)
    profile = cfg.get('admission_profile')
    ceiling = ADMISSION_PROFILES[profile] if profile else frozenset(BY_NAME)
    names = cfg.get('admitted_tools')
    return (ceiling if names is None else ceiling.intersection(names)) | DISCOVERY_TOOLS


def visible_index_tools(cfg):
    if not cfg.get('target_index', {}).get('enabled'):
        return frozenset()
    names = INDEX_TOOLS if cfg.get('admission_profile') == 'paper-editor.v4' else _V3_INDEX_TOOLS
    if not cfg.get('review_proposals',{}).get('enabled'):
        names = names - {'paper_review_proposal'}
    return names.intersection(admitted_tool_names(cfg))


def legacy_release_blocked(cfg, name, args):
    return bool(cfg.get('target_index', {}).get('enabled') and (
        name == 'paper_version_save' or name == 'paper_version_derive' and args.get('action') in {'promote', 'activate'}))


def guard_release_route(cfg, name, args):
    if name == 'paper_publish_to_drive':
        review = 'review_pdf' in args
        if review == ('version' in args) or review and 'resume_build_id' in args:
            fail('SCHEMA_INVALID', 'Select one review_pdf or a version with optional resume_build_id')
        if review and (cfg.get('admission_profile') != 'paper-editor.v4'
                       or cfg.get('execution_policy', {}).get('allow_text_reviews') is not True):
            fail('PERMISSION_DENIED', 'The project has not enabled single-PDF text reviews')
    if legacy_release_blocked(cfg, name, args):
        fail('EXPLICIT_RELEASE_REQUIRED', 'Register an explicitly requested formal release through the local index publisher')


def extension_admission_config(cfg):
    """Apply only the legacy extension default; never replace explicit limits."""
    if 'admission_profile' not in cfg and 'admitted_tools' not in cfg:
        cfg = {**cfg, 'admitted_tools': sorted(LEGACY_EXTENSION_ADMISSION)}
    validate_admission(cfg)
    return cfg


def require_admitted(cfg, name):
    """Enforce only explicit admission; operation-specific guards stay in place."""
    if name not in BY_NAME:
        fail('TOOL_DENIED', 'Unknown logical Paper Studio tool')
    if name in REVIEW_TOOLS and name not in visible_index_tools(cfg):
        fail('PERMISSION_DENIED', 'This project has not enabled indexed review and material lookup')
    if name not in admitted_tool_names(cfg):
        fail('PERMISSION_DENIED', 'This connection has not admitted the requested tool',
             project_id=cfg.get('project_id'), tool=name, availability='permission_denied')


def require_edit_kind(cfg, kind):
    if kind not in cfg.get('allowed_edit_kinds', EDIT_KINDS):
        fail('WRITE_DENIED', 'This connection does not permit this edit operation', kind=kind)


def _state(status='available', reason='configured', **fields):
    return {'status': status, 'reason': reason, **fields}


def _route(cfg, name):
    if name in HOST_TOOLS or cfg.get('legacy_provider_only') and name in {'paper_project_context','paper_project_inventory'}:
        return {'kind': 'host', 'logical_tool': name, 'gateway_tool': name,
                'native_runtime_tool': False}
    route = {'kind': 'provider', 'logical_tool': name, 'gateway_tool': name,
             'native_tool': 'plugin_tool', 'runner': cfg.get('client_id'),
             'provider_id': cfg.get('provider_id', 'research-build'),
             'requires_observed_binding': True}
    if name == 'paper_build_artifact':
        # The Host Gateway performs MCP image/ResourceLink delivery after the
        # Provider supplies verified metadata; a Provider binding cannot.
        route['host_only_actions'] = ['export', 'image']
    return route


def _zotero_read(cfg):
    scope = cfg.get('zotero_readonly')
    return isinstance(scope, dict) and bool(scope.get('items'))


def _zotero_scope(cfg):
    value = cfg.get('zotero_write')
    return value if isinstance(value, dict) and value.get('references') else None


def _zotero_grant(cfg):
    backend = cfg.get('zotero_backend')
    return (isinstance(backend, dict) and backend.get('backend') == 'pyzotero_local'
            and bool(backend.get('server_id')) and bool(backend.get('credential_path')))


def _writable(cfg):
    return bool(cfg.get('writable_roots'))


def _pptx_candidate_directories(cfg):
    figures = PurePosixPath(cfg.get('execution_policy', {}).get('figure_root', 'candidates'))
    roots = set()
    for value in cfg.get('writable_roots', []):
        writable = PurePosixPath(value)
        if writable.suffix:
            continue
        candidate = figures if figures.is_relative_to(writable) else writable
        if candidate.is_relative_to(figures) and not any(
                candidate.is_relative_to(readonly) for readonly in cfg.get('readonly_roots', [])):
            roots.add(candidate.as_posix())
    return sorted(roots)


def _tool_state(cfg, name, args):
    from paper_studio.gateway.delivery_policy import restricted
    if restricted(cfg):
        blocked_delivery = _state('permission_denied', 'chat_file_delivery_drive_links_only')
        if name in {'paper_pdf_preview', 'paper_section_delivery'}:
            return blocked_delivery
        if name == 'paper_build_artifact':
            metadata = _state(reason='local_metadata_only')
            copied = _state(reason='local_candidate_copy_checked_on_call')
            selected = args.get('action', 'metadata')
            return {**({'metadata': metadata, 'copy_to_candidate': copied}.get(selected, blocked_delivery)),
                    'actions': {'metadata': metadata, 'copy_to_candidate': copied,
                                'export': blocked_delivery, 'image': blocked_delivery, 'chunk': blocked_delivery}}
        if name == 'paper_index_get' and args.get('part') == 'content':
            return blocked_delivery
        if name == 'paper_manuscript_build' and args.get('action') == 'read':
            return blocked_delivery
        if name == 'paper_review_proposal' and args.get('action') == 'get' and args.get('part') == 'artifact':
            return blocked_delivery
    if name in REVIEW_TOOLS:
        if name not in visible_index_tools(cfg):
            return _state('not_configured', 'indexed_review_workflow_not_enabled')
        if name == 'paper_review_proposal' and args.get('action') in {'propose','decide','apply'} and (
                not _writable(cfg) or 'create' not in cfg.get('allowed_edit_kinds', EDIT_KINDS)):
            return _state('permission_denied', 'no_review_write_scope')
        return _state(reason='exact_project_index')
    if name == 'paper_index_revise' and cfg.get('admission_profile') == 'paper-editor.v4':
        return _state('permission_denied', 'confirmed_review_required')
    if legacy_release_blocked(cfg, name, args):
        return _state('permission_denied', 'formal_release_requires_explicit_local_publication')
    if name in {'paper_index_get', 'paper_index_revise'}:
        if not cfg.get('target_index'):
            return _state('not_configured', 'project_target_index_not_configured')
        if name == 'paper_index_revise' and ('create' not in cfg.get('allowed_edit_kinds', EDIT_KINDS)
                                              or not cfg['target_index'].get('ingest_roots') or not _writable(cfg)):
            return _state('permission_denied', 'no_working_revision_ingest_scope')
        return _state(reason='project_scoped_exact_index_keys')
    if name == 'paper_pdf_preview':
        if not cfg.get('pdf_preview'):
            return _state('not_configured','project_pdf_preview_not_configured')
        if 'paper_build_artifact' not in admitted_tool_names(cfg):
            return _state('permission_denied','project_artifact_read_not_admitted')
        return _state(reason='current_native_binding_and_immutable_archive_checked_on_call',
                      version=cfg['pdf_preview']['version'])
    if name == 'paper_section_delivery':
        if cfg.get('chapter_workflow') or cfg.get('execution_policy',{}).get('tex_targets'):
            return _state(reason='verified_section_or_immutable_manuscript_archive_checked_on_call')
        return _state('not_configured','project_delivery_sources_not_configured')
    if name == 'paper_section':
        if not cfg.get('chapter_workflow'):
            return _state('not_configured','project_chapter_workflow_not_configured')
        if name == 'paper_section' and args.get('action') == 'stage' and 'create' not in cfg.get('allowed_edit_kinds', EDIT_KINDS):
            return _state('permission_denied','project_create_not_authorized')
        return _state()
    if name == 'paper_review_comments':
        if not cfg.get('drive_folder_id'):
            return _state('not_configured','project_drive_publisher_not_configured')
        if args.get('action')=='apply' and (not _writable(cfg)
                or 'edit' not in cfg.get('allowed_edit_kinds', EDIT_KINDS)):
            return _state('permission_denied','no_confirmed_source_edit_scope')
        return _state(lightweight_receipts=(cfg.get('admission_profile')=='paper-editor.v4'
                    and cfg.get('execution_policy',{}).get('allow_text_reviews') is True),
                    confirmation='explicit_current_drive_user_reply_required_before_source_edit')
    if name in DISCOVERY_TOOLS:
        return _state(reason='metadata_discovery')
    if name == 'paper_build_plan':
        targets = cfg.get('execution_policy', {}).get('tex_targets', {})
        if not targets and not cfg.get('chapter_workflow'):
            return _state('not_configured', 'trusted_manuscript_targets_not_configured')
        return _state(targets=[{'name': key, 'tex_path': value['tex_path']}
                               for key, value in sorted(targets.items())])
    if name == 'paper_edit_files':
        kinds = cfg.get('allowed_edit_kinds', sorted(EDIT_KINDS))
        if not _writable(cfg) or not kinds:
            return _state('permission_denied', 'no_candidate_write_scope')
        requested = {row.get('kind') for row in args.get('changes', []) if isinstance(row, dict)}
        if requested - set(kinds):
            return _state('permission_denied', 'edit_kind_not_admitted', allowed_edit_kinds=kinds)
        return _state(reason='candidate_paths_and_revisions_checked_on_call', allowed_edit_kinds=kinds,
                      writable_roots=list(cfg.get('writable_roots', [])))
    if name == 'paper_zotero_read' or name == 'paper_zotero_verify_citations':
        return _state() if _zotero_read(cfg) else _state('not_configured', 'project_zotero_read_scope_not_configured')
    if name == 'paper_zotero_add_reference':
        if not _zotero_scope(cfg):
            return _state('permission_denied' if _zotero_read(cfg) else 'not_configured',
                          'project_zotero_write_scope_not_configured')
        write = (_state(reason='explicit_authorization_and_live_grant_checked_on_call') if _zotero_grant(cfg)
                 else _state('not_configured' if cfg.get('zotero_backend') else 'unsupported',
                             'pinned_local_write_grant_not_configured' if cfg.get('zotero_backend')
                             else 'legacy_backend_has_no_safe_collection_write'))
        actions = {'preflight': _state(reason='single_authorized_reference_preflight'), 'import': write}
        return {**(write if args.get('dry_run', True) is False else _state(reason='dry_run_available')),
                'actions': actions}
    if name == 'paper_zotero_manage':
        backend = cfg.get('zotero_backend')
        if not isinstance(backend, dict) or backend.get('backend') != 'pyzotero_local':
            return _state('not_configured', 'project_local_zotero_backend_not_configured')
        scoped = bool(_zotero_scope(cfg))
        read_collections = bool(cfg.get('zotero_readonly', {}).get('collection_keys'))
        query = _state() if scoped or read_collections else _state('permission_denied', 'project_collection_scope_not_configured')
        create = (_state(reason='explicit_authorization_and_live_grant_checked_on_call')
                  if scoped and _zotero_grant(cfg) and backend.get('allow_create_collection') is True
                  else _state('permission_denied', 'collection_creation_not_enabled_or_granted'))
        actions = {'status': _state(reason='local_backend_read_probe'), 'collection_query': query,
                   'collection_create': create, 'update': _state('unsupported', 'existing_item_updates_not_implemented')}
        action = args.get('action', 'status')
        selected = actions['update'] if action == 'update' else (
            create if action == 'collection' and args.get('dry_run', True) is False else
            query if action == 'collection' else actions['status'])
        return {**selected, 'actions': actions}
    if name == 'paper_zotero_sync_bibtex':
        if not _zotero_read(cfg):
            return _state('not_configured', 'project_zotero_read_scope_not_configured')
        binding = cfg.get('zotero_write', {}).get('bib_sync', {})
        enabled = binding.get('enabled') is True and bool(binding.get('paths'))
        apply = (_state(reason='exact_bib_path_native_revision_sha_and_authorization_checked_on_call',
                        paths=list(binding['paths'])) if enabled else
                 _state('permission_denied', 'project_bibtex_write_scope_not_enabled'))
        if enabled and args.get('bib_path') and args['bib_path'] not in binding['paths']:
            apply = _state('permission_denied', 'bibliography_path_not_authorized')
        return {**(apply if args.get('dry_run', True) is False else _state(reason='dry_run_available')),
                'actions': {'dry_run': _state(), 'apply': apply}}
    if name in LITERATURE_TOOLS:
        policy = cfg.get('literature', {})
        if not isinstance(policy, dict) or policy.get('enabled') is not True:
            return _state('not_configured', 'public_literature_metadata_not_enabled')
        provider = args.get('provider', 'crossref')
        if provider not in policy.get('providers', []):
            return _state('unsupported', 'literature_provider_not_enabled')
        return _state(providers=list(policy['providers']))
    if name == 'paper_evidence_read':
        policy = cfg.get('evidence_reader', {})
        if not isinstance(policy, dict) or policy.get('enabled') is not True:
            return _state('not_configured', 'bounded_pdf_evidence_reader_not_enabled')
        return _state(max_pdf_bytes=policy['max_pdf_bytes'])
    if name == 'paper_research_state':
        return _state() if cfg.get('research_state_path') else _state('not_configured', 'project_continuity_file_not_configured')
    if name == 'paper_source_sync':
        policy = cfg.get('drive_source_sync')
        if not isinstance(policy, dict):
            return _state('not_configured', 'project_source_sync_scope_not_configured')
        grant = bool(cfg.get('drive_oauth_file'))
        read = _state() if grant else _state('not_configured', 'drive_credential_reference_not_configured')
        write = (_state(reason='explicit_authorization_and_snapshot_identity_checked_on_call')
                 if policy.get('enabled') is True and grant else
                 _state('permission_denied' if grant else 'not_configured', 'source_sync_apply_not_enabled_or_configured'))
        preview_import = read if _writable(cfg) else _state('permission_denied', 'no_candidate_review_write_scope')
        importing = write if _writable(cfg) else _state('permission_denied', 'no_candidate_review_write_scope')
        actions = {'plan': read, 'status': _state(), 'sync_dry_run': read,
                   'sync_apply': write, 'import_review_dry_run': preview_import,
                   'import_review_apply': importing}
        action = args.get('action', 'plan')
        selected = (actions['status'] if action == 'status' else
                    (importing if args.get('dry_run', True) is False else preview_import) if action == 'import_review' else
                    write if action == 'sync' and args.get('dry_run', True) is False else read)
        return {**selected, 'actions': actions,
                'project_binding_mode': policy.get('project_binding_mode', 'tagged')}
    if name == 'paper_drive_verify':
        if not cfg.get('drive_oauth_file'):
            return _state('not_configured', 'drive_credential_reference_not_configured')
        return _state(reason='local_publisher_metadata_reads_checked_on_call',
                      metadata_only=True, arbitrary_accessible_ids=True, max_file_ids=20,
                      grants_write_permission=False)
    if name == 'paper_publish_to_drive':
        if not cfg.get('drive_oauth_file') or not cfg.get('drive_folder_id'):
            return _state('not_configured', 'project_drive_publisher_not_configured')
        return _state(reason='source_bound_pdf_or_immutable_version_and_remote_identity_checked_on_call',
                      review_pdf_sources=['marked_text_review','controlled_tex_review'],
                      single_review_pdf=(cfg.get('admission_profile') == 'paper-editor.v4'
                                         and cfg.get('execution_policy', {}).get('allow_text_reviews') is True))
    if name == 'paper_manuscript_build':
        if not cfg.get('manuscript_roots'):
            return _state('not_configured', 'managed_manuscript_roots_not_configured')
        if args.get('action') == 'restore' and not _writable(cfg):
            return _state('permission_denied', 'no_candidate_restore_scope')
        return _state(manuscript_roots=list(cfg['manuscript_roots']))
    if name == 'paper_version_derive' and not _writable(cfg):
        return _state('permission_denied', 'no_candidate_restore_scope')
    if name == 'paper_version_derive':
        managed = (_state(reason='expected_state_hashes_and_frozen_revision_checks_on_call')
                   if cfg.get('version_management') else _state('not_configured','managed_paper_versions_not_enabled'))
        actions = {action: managed for action in ['status','checkout','restore','rollback','stage','dependency','revise','figure','select','promote','activate']}
        actions['derive'] = _state(reason='legacy_create_only_candidate_derivation')
        if cfg.get('target_index', {}).get('enabled'):
            for action in ('promote', 'activate'):
                actions[action] = _state('permission_denied', 'formal_release_requires_explicit_local_publication')
        return {**actions.get(args.get('action','derive'), managed), 'actions': actions}
    if name == 'paper_build_artifact':
        if args.get('action') == 'copy_to_candidate' and not _writable(cfg):
            return _state('permission_denied', 'no_candidate_materialization_scope')
        return _state(reason='immutable_artifact_identity_checked_on_call')
    if name == 'paper_figure_build':
        engines = {'python': _state(), 'svg': _state(), 'tikz': _state(),
                   'matlab': _state('unsupported', 'matlab_execution_adapter_not_implemented')}
        engine = args.get('engine')
        return {**engines.get(engine, _state(reason='configured_figure_engines')),
                'engines': engines}
    if name == 'paper_tex_build_submit':
        policy = cfg.get('execution_policy', {})
        managed = cfg.get('manuscript_roots', [])
        # The execution planner also accepts immutable bilingual section entries.
        # This describes that route only; its template/manifest/source checks in
        # execution.target_for remain mandatory for every submit and recovery.
        sections = bool(policy and cfg.get('chapter_workflow'))
        trusted = bool(policy.get('tex_targets')) or sections
        submit = (_state(reason='trusted_section_manifest_and_template_checked_on_call',
                         chapter_root=cfg['chapter_workflow']['root'],
                         entrypoint_names=['main_en.tex', 'main_zh.tex']) if sections else
                  _state() if trusted else
                  _state('not_configured', 'trusted_manuscript_targets_not_configured'))
        recovery = (_state(reason='failed_inactive_same_source_snapshot_only') if trusted else
                    _state('unsupported', 'direct_tex_recovery_requires_trusted_current_targets'))
        if managed:
            recovery = _state('unsupported', 'managed_bnnn_uses_existing_archive_workflow')
        return {**(recovery if args.get('resume_build_id') else submit), 'actions': {'submit': submit, 'recover': recovery}}
    if name == 'paper_pptx_edit':
        rendered = _state() if cfg.get('windows_render') is True else _state('not_configured', 'windows_powerpoint_render_not_enabled')
        candidate_directories = _pptx_candidate_directories(cfg)
        imported = (_state(reason='exact_pptx_id_and_publisher_project_ancestry_checked_on_call',
                           configured_review_root_id=cfg['drive_folder_id'],
                           project_folder_name=cfg['project_id'],
                           candidate_directories=candidate_directories)
                    if cfg.get('drive_folder_id') and cfg.get('drive_oauth_file') else
                    _state('not_configured', 'project_drive_publisher_not_configured'))
        if not _writable(cfg) or 'create' not in cfg.get('allowed_edit_kinds', EDIT_KINDS):
            imported = _state('permission_denied', 'project_create_not_authorized')
        elif not candidate_directories:
            imported = _state('not_configured', 'new_pptx_candidate_directory_not_authorized')
        return {**(imported if args.get('action') == 'import_drive' else _state()),
                'actions': {'create_edit_export': _state(), 'windows_render': rendered, 'import_drive': imported}}
    return _state(reason='project_scope_checked_on_call')


def tool_capability(cfg, name, args=None):
    args = args or {}
    if name not in BY_NAME:
        return {'name': name, **_state('unsupported', 'logical_tool_not_installed')}
    if name not in admitted_tool_names(cfg):
        return {'name': name, **_state('permission_denied', 'connection_tool_not_admitted'), 'route': _route(cfg, name)}
    if args.get('project_id', cfg.get('project_id')) != cfg.get('project_id'):
        return {'name': name, **_state('permission_denied', 'request_project_identity_mismatch'), 'route': _route(cfg, name)}
    return {'name': name, **_tool_state(cfg, name, args), 'route': _route(cfg, name)}


def describe_capabilities(cfg):
    tools = [tool_capability(cfg, name) for name in sorted(BY_NAME)]
    release = cfg.get('release', {})
    safe_release = ({key: release[key] for key in ['version', 'release_id', 'source_sha256', 'manifest_sha256', 'git_commit']
                     if key in release} if isinstance(release, dict) else {})
    read_scope = cfg.get('zotero_readonly', {})
    references = (cfg.get('zotero_write') or {}).get('references', {})
    credential_refs = cfg.get('credentials', {})
    return {'schema': SCHEMA, 'project_id': cfg['project_id'],
            'chat_file_delivery': cfg.get('chat_file_delivery', 'native'),
            'instance_id': cfg.get('instance_id'), 'client_id': cfg.get('client_id'),
            'native_project': cfg.get('native_project'), 'native_artifact_project': cfg.get('native_artifact_project'),
            'project_root': cfg['project_root'], 'gateway_version': __version__,
            'extension_version': cfg.get('extension_version'), 'config_revision': cfg.get('_config_revision'),
            'release': safe_release, 'verification': 'configuration_only',
            'runtime_health': 'not_probed_by_this_readonly_tool',
            'history_lookup': {'tool': 'paper_read_files',
                'uri_template': 'paper-history://version/revision-or-~/language/role[/figure|/key/name]',
                'catalogue': 'explicit_local_import_required', 'state': 'not_probed',
                'ambiguity': 'returned_explicitly_no_latest_guess', 'changes_legacy_save_or_publish': False},
            'tools': tools, 'available_tools': [row['name'] for row in tools if row['status'] == 'available'],
            'admission': {'configured': 'admitted_tools' in cfg or 'admission_profile' in cfg,
                          'profile': cfg.get('admission_profile'),
                          'explicit_tool_ceiling': 'admitted_tools' in cfg,
                          'scope': 'connection_ceiling_shared_by_all_registered_projects',
                          'grants_filesystem_or_operation_permission': False},
            'credential_references_configured': {**{role: bool(credential_refs.get(role)) for role in ['reader', 'writer', 'builder']},
                                                'drive': bool(cfg.get('drive_oauth_file')),
                                                'zotero_local_dpapi': bool((cfg.get('zotero_backend') or {}).get('credential_path'))},
            'zotero_scope': {'citation_keys': sorted(read_scope.get('items', {})),
                             'authorized_write_citation_keys': sorted(references),
                             'authorized_collections': sorted({value['collection_name'] for value in references.values()}),
                             'local_grant_configured': bool(_zotero_grant(cfg)), 'local_grant_validity': 'not_probed'},
            'limitations': ['Available means configured and admitted, not a live service or credential validity check.',
                            'Action authorization, exact project paths, source hashes and revisions are checked by the existing tool implementation.',
                            'No credentials, attachment paths, PDF contents or arbitrary Native runtime routes are returned.']}
