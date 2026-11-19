"""Generate or check the release catalog from the installed MCP implementation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from paper_studio import __version__
from paper_studio.contracts import TOOLS
from paper_studio.gateway.protocol import SUPPORTED_VERSIONS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the existing catalog without writing")
    args = parser.parse_args()
    destination = Path(__file__).with_name("tools.schema.json")
    catalog = {
        "software_version": __version__,
        "transport": "stdio",
        "protocol_versions": list(SUPPORTED_VERSIONS),
        "tool_admission": "Discover with tools/list and check project authorization with paper_capabilities.",
        "tools": sorted(TOOLS, key=lambda tool: tool["name"]),
    }
    rendered = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if args.check:
        if destination.read_bytes() != rendered:
            parser.exit(1, "MCP tool catalog has drifted. Regenerate it from the installed source.\n")
        return
    destination.write_bytes(rendered)


if __name__ == "__main__":
    main()
