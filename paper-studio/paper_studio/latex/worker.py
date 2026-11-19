"""Isolated TeX worker with bounded process output and build diagnostics.

Adapted from the maintainers' earlier TeX and isolated worker adapters.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

WORK = Path("/work")
INPUT = Path("/input")
BIN = Path("/texlive/bin/x86_64-linux")

# This fixed hook changes only IEEEtran's split of a XeTeX font name from its
# trailing point size. The class's drop-cap font, height and placement remain.
IEEE_XETEX_DROPCAP_HOOK = r'''% WebCodex fixed compatibility hook: ieeetran_xetex_dropcap_v1
\RequirePackage{etoolbox}
\makeatletter
\def\paperstudio@fontnamewithoutsize#1 at #2\relax{#1}
\AtBeginDocument{%
  \@ifclassloaded{IEEEtran}{%
    \patchcmd{\IEEEPARstart}
      {\expandafter\@IEEEPARstartGETFIRSTWORD\@IEEEPARstartFONTNAMESPACE\relax}
      {\expandafter\paperstudio@fontnamewithoutsize\@IEEEPARstartFONTNAMESPACE at \relax}
      {\typeout{PAPER_STUDIO_COMPAT ieeetran_xetex_dropcap_v1 applied}}%
      {\PackageError{paper-studio-compat}{IEEEtran drop-cap font lookup differs}{Compatibility hook refused.}}%
  }{\PackageError{paper-studio-compat}{IEEEtran class was not loaded}{Compatibility hook refused.}}%
}
\makeatother
'''
COMPATIBILITY_HOOKS = {'ieeetran_xetex_dropcap_v1': IEEE_XETEX_DROPCAP_HOOK}


def compatibility_options(request):
    hooks = request.get('compatibility_hooks', [])
    if not isinstance(hooks, list) or len(hooks) > 1 or any(not isinstance(value, str) or value not in COMPATIBILITY_HOOKS for value in hooks):
        raise ValueError('Only registered fixed TeX compatibility hooks are allowed')
    if hooks and request.get('build_profile', 'xelatex') != 'xelatex':
        raise ValueError('The IEEEtran XeTeX compatibility hook requires XeLaTeX')
    return hooks


def prepare_compatibility(request, source, cwd):
    hooks = compatibility_options(request)
    records = []
    for identifier in hooks:
        path = WORK / ('paper_studio_' + identifier + '.tex')
        raw = COMPATIBILITY_HOOKS[identifier].encode('utf-8')
        with path.open('xb') as stream:
            stream.write(raw)
        path.chmod(0o444)
        records.append({'id': identifier, 'path': path.name, 'namespace_path': str(path),
                        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'content': raw.decode('utf-8'),
                        'scope': 'fixed runtime macro compatibility; original TeX, fonts and class bytes unchanged'})
    if not records:
        return records, None, None
    try:
        relative_source = source.relative_to(cwd).as_posix()
    except ValueError:
        raise ValueError('Compatibility pre-input requires the TeX entry beneath its selected working directory') from None
    if any(char in relative_source for char in '\\{}%#$&~^"\r\n'):
        raise ValueError('Compatibility pre-input requires a literal safe TeX filename')
    pretex = ''.join(r'\input{' + row['path'] + '}' for row in records)
    return records, pretex, pretex + r'\input{' + relative_source + '}'


def verify_compatibility(records):
    for row in records:
        if hashlib.sha256((WORK / row['path']).read_bytes()).hexdigest() != row['sha256']:
            raise RuntimeError('Fixed TeX compatibility hook changed during compilation')


def directory_option(value):
    """A trusted recipe may name bounded snapshot directories, never TeX syntax."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("TeX directory must be a bounded relative POSIX path")
    if value == ".":
        return value
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or value.startswith("-")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in "\\\x00:$#{}*?[]~\r\n\t")):
        raise ValueError("Unsafe or dynamic TeX directory")
    return value


def directory_options(request):
    """Validate internal layout fields without expanding the immutable snapshot."""
    if "working_directory" in request:
        directory_option(request["working_directory"])
    paths = request.get("tex_search_paths", [])
    if not isinstance(paths, list) or len(paths) > 16:
        raise ValueError("tex_search_paths must contain at most sixteen relative directories")
    values = [directory_option(value) for value in paths]
    if len(set(values)) != len(values):
        raise ValueError("Duplicate TeX search directories are forbidden")
    return values


def snapshot_directory(root, value):
    relative = directory_option(value)
    target = root if relative == "." else root / relative
    if target.resolve() != target or not target.is_dir() or not target.is_relative_to(root):
        raise ValueError("TeX directory is missing, aliased or outside the immutable snapshot")
    return target


def run(command, cwd=INPUT):
    print("COMMAND", json.dumps(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, stdin=subprocess.DEVNULL)


def bibliography_backend(work, name, requested='auto'):
    if requested not in {'auto','bibtex','biber'}:
        raise ValueError('Unsupported bibliography engine')
    aux=work/(name+'.aux')
    backend=('biber' if (work/(name+'.bcf')).exists() else
             'bibtex' if aux.exists() and '\\bibdata{' in aux.read_text(errors='replace') else None)
    if backend is not None and requested!='auto' and backend!=requested:
        raise ValueError('Manuscript bibliography format differs from the trusted '+requested+' recipe')
    return backend


def prepare_bibliography_inputs(work, name, dependencies, backend):
    """Normalize derived bibliography metadata, never the immutable TeX inputs.

    BibTeX/Biber run beside their output metadata in /work. Literal paths in
    that metadata originated at the main TeX cwd and may contain a legitimate
    parent directory. Resolve only preflight-observed inputs and give each
    exact source identity a unique safe runtime name instead of weakening
    openin_any or guessing a basename.
    """
    if backend is None:
        return {'phase':'before_bibliography_tools','backend':None,'files':[],'metadata':[]}
    allowed={kind:{} for kind in ['bibliography','addbibresource','bibliographystyle']}
    for row in dependencies['literal_dependencies']:
        kind=row['command']
        if kind not in allowed or not row.get('resolved'):
            continue
        literal=row['value'].strip();suffix='.bst' if kind=='bibliographystyle' else '.bib'
        keys={literal,literal[:-len(suffix)] if literal.endswith(suffix) else literal+suffix}
        for key in keys:
            previous=allowed[kind].get(key)
            if previous is not None and previous!=row['resolved']:
                raise ValueError('Ambiguous preflight bibliography identity')
            allowed[kind][key]=row['resolved']
    runtime=work/'bibliography-inputs';runtime.mkdir()
    files={};metadata=[]
    def alias(literal,kind):
        path=allowed[kind].get(literal.strip())
        if path is None:
            raise ValueError('Bibliography metadata names an input absent from dependency preflight: '+literal)
        source=Path(path)
        if source.resolve()!=source or not (source.is_relative_to(INPUT) or source.is_relative_to('/texlive')):
            raise ValueError('Bibliography input escapes the immutable snapshot or trusted TeX Live')
        raw=source.read_bytes()
        if len(raw)>32*1024**2:
            raise ValueError('Bibliography input exceeds its bounded byte limit')
        digest=hashlib.sha256(raw).hexdigest();suffix='.bst' if kind=='bibliographystyle' else '.bib'
        identity=hashlib.sha256(json.dumps([str(source),digest],separators=(',',':')).encode()).hexdigest()
        filename='paperstudio_'+identity+suffix
        if filename not in files:
            target=runtime/filename
            with target.open('xb') as stream:stream.write(raw)
            target.chmod(0o444)
            files[filename]={'literal':literal,'kind':kind,'source':str(source),
                'snapshot_path':source.relative_to(INPUT).as_posix() if source.is_relative_to(INPUT) else None,
                'runtime_path':target.relative_to(work).as_posix(),'sha256':digest,'bytes':len(raw)}
        return filename
    def rewrite(path,raw,changed):
        if len(raw)>8*1024**2:
            raise ValueError('Bibliography control metadata exceeds its bounded byte limit')
        with path.open('wb') as stream:stream.write(changed)
        metadata.append({'path':path.relative_to(work).as_posix(),
                         'original_sha256':hashlib.sha256(raw).hexdigest(),
                         'normalized_sha256':hashlib.sha256(changed).hexdigest()})
    if backend=='bibtex':
        pending=[work/(name+'.aux')];seen=set()
        while pending:
            path=pending.pop()
            if path in seen:continue
            if len(seen)>=64 or path.resolve()!=path or not path.is_relative_to(work):
                raise ValueError('Unsafe or excessive bibliography auxiliary input')
            seen.add(path);raw=path.read_bytes()
            if len(raw)>8*1024**2:raise ValueError('Bibliography auxiliary input exceeds its limit')
            text=raw.decode('utf-8')
            for value in re.findall(r'\\@input\{([^{}\r\n]*)\}',text):
                directory_option(value)
                nested=work/value
                if nested.suffix!='.aux':raise ValueError('Only bounded auxiliary inputs may be followed')
                pending.append(nested)
            def substitute(match):
                command,body=match.groups();kind='bibliography' if command=='bibdata' else 'bibliographystyle'
                names=[value.strip() for value in body.split(',')]
                if not names or any(not value for value in names):raise ValueError('Empty bibliography metadata input')
                values=[Path(alias(value,kind)).stem for value in names]
                return '\\'+command+'{'+','.join(values)+'}'
            changed=re.sub(r'\\(bibdata|bibstyle)\{([^{}\r\n]*)\}',substitute,text).encode('utf-8')
            rewrite(path,raw,changed)
    elif backend=='biber':
        path=work/(name+'.bcf');raw=path.read_bytes()
        if len(raw)>8*1024**2 or b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
            raise ValueError('Unsafe or excessive Biber control metadata')
        tree=ET.fromstring(raw)
        if tree.tag.startswith('{'):
            ET.register_namespace('bcf',tree.tag[1:].split('}',1)[0])
        for node in tree.iter():
            if node.tag.rsplit('}',1)[-1]=='datasource':
                if node.get('type','file')!='file' or node.get('glob','false')!='false' or not node.text:
                    raise ValueError('Only preflight-observed literal Biber files are allowed')
                node.text='bibliography-inputs/'+alias(node.text,'addbibresource')
        rewrite(path,raw,ET.tostring(tree,encoding='utf-8',xml_declaration=True))
    else:
        raise ValueError('Unsupported bibliography backend')
    for variable in ['BIBINPUTS','BSTINPUTS']:
        os.environ[variable]=str(runtime)+':'+os.environ.get(variable,'')
    result={'phase':'before_bibliography_tools','backend':backend,'files':list(files.values()),'metadata':metadata}
    with (work/'bibliography-input-map.json').open('x') as stream:json.dump(result,stream,indent=2)
    (work/'bibliography-input-map.json').chmod(0o444)
    return result


def verify_bibliography_inputs(work,record):
    for row in record['files']:
        path=work/row['runtime_path']
        if path.resolve()!=path or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise RuntimeError('Normalized bibliography input changed during compilation')


def preflight(source, *, cwd=None, tex_search_paths=()):
    """Follow reachable literal TeX inputs using the selected main file's cwd.

    TeX's ordinary input/include paths are relative to the engine cwd, including
    when they occur inside an included file. Never scan unrelated candidates.
    """
    missing, unresolved, literal = [], [], []
    cwd = source.parent if cwd is None else cwd
    pending, visited, allowed = [(source, ())], set(), {source}
    while pending:
        path, graphics = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        text = re.sub(r"(?<!\\)%[^\n]*", "", path.read_text(encoding="utf-8", errors="replace"))
        graphic_pattern = r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})*\s*)\}"
        # Only explicit literal directory lists extend graphics lookup. Dynamic
        # TeX is not interpreted here, and can never expand the recorder allowlist.
        for occurrence in re.finditer(r"\\graphicspath\b", text):
            if re.match(graphic_pattern, text[occurrence.start():]) is None:
                raise ValueError("Dynamic or malformed graphicspath is unsupported")
        commands = re.compile(graphic_pattern + r"|\\(input|include|includegraphics|bibliography|bibliographystyle|addbibresource|documentclass|usepackage)(?:\[[^\]]*\])?\s*\{([^}]*)\}")
        for match in commands.finditer(text):
            graphic_body, command, body = match.groups()
            if graphic_body is not None:
                directories = []
                for name in re.findall(r"\{([^{}]*)\}", graphic_body):
                    if not name or any(c in name for c in "\\#{}$:\x00") or Path(name).is_absolute():
                        raise ValueError("Unsafe literal graphicspath directory")
                    directory = (cwd / name).resolve()
                    if not directory.is_relative_to(INPUT):
                        raise ValueError("Literal graphicspath escapes snapshot")
                    if directory not in directories:
                        directories.append(directory)
                graphics = tuple(directories)
                continue
            if any(c in body for c in "\\#{}$"):
                unresolved.append({"file": str(path.relative_to(INPUT)), "command": command, "value": body})
                continue
            for name in body.split(","):
                name = name.strip()
                if not name or Path(name).is_absolute() or ":" in name:
                    raise ValueError("Unsafe literal TeX dependency: " + name)
                normalized = (cwd / name).resolve()
                if not normalized.is_relative_to(INPUT):
                    raise ValueError("Literal TeX dependency escapes snapshot: " + name)
                extensions = {"input": ["", ".tex"], "include": [".tex"], "includegraphics": ["", ".pdf", ".png", ".jpg", ".eps"],
                              "bibliography": [".bib"], "bibliographystyle": [".bst"], "addbibresource": [""], "documentclass": [".cls"], "usepackage": [".sty"]}[command]
                found = None
                if command == "includegraphics":
                    for directory in (cwd, *graphics, *tex_search_paths):
                        for ext in extensions:
                            filename = name if ext and name.endswith(ext) else name + ext
                            candidate = (directory / filename).resolve()
                            if not candidate.is_relative_to(INPUT):
                                raise ValueError("Literal graphics dependency escapes snapshot")
                            if candidate.is_file():
                                found = str(candidate)
                                break
                        if found:
                            break
                for ext in extensions:
                    if found:
                        break
                    filename = name if ext and name.endswith(ext) else name + ext
                    for directory in (cwd, *tex_search_paths):
                        candidate = (directory / filename).resolve()
                        if not candidate.is_relative_to(INPUT):
                            continue
                        if candidate.is_file() and (tex_search_paths or command not in {"documentclass", "usepackage", "bibliographystyle"}):
                            found = str(candidate)
                            break
                    if found:
                        break
                    if command in {"documentclass", "usepackage", "bibliographystyle", "input"}:
                        probe = subprocess.run([str(BIN / "kpsewhich"), filename], cwd=cwd, capture_output=True, text=True)
                        if probe.returncode == 0 and probe.stdout.strip():
                            resolved = Path(probe.stdout.strip())
                            resolved = (cwd / resolved).resolve() if not resolved.is_absolute() else resolved.resolve()
                            if not resolved.is_relative_to(INPUT) and not resolved.is_relative_to("/texlive"):
                                raise ValueError("kpsewhich dependency escapes allowed roots")
                            found = str(resolved)
                            break
                literal.append({"file": str(path.relative_to(INPUT)), "command": command, "value": name, "resolved": found})
                if not found:
                    missing.append(name)
                elif Path(found).is_relative_to(INPUT):
                    allowed.add(Path(found))
                    if command in {"input", "include"}:
                        pending.append((Path(found), graphics))
    result = {"literal_dependencies": literal, "missing": missing, "dynamic_dependencies": unresolved,
              "engine_cwd": str(cwd), "reachable_tex_files": sorted(str(p.relative_to(INPUT)) for p in visited),
              "tex_search_paths": [str(p) for p in tex_search_paths],
              "allowed_snapshot_inputs": sorted(str(p) for p in allowed),
              "coverage": "reachable literal commands only; dynamic TeX remains subject to recorder and namespace checks"}
    (WORK / "dependency_preflight.json").write_text(json.dumps(result, indent=2))
    if missing:
        raise ValueError("Missing TeX dependencies: " + ", ".join(missing))
    return result


def recorder_check(cwd, explicit_inputs):
    records = []
    allowed = [cwd, WORK, Path("/texlive"), Path("/fonts/windows"), Path("/usr/share/fonts"), Path("/usr/share/fontconfig")]
    explicit = {Path(p) for p in explicit_inputs}
    for recorder in WORK.glob("*.fls"):
        recorder_cwd = cwd
        for line in recorder.read_text(errors="replace").splitlines():
            if line.startswith("PWD "):
                recorder_cwd = Path(line[4:]).resolve()
                if recorder_cwd != cwd:
                    raise RuntimeError("TeX recorder cwd differs from selected main directory")
                continue
            if not line.startswith(("INPUT ", "OUTPUT ")):
                continue
            kind, value = line.split(" ", 1)
            path = Path(value)
            path = (recorder_cwd / path).resolve() if not path.is_absolute() else path.resolve()
            roots = [WORK] if kind == "OUTPUT" else allowed
            if not any(path == root or root in path.parents for root in roots) and not (kind == "INPUT" and path in explicit):
                raise RuntimeError("TeX recorder path outside allowed roots: " + str(path))
            records.append({"kind": kind, "path": str(path)})
    if not records:
        raise RuntimeError("Required TeX recorder output missing")
    (WORK / "recorder.json").write_text(json.dumps(records, indent=2))
    return len(records)


def main(request):
    profile = request.get("build_profile", "xelatex")
    bibliography=request.get('bibliography_engine','auto')
    if bibliography not in {'auto','bibtex','biber'}:
        raise ValueError('Unsupported bibliography engine')
    required = [profile, "kpsewhich", "bibtex", "biber"] + (["latexmk"] if request.get("use_latexmk", True) else [])
    for tool in required:
        if not (BIN / tool).is_file():
            raise RuntimeError("Installed TeX dependency missing: " + tool)
    root = "/texlive"
    dist = root + "/texmf-dist"
    var = root + "/texmf-var"
    source = (INPUT / request["tex_path"]).resolve()
    if not source.is_relative_to(INPUT) or not source.is_file():
        raise FileNotFoundError("TeX main file missing or outside snapshot")
    search_paths = [snapshot_directory(INPUT, value) for value in directory_options(request)]
    cwd = snapshot_directory(INPUT, request["working_directory"]) if "working_directory" in request else source.parent
    os.environ.update({"TEXMFCNF": dist + "/web2c", "TEXMF": "{" + var + "," + dist + "}",
        "TEXMFDBS": "{!!" + var + ",!!" + dist + "}", "TEXMFHOME": "/work/unused-home",
        "TEXMFCONFIG": "/work/texmf-config", "TEXMFVAR": "/work/texmf-var",
        "TEXINPUTS": str(cwd) + ":/work:!!" + dist + "/tex//", "TEXPICTS": str(cwd), "BIBINPUTS": str(cwd),
        "BSTINPUTS": str(cwd) + ":!!" + dist + "/bibtex/bst//",
        "OPENTYPEFONTS": "!!" + dist + "/fonts/opentype//:/fonts/windows",
        "TTFONTS": "!!" + dist + "/fonts/truetype//:/fonts/windows",
        "openin_any": "p", "openout_any": "p", "shell_escape": "f",
        "MKTEXFMT": "0", "MKTEXPK": "0", "MKTEXTFM": "0", "MKTEXMF": "0", "MKTEXTEX": "0"})
    if search_paths:
        for variable in ("TEXINPUTS", "BIBINPUTS", "BSTINPUTS"):
            parts = os.environ[variable].split(":")
            extras = [str(directory) for directory in search_paths if str(directory) not in parts]
            os.environ[variable] = ":".join([parts[0], *extras, *parts[1:]])
    dependencies = preflight(source, cwd=cwd, tex_search_paths=search_paths)
    # Only explicitly named cross-directory dependencies extend search paths;
    # never add a recursive /input search or unrelated candidate directories.
    for variable, commands in {"TEXINPUTS": {"input", "include", "documentclass", "usepackage"},
                               "TEXPICTS": {"includegraphics"}, "BIBINPUTS": {"bibliography", "addbibresource"},
                               "BSTINPUTS": {"bibliographystyle"}}.items():
        directories = []
        for dependency in dependencies["literal_dependencies"]:
            resolved = dependency["resolved"]
            if dependency["command"] in commands and resolved and Path(resolved).is_relative_to(INPUT):
                directory = str(Path(resolved).parent)
                if directory != str(cwd) and directory not in directories:
                    directories.append(directory)
        if directories:
            parts = os.environ[variable].split(":")
            os.environ[variable] = ":".join([parts[0], *directories, *parts[1:]])
    name = source.stem
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("-"):
        raise ValueError("TeX job name must use ASCII letters, digits, dots, hyphens or underscores")
    flags = ["-no-shell-escape", "-no-parse-first-line", "-recorder", "-interaction=nonstopmode",
             "-halt-on-error", "-file-line-error", "-output-directory=/work"]
    compatibility, pretex, preinput = prepare_compatibility(request, source, cwd)
    if compatibility:
        flags += ['-jobname=' + name]
    engine_command=[str(BIN / profile), *flags, preinput if compatibility else str(source)]
    run(engine_command,cwd)
    backend=bibliography_backend(WORK,name,bibliography)
    bibliography_inputs=prepare_bibliography_inputs(WORK,name,dependencies,backend)
    if backend=='biber':
        run([str(BIN / "biber"), "--input-directory=/work", "--output-directory=/work", name], WORK)
    elif backend=='bibtex':
        run([str(BIN / "bibtex"), name], WORK)
    if request.get("use_latexmk", True):
        option = {"xelatex": "-xelatex", "pdflatex": "-pdf", "lualatex": "-lualatex"}[profile]
        # -norc disables system, user AND project latexmkrc. Engine strings are fixed.
        fixed_engine = str(BIN / profile) + " " + " ".join(flags) + (" %O %P" if compatibility else " %O %S")
        # Bibliography tools above use normalized, hash-bound inputs. latexmk
        # handles only subsequent TeX passes; it cannot rerun a bibliography
        # tool on metadata that TeX has rewritten to its original relative form.
        command = [str(BIN / "latexmk"), "-norc", "-bibtex-", option, "-outdir=/work", "-interaction=nonstopmode", "-halt-on-error",
                   "-" + profile + "=" + fixed_engine, str(source)]
        if compatibility:
            command.insert(-1, '-pretex=' + pretex)
        run(command, cwd)
    else:
        run(engine_command,cwd)
        run(engine_command,cwd)
    verify_compatibility(compatibility)
    verify_bibliography_inputs(WORK,bibliography_inputs)
    backend=bibliography_backend(WORK,name,bibliography)
    count = recorder_check(cwd, dependencies["allowed_snapshot_inputs"])
    log = (WORK / (name + ".log")).read_text(errors="replace")
    patterns = {"unresolved_citations": r"Citation .*undefined|undefined citations", "unresolved_references": r"Reference .*undefined|undefined references|Rerun to get cross-references",
                "label_conflicts": r"Label .*multiply defined|multiply-defined labels|destination with the same identifier|duplicate (?:destination|ignored)|ignoring duplicate destination|Object @\S+ already defined",
                "font_warnings": r"Missing character|font.*not found|font.*undefined|font.*substitut|fontspec.*Warning",
                "overfull_boxes": r"Overfull", "underfull_boxes": r"Underfull", "latex_warnings": r"LaTeX Warning:|Package .* Warning:"}
    diagnostics = {key: [line for line in log.splitlines() if re.search(pattern, line, re.I)] for key, pattern in patterns.items()}
    pdf = WORK / (name + ".pdf")
    if not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
        raise RuntimeError("No valid PDF produced")
    from pypdf import PdfReader
    reader = PdfReader(pdf)
    result = {"status": "completed", "engine": profile, "bibliography_engine":backend,
              "bibliography_recipe":bibliography,"pdf_path": pdf.name, "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
              "pages": len(reader.pages), "pdf_bytes": pdf.stat().st_size, "dependency_preflight": dependencies, "recorder_entries": count,
              "bibliography_input_map":bibliography_inputs,
              "bibliography_metadata_after_tex":[{'path':row['path'],
                  'sha256':hashlib.sha256((WORK/row['path']).read_bytes()).hexdigest()}
                  for row in bibliography_inputs['metadata']],
              "diagnostics": diagnostics, "visual_status": "not_reviewed", "typography": {"engine": profile, "font_warnings": diagnostics["font_warnings"]},
              "compatibility_hooks": compatibility}
    (WORK / "build.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(json.loads(Path(sys.argv[1]).read_text()))
