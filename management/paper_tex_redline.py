"""Generate a text-review TeX beside an existing manuscript."""
import argparse
import json
import sys

from paper_studio.tex_redline import generate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', required=True)
    parser.add_argument('--new', required=True, help='Existing new source, or - to read candidate UTF-8 text from stdin')
    parser.add_argument('--project-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--template')
    parser.add_argument('--input', dest='input_paths', action='append', help='One exact input path already present in the template')
    parser.add_argument('--latexdiff', default='latexdiff')
    parser.add_argument('--tex-bin')
    parser.add_argument('--expected-old-sha256')
    parser.add_argument('--expected-new-sha256')
    parser.add_argument('--expected-output-sha256')
    parser.add_argument('--preamble-input', dest='preamble_inputs', action='append', default=[])
    args = parser.parse_args()
    if args.input_paths and len(args.input_paths) != 1:
        parser.error('Select one input fragment per text review')
    args.input_path = args.input_paths[0] if args.input_paths else None
    del args.input_paths
    if args.new == '-':
        args.new_content = sys.stdin.buffer.read()
    try:
        result = generate(**vars(args))
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
