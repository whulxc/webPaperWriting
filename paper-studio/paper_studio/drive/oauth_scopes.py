"""Shared explicit OAuth-mode validation; never reads or writes credentials."""
from paper_studio.security.policy import fail

DRIVE_FILE_SCOPE='https://www.googleapis.com/auth/drive.file'
DRIVE_FULL_SCOPE='https://www.googleapis.com/auth/drive'
AUTHORIZATION_MODES=('drive_file','drive_full')


def validate_scope_string(value,authorization_mode='drive_file'):
    """Validate actual server-returned scopes for an explicitly selected mode.

    The default retains the existing drive.file-only contract. Full Drive is
    accepted only in drive_full mode, and no unrelated Google scopes are added.
    """
    if authorization_mode not in AUTHORIZATION_MODES:fail('OAUTH_SCOPE','Unsupported explicit Drive authorization mode')
    if not isinstance(value,str) or not value.strip():fail('OAUTH_SCOPE','The actual OAuth scope string is required')
    scopes=set(value.split())
    if authorization_mode=='drive_file':
        if scopes!={DRIVE_FILE_SCOPE}:fail('OAUTH_SCOPE','Actual OAuth grant must contain only drive.file in the default authorization mode')
    elif DRIVE_FULL_SCOPE not in scopes or scopes-{DRIVE_FULL_SCOPE,DRIVE_FILE_SCOPE}:
        fail('OAUTH_SCOPE','Explicit drive_full mode requires full Drive scope and rejects unrelated scopes')
    return sorted(scopes)


def validate_credential_scopes(grant):
    """Return (mode, validated scopes) without copying private token fields."""
    if not isinstance(grant,dict):fail('OAUTH_SCOPE','OAuth credential metadata must be an object')
    mode=grant.get('authorization_mode','drive_file');scopes=grant.get('scopes')
    if (not isinstance(scopes,list) or not scopes or any(not isinstance(item,str) or not item or any(char.isspace() for char in item) for item in scopes)
        or len(set(scopes))!=len(scopes)):
        fail('OAUTH_SCOPE','OAuth credentials must record the exact nonduplicated granted scope list')
    return mode,validate_scope_string(' '.join(scopes),mode)
