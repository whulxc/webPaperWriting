"""Exact revision-to-review receipts in the existing private history catalogue."""
import json
import re
from pathlib import Path

from paper_studio.security.policy import StudioError, fail, safe_read
from paper_studio.versions import history_objects


def _journal(cfg, receipt_id):
    if not isinstance(receipt_id, str) or not re.fullmatch('[0-9a-f]{64}', receipt_id):
        fail('REVIEW_RECEIPT_CHANGED', 'An exact review receipt ID is required')
    return json.loads(safe_read(Path(cfg['state_root']) / 'review-display', receipt_id + '.json', 2 * 1024**2))


def _binding(cfg, receipt_id):
    from paper_studio.drive.review_comments import lightweight_context
    from paper_studio.provider.jobs import job_path
    context = lightweight_context(cfg, receipt_id)
    job = job_path(cfg, context['review_build_id'])
    record = json.loads(safe_read(job, 'request.json', 4 * 1024**2))
    binding = record.get('request', {}).get('_version_binding')
    if not isinstance(binding, dict):
        return None
    from paper_studio.versions.lifecycle import _revision
    _, metadata, _ = _revision(cfg, binding.get('version'), binding.get('revision'))
    if (binding.get('language') != context['pdfs'][0]['language']
            or metadata['revisions'][binding['revision']] != binding.get('manifest_sha256')):
        fail('REVIEW_RECEIPT_CHANGED', 'Review receipt differs from its managed revision')
    return binding, context, _journal(cfg, receipt_id)


def register(cfg, receipt_id):
    """Append one successful managed display; retries complete an interrupted append."""
    if not cfg.get('version_management'):
        return
    selected = _binding(cfg, receipt_id)
    if selected is None:
        return
    binding, context, journal = selected
    row = (receipt_id, binding['version'], binding['revision'], binding['language'],
           context['review_build_id'], journal['plan_sha256'])
    with history_objects._db(cfg, create=True) as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS review_deliveries(
            receipt_id TEXT PRIMARY KEY,version TEXT NOT NULL,revision TEXT NOT NULL,
            language TEXT NOT NULL,build_id TEXT NOT NULL,plan_sha256 TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS review_delivery_selector ON review_deliveries(version,revision,language);
          CREATE INDEX IF NOT EXISTS review_delivery_revision ON review_deliveries(revision,version);
        ''')
        db.execute('BEGIN IMMEDIATE')
        try:
            previous = db.execute('SELECT * FROM review_deliveries WHERE receipt_id=?', (receipt_id,)).fetchone()
            if previous is not None and tuple(previous) != row:
                fail('REVIEW_RECEIPT_CHANGED', 'Indexed review belongs to another revision')
            db.execute('INSERT OR IGNORE INTO review_deliveries VALUES(?,?,?,?,?,?)', row)
            db.commit()
        except BaseException:
            db.rollback()
            raise


def lookup(cfg, version, revision):
    """Read only the selected revision, without remote calls or history discovery."""
    result = {'version': version, 'revision': revision, 'items': [],
              'remote_revalidated': False, 'directory_enumeration_attempts': 0}
    if not history_objects.path(cfg).exists():
        return result
    with history_objects._db(cfg) as db:
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_deliveries'").fetchone() is None:
            return result
        rows = db.execute('SELECT * FROM review_deliveries WHERE version=? AND revision=? ORDER BY language,receipt_id LIMIT 33',
                          (version, revision)).fetchall()
    result['items_truncated'] = len(rows) > 32
    from paper_studio.drive.target_display import review_build_source
    for row in rows[:32]:
        receipt_id = row['receipt_id']
        try:
            selected = _binding(cfg, receipt_id)
            if selected is None:
                fail('REVIEW_RECEIPT_CHANGED', 'Indexed display has no managed revision')
            binding, context, journal = selected
            if ((binding['version'], binding['revision'], binding['language'], context['review_build_id'], journal['plan_sha256'])
                    != tuple(row[key] for key in ('version', 'revision', 'language', 'build_id', 'plan_sha256'))):
                fail('REVIEW_RECEIPT_CHANGED', 'Indexed display identity changed')
            pdf = context['pdfs'][0]
            receipt = journal['receipt']
            item = {'status': 'verified_local_receipt', 'review_receipt_id': receipt_id,
                    'language': pdf['language'], 'build_id': pdf['build_id'], 'file_id': pdf['file_id'],
                    'name': pdf['name'], 'sha256': pdf['pdf_sha256'], 'bytes': pdf['bytes'], 'pages': pdf['pages'],
                    'url': 'https://drive.google.com/file/d/' + pdf['file_id'] + '/view',
                    'last_remote_verified_at': receipt.get('verified_at'), 'remote_revalidated': False}
            try:
                review_build_source(cfg, pdf['build_id'], pdf['language'], current=True)
                item['current_inputs_verified'] = True
            except StudioError as error:
                item.update(current_inputs_verified=False, current_source_error=error.code)
        except StudioError as error:
            item = {'status': 'invalid', 'review_receipt_id': receipt_id, 'error': error.code}
        except (OSError, ValueError, TypeError, KeyError):
            item = {'status': 'invalid', 'review_receipt_id': receipt_id, 'error': 'REVIEW_RECEIPT_CHANGED'}
        result['items'].append(item)
    return result


def parents(cfg, revision):
    """Find exact indexed parent labels for a short revision without choosing one."""
    if not history_objects.path(cfg).exists():
        return []
    with history_objects._db(cfg) as db:
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_deliveries'").fetchone() is None:
            return []
        rows = db.execute('SELECT DISTINCT version FROM review_deliveries WHERE revision=? ORDER BY version LIMIT 33',
                          (revision,)).fetchall()
    return [history_objects._label(row['version']) for row in rows]
