"""Small private create/replace helper shared by local management commands."""
import os
from pathlib import Path
import stat
import tempfile


def private_write(path, text, *, replace=False):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path:
        raise ValueError('Canonical absolute private file path required')
    for component in (path,*path.parents):
        if component.is_symlink():raise ValueError('Private file symlink rejected')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=text.encode('utf-8') if isinstance(text,str) else bytes(text)
    if not replace:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        return
    if path.exists():
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('Existing private file must be owned, single-link and mode 0600')
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()
