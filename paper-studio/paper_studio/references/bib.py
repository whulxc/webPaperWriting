"""Source-backed bibliography diagnostics, no generated reference metadata."""
import re
from pathlib import Path
from paper_studio.security.policy import safe_read,sha,fail

def inspect_bib(cfg,bib_path,tex_paths=None):
    raw=safe_read(cfg['project_root'],bib_path,2*1024*1024);text=raw.decode('utf-8');entries=re.findall(r'@([A-Za-z]+)\s*\{\s*([^,\s]+)',text)
    keys=[key for typ,key in entries if typ.lower() not in ['comment','preamble','string']];duplicates=sorted(k for k in set(keys) if keys.count(k)>1)
    cited=set()
    for p in tex_paths or []:
        tex=safe_read(cfg['project_root'],p,2*1024*1024).decode('utf-8')
        for chunk in re.findall(r'\\(?:cite|citep|citet|autocite|parencite)\*?(?:\[[^\]]*\])*\{([^}]+)\}',tex):cited.update(k.strip() for k in chunk.split(','))
    return {'path':bib_path,'sha256':sha(raw),'keys':keys,'duplicate_keys':duplicates,'unresolved_citations':sorted(cited-set(keys)),'metadata_verified_externally':False,'parser':'bounded citation-key diagnostic; BibTeX/Biber build is authoritative syntax check'}
