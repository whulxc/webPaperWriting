"""Inspect the current reachable manuscript, with hashes from its dependency plan.

Historical counts describe changes. They do not authorize or forbid editorial
changes. Runtime TeX diagnostics remain authoritative for macro expansion and
conditional execution that cannot be decided from literal source.
"""
from pathlib import Path
import re

from paper_studio.security.policy import fail, safe_read, sha
from paper_studio.latex.dependencies import _macro_ranges


_NUMBERING = re.compile(
    r'\\(?:renewcommand|newcommand|providecommand)\*?\s*\{\\the(figure|table)\}'
    r'\s*\{([^\n]*?)\\(?:arabic|Roman|roman|Alph|alph)\s*\{(?:figure|table)\}\s*\}'
)
_EVENTS = re.compile(
    r'\\(?P<input>input|include)\s*\{(?P<path>[^{}]+)\}'
    r'|\\begin\s*\{(?P<float>figure\*?|table\*?)\}'
    r'|\\(?P<appendix>appendix|appendices)\b|\\begin\s*\{(?P<appendices>appendices)\}'
    r'|\\(?P<label>label)\s*\{(?P<key>[^{}]+)\}'
    r'|\\(?P<ref>ref|eqref|pageref|autoref|cref|Cref|vref|Vref|nameref)\*?\s*\{(?P<refs>[^{}]+)\}'
    r'|\\end\s*\{(?P<end>document)\}'
)


def _literal_text(raw):
    text = re.sub(r'(?<!\\)%[^\n]*', '', raw.decode('utf-8'))
    # Definitions and verbatim examples do not execute their literal labels or floats.
    numbering = list(_NUMBERING.finditer(text))
    ranges = _macro_ranges(text)
    for start, end in reversed(ranges):
        if any(match.start() <= start < match.end() for match in numbering):
            continue
        text = text[:start] + re.sub(r'[^\n]', ' ', text[start:end + 1]) + text[end + 1:]
    text = re.sub(r'\\begin\{(verbatim\*?|Verbatim|lstlisting|minted)\}.*?\\end\{\1\}',
                  lambda match: re.sub(r'[^\n]', ' ', match.group()), text, flags=re.S)
    text = re.sub(r'\\verb\*?([^\w\s]).*?\1', lambda match: re.sub(r'[^\n]', ' ', match.group()), text)
    # Literal false branches are decidable without evaluating arbitrary TeX.
    tokens = list(re.finditer(r'\\(?:if[a-zA-Z]*|else|fi)\b', text))
    false_depth = None; depth = 0; start = None; ignored = []
    for token in tokens:
        command = token.group()[1:]
        if command.startswith('if'):
            depth += 1
            if command == 'iffalse' and false_depth is None:
                false_depth, start = depth, token.start()
        elif command == 'else' and false_depth == depth:
            ignored.append((start, token.end())); false_depth = start = None
        elif command == 'fi':
            if false_depth == depth:
                ignored.append((start, token.end())); false_depth = start = None
            depth = max(0, depth - 1)
    for start, end in reversed(ignored):
        text = text[:start] + re.sub(r'[^\n]', ' ', text[start:end]) + text[end:]
    return text


def manuscript_structure(project_root, tex_path, plan, baseline_counts=None, *,
                         baseline_source='historical_configuration_counts'):
    """Follow only dependency-resolved input/include edges in manuscript order.

    The output describes literal structure and explicit appendix/S-numbered
    regions. No directory discovery, history scan or second layout contract is
    used. Every inspected source is checked against the plan before parsing.
    """
    rows = {row['path']: row for row in plan['files'] if row['path'].endswith('.tex')}
    if tex_path not in rows:
        fail('REVISION_CONFLICT', 'Main TeX is absent from the dependency structure plan')
    root = Path(project_root)
    edges = {}
    for item in plan.get('literal_dependencies', []):
        resolved = item.get('resolved')
        if item.get('command') in {'input', 'include'} and item.get('required', True) and resolved:
            target = Path(resolved)
            if target.is_absolute() and target.is_relative_to(root):
                edges[(item['file'], item['command'], item['value'])] = target.relative_to(root).as_posix()
    counts = {'figure': 0, 'table': 0}
    sections = {part: dict(counts) for part in ('body', 'appendix')}
    section = {'figure': 'body', 'table': 'body'}
    appendix_mode = False
    sources = {}; labels = {}; references = []; floats = []; duplicates = []; deferred = []
    visits = 0; finished = False

    def visit(path, stack):
        nonlocal visits, finished, appendix_mode
        if finished:
            return
        visits += 1
        if path in stack or len(stack) >= 64 or visits > 2048:
            fail('MANUSCRIPT_STRUCTURE_INVALID', 'Recursive or excessive literal manuscript inputs', path=path)
        row = rows.get(path)
        if row is None:
            fail('DEPENDENCY_UNRESOLVED', 'A manuscript section is absent from its dependency plan', path=path)
        raw = safe_read(root, path, 4 * 1024**2)
        if sha(raw) != row['sha256']:
            fail('REVISION_CONFLICT', 'TeX source changed while inspecting manuscript structure', path=path)
        sources[path] = {'path': path, 'bytes': len(raw), 'sha256': row['sha256']}
        text = _literal_text(raw)
        original = raw.decode('utf-8')
        if re.search(r'\\if(?!false\b|true\b)[A-Za-z]*\b', text):
            deferred.append({'path':path,'reason':'conditional_execution_requires_TeX'})
        if any(re.search(r'\\(?:label|ref|eqref|cref|Cref)\b', original[start:end])
               for start,end in _macro_ranges(original)) or re.search(r'\\(?:externaldocument|zexternaldocument|csname)\b', text):
            deferred.append({'path':path,'reason':'macro_or_external_labels_require_TeX'})
        events = [(match.start(), 'source', match) for match in _EVENTS.finditer(text)]
        events += [(match.start(), 'numbering', match) for match in _NUMBERING.finditer(text)]
        for offset, kind, event in sorted(events, key=lambda item: item[0]):
            line = text.count('\n', 0, offset) + 1
            if kind == 'numbering':
                float_kind, prefix = event.groups()
                supplement = re.fullmatch(r'\s*[SA][.\s-]*', prefix) is not None
                section[float_kind] = 'appendix' if appendix_mode or supplement else 'body'
            elif event['end']:
                finished = True
                return
            elif event['input']:
                target = edges.get((path, event['input'], event['path'].strip()))
                if target is not None:
                    visit(target, (*stack, path))
                if finished:
                    return
            elif event['appendix'] or event['appendices']:
                appendix_mode = True
                section.update(figure='appendix', table='appendix')
            elif event['float']:
                float_kind = event['float'].rstrip('*')
                counts[float_kind] += 1; sections[section[float_kind]][float_kind] += 1
                floats.append({'kind': float_kind, 'section': section[float_kind], 'path': path, 'line': line})
            elif event['label']:
                key = event['key'].strip()
                if not key or any(char in key for char in '\\#$'):
                    continue
                origin = {'path': path, 'line': line}
                if key in labels:
                    duplicates.append({'label':key,'occurrences':[labels[key],origin]})
                labels[key] = origin
            elif event['ref']:
                for key in event['refs'].split(','):
                    key = key.strip()
                    if key and not any(char in key for char in '\\#$'):
                        references.append({'label': key, 'path': path, 'line': line})

    visit(tex_path, ())
    unresolved = [item for item in references if item['label'] not in labels]
    if duplicates and not deferred:
        fail('MANUSCRIPT_LABEL_CONFLICT', 'Duplicate literal manuscript label',
             label=duplicates[0]['label'],occurrences=duplicates[0]['occurrences'])
    if unresolved and not deferred:
        fail('MANUSCRIPT_REFERENCE_UNRESOLVED', 'A literal manuscript reference has no label', references=unresolved)
    before = counts if baseline_counts is None else baseline_counts
    return {'schema': 'paper-studio.manuscript-structure.v1',
            'source_tex_sha256': rows[tex_path]['sha256'], 'counts': counts, 'sections': sections,
            'baseline_counts': dict(before),
            'baseline_source': baseline_source if baseline_counts is not None else 'current_source',
            'change': {'from': dict(before), 'to': dict(counts),
                'delta': {kind: counts[kind] - before[kind] for kind in counts}},
            'source_files': [sources[path] for path in sorted(sources)], 'floats': floats,
            'labels': sorted(labels), 'unresolved_references': unresolved,
            'duplicate_labels':duplicates, 'deferred_validation':deferred,
            'coverage': 'Reachable literal TeX in input order, explicit appendix or S/A-numbering regions; '
                        'macro-generated labels, floats and undecidable branches require final TeX diagnostics'}


def _aux_group(text, offset):
    """Read one balanced TeX group without expanding auxiliary-file commands."""
    while offset < len(text) and text[offset].isspace():
        offset += 1
    if offset >= len(text) or text[offset] != '{':
        return None
    start = offset + 1; depth = 1; cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char == '\\':
            cursor += 2
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:cursor], cursor + 1
        if depth > 256:
            fail('MANUSCRIPT_STRUCTURE_INVALID', 'Auxiliary group nesting exceeds the parser budget')
        cursor += 1
    return None


def auxiliary_float_anchors(output, pdf_path, *, max_files=2048, max_bytes=64 * 1024**2):
    """Verify executed float captions across the final main and chapter auxiliaries.

    List-of-figures/tables entries bind displayed float numbers to destinations.
    Arbitrary newlabel aliases may inherit a previous float anchor while keeping
    a section number, so they are deliberately not treated as float declarations.
    """
    from paper_studio.security.policy import relpath
    output = Path(output)
    main = relpath(pdf_path).with_suffix('.aux').as_posix()
    if not (output / main).is_file():
        return {'status':'not_available', 'files':[], 'float_captions':[]}
    pending = [main]; seen = set(); files = []; captions = []; anchors = {}; total = 0
    commands = re.compile(r'(?m)^[ \t]*\\(?P<command>@writefile|@input)\b')
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        if len(seen) >= max_files:
            fail('INPUT_LIMIT', 'Auxiliary file graph exceeds the build file budget')
        seen.add(path)
        raw = safe_read(output, path, max_bytes)
        total += len(raw)
        if total > max_bytes:
            fail('INPUT_LIMIT', 'Auxiliary file graph exceeds the build output budget')
        files.append({'path':path, 'bytes':len(raw), 'sha256':sha(raw)})
        text = re.sub(r'(?<!\\)%[^\n]*', '', raw.decode('utf-8', errors='replace'))
        for command in commands.finditer(text):
            first = _aux_group(text, command.end())
            if first is None:
                continue
            name, cursor = first
            if command['command'] == '@input':
                member = relpath(name).as_posix()
                if not member.endswith('.aux'):
                    fail('MANUSCRIPT_STRUCTURE_INVALID', 'Chapter auxiliary input must identify an .aux file')
                pending.append(member)
                continue
            if name not in {'lof', 'lot'}:
                continue
            second = _aux_group(text, cursor)
            if second is None:
                continue
            content = second[0]
            contentsline = re.match(r'\s*\\contentsline\b', content)
            if contentsline is None:
                continue
            fields = []; cursor = contentsline.end()
            for _ in range(4):
                group = _aux_group(content, cursor)
                if group is None:
                    break
                value, cursor = group; fields.append(value)
            if len(fields) < 4 or fields[0].strip() not in {'figure', 'table'}:
                continue
            numberline = re.match(r'\s*\\numberline\b', fields[1])
            if numberline is None:
                continue
            number = _aux_group(fields[1], numberline.end())
            if number is None or not number[0].strip() or not fields[3].strip():
                continue
            row = {'kind':fields[0].strip(), 'number':number[0].strip(),
                   'page':fields[2].strip(), 'anchor':fields[3].strip(), 'aux_path':path}
            previous = anchors.get(row['anchor'])
            if previous and (previous['kind'],previous['number']) != (row['kind'],row['number']):
                fail('MANUSCRIPT_LABEL_CONFLICT', 'Distinct executed float numbers share one PDF destination',
                     anchor=row['anchor'], occurrences=[previous,row])
            anchors[row['anchor']] = row
            captions.append(row)
    return {'status':'verified', 'files':sorted(files,key=lambda row:row['path']),
            'float_captions':captions, 'coverage':'Executed numbered figure/table list entries; ordinary label aliases allowed'}
