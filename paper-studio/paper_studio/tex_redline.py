"""Create one CJK text-review source using an installed latexdiff command."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from contextlib import contextmanager


MARKUP = r"""
% Paper Studio text-review markup. The source edition is unchanged.
\usepackage{accsupp}
\usepackage{xeCJKfntef}
\usepackage{xcolor}
\providecommand{\DIFadd}[1]{}
\providecommand{\DIFdel}[1]{}
\renewcommand{\DIFadd}[1]{{\color{blue}\CJKunderline[format=\color{blue}]{#1}}}
\renewcommand{\DIFdel}[1]{{\color{red}\CJKsout[format=\color{red}]{#1}}}
\providecommand{\DIFprotect}[1]{\mbox{#1}}
\providecommand{\DIFaddbegin}{}
\providecommand{\DIFaddend}{}
\providecommand{\DIFdelbegin}{}
\providecommand{\DIFdelend}{}
\providecommand{\DIFaddFL}[1]{\DIFadd{#1}}
\providecommand{\DIFdelFL}[1]{\DIFdel{#1}}
\providecommand{\DIFaddbeginFL}{}
\providecommand{\DIFaddendFL}{}
\providecommand{\DIFdelbeginFL}{}
\providecommand{\DIFdelendFL}{}
"""


def _source(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Each input must be an existing file inside the selected project')
    raw = path.read_bytes()
    return path, raw.decode('utf-8'), hashlib.sha256(raw).hexdigest()


def _active_text(text):
    """Mask ordinary TeX comments while retaining source character positions."""
    lines = []
    for line in text.splitlines(keepends=True):
        for match in re.finditer('%', line):
            preceding = re.search(r'\\*$', line[:match.start()]).group()
            if len(preceding) % 2 == 0:
                line = line[:match.start()] + ' ' * (len(line.rstrip('\r\n')) - match.start()) + line[len(line.rstrip('\r\n')):]
                break
        lines.append(line)
    return ''.join(lines)


def _group_end(text, position, opening='{', closing='}'):
    depth = 0
    while position < len(text):
        char = text[position]
        if char == '\\':
            position += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    raise ValueError('Unbalanced TeX group in text review')


def _tokens(text):
    """Preserve raw characters, keeping citations and math as complete tokens."""
    result = []
    position = 0
    argument_commands = {'cite', 'citep', 'citet', 'parencite', 'textcite', 'ref', 'eqref',
                         'autoref', 'cref', 'Cref', 'label', 'input', 'include', 'includegraphics',
                         'bibliography', 'bibliographystyle', 'url', 'href'}
    while position < len(text):
        start = position
        if text[position] == '%':
            found = text.find('\n', position)
            position = len(text) if found < 0 else found
        elif text.startswith(('\\[', '\\('), position) or text[position] == '$':
            opening = text[position:position+2] if text[position] == '\\' or text.startswith('$$', position) else '$'
            closing = {'\\[':'\\]', '\\(':'\\)'}.get(opening, opening)
            position += len(opening)
            end = re.search(r'(?<!\\)' + re.escape(closing), text[position:])
            if end is None:
                raise ValueError('Unclosed math expression in text review')
            position += end.end()
        elif text[position] == '\\':
            found = re.match(r'\\([A-Za-z@]+\*?|[^\n])', text[position:])
            if found is None:
                raise ValueError('Incomplete TeX command in text review')
            position += len(found.group())
            command = found.group(1).rstrip('*')
            if command in argument_commands:
                while position < len(text) and text[position] in '[{':
                    position = _group_end(text, position, '[', ']') if text[position] == '[' else _group_end(text, position)
        else:
            found = re.match(r'[A-Za-z0-9_]+|\s+', text[position:])
            position += len(found.group()) if found else 1
        result.append(text[start:position])
    return result


def _project_markup(marked, view):
    """Recover original bytes independently from the actual emitted markup."""
    output = []
    position = 0
    state = None
    while position < len(marked):
        boundary = re.match(r'\\DIF(add|del)(begin|end)\{\}', marked[position:])
        if boundary:
            kind, action = boundary.groups()
            if action == 'begin':
                if state is not None:
                    raise ValueError('Nested text-review boundary')
                state = kind
            else:
                if state != kind:
                    raise ValueError('Mismatched text-review boundary')
                state = None
            position += len(boundary.group())
            continue
        wrapper = re.match(r'\\DIF(add|del)\{', marked[position:])
        if wrapper:
            kind = wrapper.group(1)
            if state != kind:
                raise ValueError('Text-review wrapper is outside its change block')
            content_start = position + len(wrapper.group())
            end = _group_end(marked, content_start - 1)
            if (view == 'old' and kind == 'del') or (view == 'new' and kind == 'add'):
                content = marked[content_start:end-1]
                restored = []
                cursor = 0
                while True:
                    protected = content.find('\\DIFprotect{', cursor)
                    if protected < 0:
                        restored.append(content[cursor:])
                        break
                    restored.append(content[cursor:protected])
                    inner_start = protected + len('\\DIFprotect{')
                    inner_end = _group_end(content, inner_start - 1)
                    restored.append(content[inner_start:inner_end-1])
                    cursor = inner_end
                output.append(''.join(restored))
            position = end
            continue
        if state is None or (view == 'old' and state == 'del') or (view == 'new' and state == 'add'):
            output.append(marked[position])
        position += 1
    if state is not None:
        raise ValueError('Unclosed text-review boundary')
    return ''.join(output)


def _text_markup(old, new):
    """Diff only balanced inline edits and prove both source projections."""
    before, after = _tokens(old), _tokens(new)
    pieces = []
    if any(re.search(r'\\DIF(?:add|del|protect)', source) for source in (old,new)):
        raise ValueError('Compare clean sources without existing review commands')
    for kind, a, b, c, d in SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        old_part, new_part = ''.join(before[a:b]), ''.join(after[c:d])
        if kind == 'equal':
            pieces.append(new_part)
            continue
        for label, tokens in (('del', before[a:b]), ('add', after[c:d])):
            if not tokens:
                continue
            depth = 0
            for token in tokens:
                if token.startswith('%') or re.match(r'\\(?:begin|end|section|subsection|newcommand|input|include|label|bibliography)', token):
                    raise ValueError('A structural TeX change needs an explicit scoped review')
                depth += int(token == '{') - int(token == '}')
                if depth < 0:
                    raise ValueError('Text change crosses a TeX argument boundary')
            if depth:
                raise ValueError('Text change crosses a TeX argument boundary')
            raw = ''.join(tokens)
            pieces.append('\\DIF' + label + 'begin{}')
            for part in re.split(r'(\n[ \t]*\n(?:[ \t]*\n)*)', raw):
                if part:
                    protected = []
                    for token in _tokens(part):
                        if token.startswith(('$','\\[','\\(')) or re.match(r'\\(?:cite|citep|citet|ref|eqref|autoref|cref|Cref|parencite|textcite|url|href)(?:\*|\[|\{)', token):
                            token = '\\DIFprotect{' + token + '}'
                        protected.append(token)
                    pieces.append(part if not part.strip() else '\\DIF' + label + '{' + ''.join(protected) + '}')
            pieces.append('\\DIF' + label + 'end{}')
    marked = ''.join(pieces)
    if _project_markup(marked,'old') != old or _project_markup(marked,'new') != new:
        raise ValueError('Text review did not preserve both original source projections')
    return marked, {'old_projection_sha256':hashlib.sha256(old.encode()).hexdigest(),
                            'new_projection_sha256':hashlib.sha256(new.encode()).hexdigest(),
                            'source_projection_verified':True}


@contextmanager
def _output_lock(parent):
    """Serialize generation at one existing directory without a lock file."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = 'Local\\PaperTexReview-' + hashlib.sha256(str(parent).lower().encode()).hexdigest()
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'Cannot create review output mutex')
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 120000) in (0, 0x80)
            if not acquired:
                raise TimeoutError('Timed out waiting for this review output')
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _write_output(destination, content, expected_output_sha256):
    with _output_lock(destination.parent):
        if destination.is_symlink():
            raise ValueError('Review output cannot be a symbolic link')
        if destination.exists():
            previous = destination.read_bytes()
            if previous == content:
                return True
            if hashlib.sha256(previous).hexdigest() != expected_output_sha256:
                raise ValueError('Review output changed; read its current SHA before replacing it')
            descriptor, temporary = tempfile.mkstemp(prefix='.' + destination.name + '-', dir=destination.parent)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if destination.is_symlink() or destination.read_bytes() != previous:
                    raise ValueError('Review output changed during replacement')
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            if expected_output_sha256 is not None:
                raise ValueError('Expected previous review output is missing')
            with destination.open('xb') as stream:
                stream.write(content)
    return False


def generate(old, new, *, project_root, output, latexdiff='latexdiff',
             template=None, input_path=None, tex_bin=None,
             expected_old_sha256=None, expected_new_sha256=None,
             expected_output_sha256=None, preamble_inputs=(), new_content=None):
    """Compare two files, optionally inlining one selected input in a template.

    Dependencies keep their original project-relative paths. The caller runs
    the normal template toolchain from project_root after generating this file.
    Multi-input review can be prepared as one explicit complete document first.
    No version database or manuscript source is modified here.
    """
    root = Path(project_root).resolve(strict=True)
    old_path, old_text, old_sha = _source(root, old)
    if new == '-':
        if not isinstance(new_content, bytes):
            raise ValueError('Candidate stdin must be supplied as UTF-8 bytes')
        new_path, new_text = None, new_content.decode('utf-8')
        new_sha = hashlib.sha256(new_content).hexdigest()
    else:
        new_path, new_text, new_sha = _source(root, new)
    for expected, actual in ((expected_old_sha256, old_sha), (expected_new_sha256, new_sha)):
        if expected is not None and expected != actual:
            raise ValueError('Selected TeX source changed since it was read')
    if (template is None) != (input_path is None):
        raise ValueError('Fragment review requires both a template and one explicit input path')
    if any('\\DIFadd{' in text or '\\DIFdel{' in text for text in (old_text, new_text)):
        raise ValueError('Compare clean TeX sources, without existing revision markers')
    destination = Path(output)
    if not destination.is_absolute():
        destination = root / destination
    destination = destination.parent.resolve(strict=True) / destination.name
    if not destination.is_relative_to(root) or destination in (old_path, new_path):
        raise ValueError('Review output must be a separate file inside the selected project')
    if destination.is_symlink():
        raise ValueError('Review output cannot be a symbolic link')
    template_path = None
    template_sha = None
    if template is not None:
        template_path, template_text, template_sha = _source(root, template)
        if destination == template_path:
            raise ValueError('Review output cannot replace the template')
        include = re.compile(r'\\input\s*\{\s*' + re.escape(input_path) + r'\s*\}')
        matches = list(include.finditer(_active_text(template_text)))
        if len(matches) != 1:
            raise ValueError('The selected input must occur exactly once in the template')
        selected_path, _, selected_sha = _source(root, input_path)
        if (selected_path, selected_sha) not in ((old_path, old_sha), (new_path, new_sha)):
            raise ValueError('The selected template input must be the old or new text source')
    elif old_text == new_text and re.search(r'\\(?:input|include)\s*\{', _active_text(old_text)):
        raise ValueError('Main TeX is unchanged; select the actual changed input with template and input')
    explicit_command = Path(latexdiff)
    command = str(explicit_command) if explicit_command.is_absolute() and explicit_command.is_file() else shutil.which(str(latexdiff))
    warnings = ''
    proof = {}
    if command is None or new_path is None:
        marked, proof = _text_markup(old_text, new_text)
        algorithm = 'balanced_inline_text'
    else:
        command_path = Path(command)
        launch = [str(command_path)] if os.access(command_path, os.X_OK) else ['perl', str(command_path)]
        environment = os.environ.copy()
        if tex_bin is not None:
            environment['PATH'] = str(Path(tex_bin).resolve(strict=True)) + os.pathsep + environment.get('PATH', '')
        result = subprocess.run(launch + ['--encoding=utf8', '--type=UNDERLINE',
                                '--append-textcmd=newcommand', str(old_path.relative_to(root)), str(new_path.relative_to(root))],
                                cwd=root, env=environment, capture_output=True, text=True,
                                encoding='utf-8', timeout=120, check=False)
        if result.returncode:
            raise ValueError('latexdiff failed: ' + result.stderr[-4000:])
        warnings = result.stderr.strip()
        if 'Inconsistency in length' in warnings or 'Missing characters' in warnings:
            marked, proof = _text_markup(old_text, new_text)
            algorithm = 'balanced_inline_text'
            proof['external_parser_diagnostic_bytes'] = len(warnings.encode())
            warnings = 'latexdiff reported parser inconsistencies; both emitted text projections were independently verified'
        else:
            marked = re.sub(r'^%DIF DEL .*$', '%DIF DEL sha256 ' + old_sha, result.stdout, flags=re.MULTILINE)
            marked = re.sub(r'^%DIF ADD .*$', '%DIF ADD sha256 ' + new_sha, marked, flags=re.MULTILINE)
            algorithm = 'latexdiff'
    visible_body = marked.split('\\begin{document}', 1)[-1]
    if old_text != new_text and not re.search(r'\\DIF(?:add|del)\{', _active_text(visible_body)):
        raise ValueError('No visible text difference was produced; select the actual changed text source')
    if template is not None:
        if '\\begin{document}' in old_text or '\\begin{document}' in new_text:
            raise ValueError('The selected input must be a fragment, not a complete document')
        match = matches[0]
        marked = template_text[:match.start()] + marked + template_text[match.end():]
    if marked.count('\\begin{document}') != 1 or marked.count('\\end{document}') != 1:
        raise ValueError('Review needs one complete document or one fragment plus its template')
    explicit = []
    for value in preamble_inputs:
        path, _, _ = _source(root, value)
        if path.suffix != '.def':
            raise ValueError('Explicit preamble inputs are project .def dependencies')
        explicit.append('\\makeatletter\n\\input{' + path.relative_to(root).as_posix() + '}\n\\makeatother\n')
    marked = marked.replace('\\begin{document}', MARKUP + '\n' + ''.join(explicit) + '\\begin{document}', 1)
    # Recheck the two named inputs after the command completes before writing.
    if _source(root, old)[2] != old_sha or new_path is not None and _source(root, new)[2] != new_sha:
        raise ValueError('Selected source changed while preparing the text review')
    if template is not None and _source(root, template)[2] != template_sha:
        raise ValueError('Template changed while preparing the text review')
    content = marked.encode('utf-8')
    reused = _write_output(destination, content, expected_output_sha256)
    return {'output': str(destination), 'old_sha256': old_sha, 'new_sha256': new_sha,
            'sha256': hashlib.sha256(marked.encode()).hexdigest(),
            'scope': 'one_input_text' if template else 'document_text',
            'input_path': input_path, 'template': str(template_path) if template_path else None,
            'warnings': warnings, 'version_created': False, 'algorithm':algorithm,
            **proof,
            'reused_output': reused,
            'dependencies': 'unchanged template references; compile from selected project root'}
