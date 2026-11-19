"""Project-scoped bilingual sections with immutable revision and material identities."""
from .workflow import dispatch, read_revision, target_for

__all__ = ['dispatch', 'read_revision', 'target_for']
