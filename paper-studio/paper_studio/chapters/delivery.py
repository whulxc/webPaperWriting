"""Receive an artifact using the public metadata/chunk contract with bounded memory."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile

from paper_studio.security.policy import fail, no_links


def receive(call, *, project_id, build_id, path, destination, expected_sha256=None,
            max_bytes=64 * 1024**2):
    """call(name, args) may be a gateway, remote MCP adapter, or in-process provider."""
    base = {'project_id': project_id, 'build_id': build_id, 'path': path}
    metadata = call('paper_build_artifact', {**base, 'action': 'metadata'})
    size, expected = metadata['bytes'], metadata['sha256']
    if type(size) is not int or not 0 < size <= max_bytes:
        fail('OUTPUT_LIMIT', 'Artifact size exceeds the receiving client budget')
    if expected_sha256 is not None and expected_sha256 != expected:
        fail('ARTIFACT_CHANGED', 'Artifact differs from the delivery selection')
    dest = no_links(Path(destination))
    if dest.exists():
        fail('DESTINATION_EXISTS', 'Delivery destination must be a new file')
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.receiving-', dir=dest.parent)
    offset = 0; count = 0; digest = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as stream:
            while offset < size:
                part = call('paper_build_artifact', {**base, 'action': 'chunk',
                    'offset': offset, 'length': min(262144, size - offset)})
                raw = base64.b64decode(part['data_base64'], validate=True)
                end = offset + len(raw)
                if (part['sha256'] != expected or part['bytes'] != size or part['offset'] != offset
                        or not raw or end > size or len(raw) > 262144
                        or hashlib.sha256(raw).hexdigest() != part['chunk_sha256']
                        or part['next_offset'] != (end if end < size else None)):
                    fail('ARTIFACT_CHANGED', 'Chunk identity, order or complete size differs')
                stream.write(raw); digest.update(raw); offset = end; count += 1
            stream.flush(); os.fsync(stream.fileno())
        if digest.hexdigest() != expected:
            fail('ARTIFACT_CHANGED', 'Assembled artifact SHA256 differs')
        # Hard-link commit is create-only, including a concurrent receiving client.
        os.link(name, dest)
        return {'project_id': project_id, 'build_id': build_id, 'path': path,
            'destination': str(dest), 'bytes': size, 'sha256': expected, 'chunks': count,
            'bytes_received': True, 'host_attachment_created': False}
    finally:
        Path(name).unlink(missing_ok=True)
