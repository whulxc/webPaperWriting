"""Read-only literal dependency plans for locally approved TeX recipes.

This is not a TeX interpreter. Project-local TeX/classes/packages are followed;
system packages are resolved and hashed, with runtime recorder checks retaining
authority over expansion, conditional selection and transitive system inputs.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

from paper_studio.security.policy import encoded, safe_read, sha
from paper_studio.security.sandbox import relative_path
from .worker import directory_options, snapshot_directory


COMMANDS = re.compile(r'\\(input|include|includegraphics|bibliography|bibliographystyle|addbibresource|documentclass|usepackage|RequirePackage|LoadClass)(?:\[[^\]]*\])?\s*\{([^}]*)\}')
GRAPHICS = re.compile(r'\\graphicspath\s*\{((?:\s*\{[^{}]*\})*\s*)\}')
FONT = re.compile(r'\\(?:setmainfont|setsansfont|setmonofont|setCJKmainfont|setCJKsansfont|setCJKmonofont)(?:\[([^\]]*)\])?\s*\{([^}]*)\}(?:\[([^\]]*)\])?')
EXTENSIONS = {'input': ('', '.tex'), 'include': ('.tex',), 'includegraphics': ('', '.pdf', '.png', '.jpg', '.eps'),
    'bibliography': ('.bib',), 'addbibresource': ('',), 'bibliographystyle': ('.bst',),
    'documentclass': ('.cls',), 'LoadClass': ('.cls',), 'usepackage': ('.sty',), 'RequirePackage': ('.sty',)}


def _hash(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _macro_ranges(text):
    """Identify definition bodies; their expansion cannot be decided statically."""
    starts = re.compile(r'\\(?:[gx e]?def|newcommand|renewcommand|providecommand|DeclareRobustCommand)\b'.replace(' ', ''))
    ranges = []
    for match in starts.finditer(text):
        cursor = match.end()
        # A newcommand's first braced argument is its command name.
        if not match.group().endswith('def'):
            command = re.match(r'\s*\{[^{}]*\}|\s*\\[^\s\[{}]+', text[cursor:])
            if command:
                cursor += command.end()
            while re.match(r'\s*\[[^\]]*\]', text[cursor:]):
                cursor += re.match(r'\s*\[[^\]]*\]', text[cursor:]).end()
        begin = text.find('{', cursor)
        if begin < 0:
            continue
        level = 1
        for end in range(begin + 1, len(text)):
            if text[end] in '{}' and (end == 0 or text[end - 1] != '\\'):
                level += 1 if text[end] == '{' else -1
                if level == 0:
                    ranges.append((begin, end))
                    break
    return ranges


def plan_dependencies(project_root, tex_path, working_directory='.', search_paths=None, *, texlive_root,
                      windows_fonts_root=None, max_files=2048, max_file_bytes=32 * 1024**2,
                      build_profile='xelatex'):
    """Return a current, sorted snapshot plan without running TeX or writing files.

    ``files`` and its SHA256 use the same encoding/order as provider.jobs.snapshot.
    Missing required paths and unresolved dynamic paths remain explicit; callers
    decide whether an approved target can proceed. Macro/conditional branches
    are conservatively reported and never claimed to have executed.
    """
    if not isinstance(build_profile,str) or build_profile not in {'pdflatex','xelatex','lualatex'}:
        raise ValueError('Unsupported fixed TeX dependency profile')
    root = Path(project_root)
    texlive = Path(texlive_root)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError('A real, unaliased absolute project root is required')
    if not texlive.is_absolute() or texlive.resolve() != texlive or not texlive.is_dir():
        raise ValueError('A real, unaliased absolute TeX Live root is required')
    paths = directory_options({'working_directory': working_directory, 'tex_search_paths': [] if search_paths is None else search_paths})
    cwd = snapshot_directory(root, working_directory)
    search = [snapshot_directory(root, value) for value in paths]
    entry = root / relative_path(tex_path)
    if entry.resolve() != entry or not entry.is_file() or not entry.is_relative_to(root):
        raise ValueError('TeX entry must be a real project file')
    kpse = texlive / 'bin/x86_64-linux/kpsewhich'
    if not kpse.is_file() or not kpse.resolve().is_relative_to(texlive):
        raise ValueError('The configured fixed kpsewhich is unavailable')
    font_roots = [texlive, Path('/usr/share/fonts')]
    if windows_fonts_root is not None:
        font_root = Path(windows_fonts_root)
        if not font_root.is_absolute() or font_root.resolve() != font_root or not font_root.is_dir():
            raise ValueError('Configured fonts root must be real and absolute')
        font_roots.append(font_root)
    dist, var = texlive / 'texmf-dist', texlive / 'texmf-var'
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/nonexistent',
        'TEXMFCNF': str(dist / 'web2c'), 'TEXMF': '{' + str(var) + ',' + str(dist) + '}',
        'TEXMFDBS': '{!!' + str(var) + ',!!' + str(dist) + '}', 'TEXMFHOME': '/nonexistent',
        'TEXMFCONFIG': '/nonexistent', 'TEXMFVAR': '/nonexistent', 'openin_any': 'p',
        'MKTEXFMT': '0', 'MKTEXPK': '0', 'MKTEXTFM': '0', 'MKTEXMF': '0', 'MKTEXTEX': '0'}
    local = ':'.join(str(p) for p in [cwd, *search])
    env.update(TEXINPUTS=local + ':!!' + str(dist / 'tex') + '//',
        BIBINPUTS=local + ':!!' + str(dist / 'bibtex/bib') + '//',
        BSTINPUTS=local + ':!!' + str(dist / 'bibtex/bst') + '//')
    files, system, fonts, unresolved, missing, literal = {}, {}, [], [], [], []
    lookup_cache = {}

    def project_bytes(path):
        if path.resolve() != path or not path.is_relative_to(root):
            raise ValueError('Project dependency is aliased or escapes the root')
        relative = path.relative_to(root).as_posix()
        raw = safe_read(root, relative, max_file_bytes)
        files[relative] = {'path': relative, 'bytes': len(raw), 'sha256': sha(raw)}
        if len(files) > max_files:
            raise ValueError('TeX dependency file limit exceeded')
        return raw

    def trusted_system(path):
        resolved = path.resolve()
        if not resolved.is_relative_to(texlive) or not resolved.is_file():
            raise ValueError('kpsewhich resolved outside the configured TeX Live tree')
        if str(resolved) not in system:
            system[str(resolved)] = {'path': str(resolved), 'bytes': resolved.stat().st_size, 'sha256': _hash(resolved)}
        return resolved

    def lookup(name):
        if name not in lookup_cache:
            probe = subprocess.run([str(kpse), '-progname='+build_profile, name], cwd=cwd, env=env,
                                   capture_output=True, text=True, timeout=10, check=False)
            found = probe.stdout.strip() if probe.returncode == 0 else ''
            if len(found) > 4096 or '\n' in found:
                raise ValueError('Unbounded or ambiguous kpsewhich response')
            lookup_cache[name] = Path(found) if found else None
        found = lookup_cache[name]
        if found is None:
            return None
        found = found if found.is_absolute() else cwd / found
        if found.is_relative_to(root):
            if found.resolve() != found:
                raise ValueError('kpsewhich found an aliased project file')
            return found
        return trusted_system(found)

    def resolve(name, command, graphics):
        if (not name or len(name) > 1024 or name.startswith('-') or Path(name).is_absolute()
            or any(char in name for char in '\\\x00:$#{}*?[]\r\n\t')):
            return None, 'unsafe_or_dynamic'
        if not (cwd / name).resolve().is_relative_to(root):
            raise ValueError('Literal TeX dependency escapes the project root')
        directories = [cwd, *graphics, *search] if command == 'includegraphics' else [cwd, *search]
        for ext in EXTENSIONS[command]:
            filename = name if ext and name.endswith(ext) else name + ext
            for directory in directories:
                candidate = Path(os.path.abspath(directory / filename))
                if candidate.resolve() != candidate and (candidate.exists() or candidate.is_symlink()):
                    raise ValueError('Project dependency aliases are forbidden')
                if not candidate.resolve().is_relative_to(root):
                    continue
                if candidate.is_file():
                    if candidate.resolve() != candidate:
                        raise ValueError('Project dependency aliases are forbidden')
                    return candidate, None
            if command != 'includegraphics':
                found = lookup(filename)
                if found is not None:
                    return found, None
        return None, 'not_found'

    def font_declaration(path, match):
        options = (match.group(1) or '') + ',' + (match.group(3) or '')
        names = [match.group(2)] + re.findall(r'(?:BoldFont|ItalicFont|BoldItalicFont|SmallCapsFont)\s*=\s*([^,\]\s]+)', options)
        path_option = re.search(r'(?:^|,)\s*Path\s*=\s*([^,\]]+)', options)
        directory = path_option.group(1).strip() if path_option else None
        for name in dict.fromkeys(names):
            row = {'declared_in': path.relative_to(root).as_posix(), 'name': name, 'path_option': directory,
                   'selection': 'declared_candidate; actual engine selection requires recorder'}
            if any(char in name for char in '\\\x00:$#{}*?[]\r\n\t'):
                row['resolution'] = 'dynamic_unverified'
                fonts.append(row)
                continue
            candidates = []
            if Path(name).suffix.lower() in {'.ttf', '.ttc', '.otf'}:
                if directory:
                    base = Path(directory)
                    base = base if base.is_absolute() else cwd / base
                    candidates.append(base / name)
                else:
                    candidates += [base / name for base in [cwd, *search, *font_roots]]
                    found = lookup(name)
                    if found:
                        candidates.append(found)
            else:
                fc = Path('/usr/bin/fc-match')
                if fc.is_file():
                    probe = subprocess.run([str(fc), '--format', '%{family}\n%{file}', name], capture_output=True,
                        text=True, timeout=10, check=False, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/nonexistent'})
                    lines = probe.stdout.splitlines()
                    if probe.returncode == 0 and len(lines) == 2:
                        row['matched_family'] = lines[0]
                        row['substitution'] = name.casefold() not in [n.strip().casefold() for n in lines[0].split(',')]
                        candidates.append(Path(lines[1]))
            for candidate in candidates:
                resolved = candidate.resolve()
                if not (resolved.is_relative_to(root) or any(resolved.is_relative_to(base) for base in font_roots)):
                    continue
                if resolved.is_file():
                    if resolved.is_relative_to(root):
                        project_bytes(candidate)
                    row.update(resolution='resolved_candidate', path=str(resolved), bytes=resolved.stat().st_size, sha256=_hash(resolved))
                    break
            row.setdefault('resolution', 'unresolved_optional_font_candidate')
            fonts.append(row)

    pending, visited = [(entry, ())], set()
    while pending:
        path, graphics = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        raw = project_bytes(path)
        if len(raw) > 2 * 1024**2:
            raise ValueError('TeX source exceeds bounded parsing limit')
        text = re.sub(r'(?<!\\)%[^\n]*', '', raw.decode('utf-8', errors='replace'))
        macros = _macro_ranges(text)
        for occurrence in re.finditer(r'\\graphicspath\b', text):
            if GRAPHICS.match(text, occurrence.start()) is None:
                raise ValueError('Unsafe or dynamic graphicspath')
        for occurrence in re.finditer(r'\\(?:' + '|'.join(EXTENSIONS) + r')\b', text):
            if COMMANDS.match(text, occurrence.start()) is None:
                unresolved.append({'file': path.relative_to(root).as_posix(), 'command': occurrence.group()[1:],
                    'value': None, 'resolved': None, 'reason': 'unsupported_unbraced_or_dynamic_dependency',
                    'required': not any(begin < occurrence.start() < end for begin, end in macros)})
        for font in FONT.finditer(text):
            font_declaration(path, font)
        pattern = re.compile(GRAPHICS.pattern + '|' + COMMANDS.pattern)
        for match in pattern.finditer(text):
            graphics_body, command, body = match.groups()
            if graphics_body is not None:
                directories = []
                for value in re.findall(r'\{([^{}]*)\}', graphics_body):
                    if not value or any(c in value for c in '\\\x00:$#{}*?[]') or Path(value).is_absolute():
                        raise ValueError('Unsafe or dynamic graphicspath')
                    directory = (cwd / value).resolve()
                    if not directory.is_relative_to(root):
                        raise ValueError('graphicspath escapes project')
                    directories.append(directory)
                graphics = tuple(directories)
                continue
            deferred = any(begin < match.start() < end for begin, end in macros)
            for name in body.split(','):
                name = name.strip()
                target, reason = resolve(name, command, graphics)
                row = {'file': path.relative_to(root).as_posix(), 'command': command, 'value': name,
                       'resolved': str(target) if target else None, 'required': not deferred}
                literal.append(row)
                if target is None:
                    row = {**row, 'reason': 'deferred_macro_dependency' if deferred else reason}
                    (unresolved if deferred or reason != 'not_found' else missing).append(row)
                elif target.is_relative_to(root):
                    project_bytes(target)
                    if command in {'input', 'include', 'documentclass', 'usepackage', 'LoadClass', 'RequirePackage'}:
                        pending.append((target, graphics))
    records = [files[name] for name in sorted(files)]
    return {'tex_path': relative_path(tex_path), 'working_directory': working_directory,'dependency_profile':build_profile,
        'tex_search_paths': paths, 'input_paths': [row['path'] for row in records], 'files': records,
        'bytes': sum(row['bytes'] for row in records), 'sha256': sha(encoded(records)),
        'literal_dependencies': literal, 'system_dependencies': [system[name] for name in sorted(system)],
        'font_dependencies': fonts, 'unresolved': unresolved, 'missing': missing,
        'coverage': 'Project-local reachable literal TeX/cls/sty plus direct system resolution; macro expansion, conditional execution, font selection and transitive system inputs require isolated runtime recorder checks. No TeX or generator was executed.'}
