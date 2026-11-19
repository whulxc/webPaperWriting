"""Read-only, standard-library OOXML shape inventory; no Office or shell calls.

inspect_deck retains every native shape, including blank shapes and groups.
page_slide_inventory provides bounded object pages; inspect_shape returns one
complete record, including native path commands. Geometry is OOXML evidence,
not a claim that a named curve represents measured sensor data.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import posixpath
import xml.etree.ElementTree as ET
import zipfile

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
EMU_PER_MM = 36000
IDENTITY = (1., 0., 0., 1., 0., 0.)
METADATA = {"nvGrpSpPr", "grpSpPr", "extLst"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _xml_tree(node):
    return {"tag": _local(node.tag), "attributes": dict(node.attrib),
            "text": node.text if node.text and node.text.strip() else None,
            "children": [_xml_tree(child) for child in node]}


def _resolve(part, target):
    return target.lstrip("/") if target.startswith("/") else posixpath.normpath(
        posixpath.join(posixpath.dirname(part), target))


def _package(path):
    from .document import read_package
    return read_package(path)


def _rels(data, part):
    name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if name not in data:
        return {}
    return {r.get("Id"): {"target": _resolve(part, r.get("Target", "")),
                            "type": r.get("Type", "")} for r in ET.fromstring(data[name])}


def _multiply(a, b):
    aa, ab, ac, ad, ae, af = a
    ba, bb, bc, bd, be, bf = b
    return (aa*ba+ac*bb, ab*ba+ad*bb, aa*bc+ac*bd, ab*bc+ad*bd,
            aa*be+ac*bf+ae, ab*be+ad*bf+af)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return [a*x+c*y+e, b*x+d*y+f]


def _transform(xfrm, group=False, path_size=None):
    if xfrm is None:
        return IDENTITY
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return IDENTITY
    x, y = float(off.get("x", 0)), float(off.get("y", 0))
    w, h = float(ext.get("cx", 0)), float(ext.get("cy", 0))
    lx = ly = 0.
    lw, lh = path_size if path_size is not None else (w, h)
    if group:
        co, ce = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
        if co is None or ce is None:
            raise ValueError("Group transform lacks chOff/chExt")
        lx, ly = float(co.get("x", 0)), float(co.get("y", 0))
        lw, lh = float(ce.get("cx", 0)), float(ce.get("cy", 0))
    sx, sy = w/lw if lw else 0., h/lh if lh else 0.
    scale = (sx, 0., 0., sy, x-lx*sx, y-ly*sy)
    angle = float(xfrm.get("rot", 0))/60000*math.pi/180
    fx = -1 if xfrm.get("flipH") in ("1", "true") else 1
    fy = -1 if xfrm.get("flipV") in ("1", "true") else 1
    rotation = (math.cos(angle)*fx, math.sin(angle)*fx,
                -math.sin(angle)*fy, math.cos(angle)*fy, 0., 0.)
    cx, cy = x+w/2, y+h/2
    return _multiply(_multiply((1., 0., 0., 1., cx, cy),
                              _multiply(rotation, (1., 0., 0., 1., -cx, -cy))), scale)


def _geometry(node, xfrm, parent):
    custom = node.find("p:spPr/a:custGeom", NS)
    preset = node.find("p:spPr/a:prstGeom", NS)
    if custom is None:
        return {"kind": "preset", "preset": preset.get("prst"), "native": _xml_tree(preset)} if preset is not None else {"kind": "none"}
    paths = []
    command_counts = Counter()
    for path in custom.findall("a:pathLst/a:path", NS):
        commands = [_xml_tree(c) for c in path]
        command_counts.update(_local(c.tag) for c in path)
        points = []
        try:
            size = (float(path.get("w")), float(path.get("h")))
            matrix = _multiply(parent, _transform(xfrm, path_size=size))
        except (TypeError, ValueError):
            matrix = None
        for ci, command in enumerate(path):
            for pi, pt in enumerate(command.findall("a:pt", NS)):
                point = {"command_index": ci, "point_index": pi, "x": pt.get("x"), "y": pt.get("y")}
                if matrix is not None:
                    try:
                        point["slide_mm"] = [v/EMU_PER_MM for v in _apply(matrix, float(pt.get("x")), float(pt.get("y")))]
                    except (TypeError, ValueError):
                        pass  # Formula-valued geometry stays available verbatim.
                points.append(point)
        payload = json.dumps(commands, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        paths.append({"attributes": dict(path.attrib), "command_count": len(commands),
                      "commands_sha256": _sha(payload), "commands": commands,
                      "control_point_count": len(points), "control_points": points,
                      "first_point_mm": points[0].get("slide_mm") if points else None,
                      "last_point_mm": points[-1].get("slide_mm") if points else None})
    return {"kind": "custom", "native": _xml_tree(custom), "paths": paths,
            "summary": {"path_count": len(paths), "command_counts": dict(command_counts),
                        "control_point_count": sum(p["control_point_count"] for p in paths)},
            "geometry_sha256": _sha(ET.tostring(custom, encoding="utf-8"))}


def _text(node, typ):
    paragraphs = node.findall("./p:txBody/a:p", NS)
    if typ == "graphicFrame":
        paragraphs = node.findall(".//a:p", NS)
    lines = []
    for paragraph in paragraphs:
        lines.append("".join((n.text or "") if _local(n.tag) == "t" else "\n"
                             for n in paragraph.iter() if _local(n.tag) in ("t", "br")))
    return "\n".join(lines)


def _walk(container, part, data, rels, parent=IDENTITY, ancestors=(), address=()):
    for index, node in enumerate(container):
        typ = _local(node.tag)
        if typ in METADATA:
            continue
        # Preserve wrappers and recurse through their selected/fallback branches;
        # branch identity is recorded rather than silently selecting one.
        identity = next((c for child in node if _local(child.tag).startswith("nv")
                         for c in child if _local(c.tag) == "cNvPr"), None)
        if identity is None and typ in ("AlternateContent", "Choice", "Fallback"):
            yield from _walk(node, part, data, rels, parent, ancestors, address+(index,))
            continue
        xfrm = node.find("p:grpSpPr/a:xfrm", NS) if typ == "grpSp" else node.find("p:spPr/a:xfrm", NS)
        if typ == "graphicFrame":
            xfrm = node.find("p:xfrm", NS)
        name, ident = (identity.get("name", ""), identity.get("id", "")) if identity is not None else ("", "")
        key = "/".join(map(str, address+(index,)))
        transform = _multiply(parent, _transform(xfrm))
        bbox = None
        if xfrm is not None:
            ext = xfrm.find("a:ext", NS)
            if ext is not None:
                w, h = float(ext.get("cx", 0)), float(ext.get("cy", 0))
                corners = [_apply(transform, x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
                xs, ys = [p[0]/EMU_PER_MM for p in corners], [p[1]/EMU_PER_MM for p in corners]
                bbox = [min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)]
        geometry = _geometry(node, xfrm, parent)
        line = node.find("p:spPr/a:ln", NS)
        record = {"key": key, "id": ident, "name": name, "type": typ,
                  "parent": ancestors[-1] if ancestors else None,
                  "ancestors": list(ancestors), "depth": len(ancestors),
                  "text": _text(node, typ), "bbox_mm": bbox,
                  "transform": _xml_tree(xfrm) if xfrm is not None else None,
                  "slide_matrix_emu": list(transform), "geometry": geometry,
                  "line": _xml_tree(line) if line is not None else None}
        if line is not None:
            record["line_style"] = {"attributes": dict(line.attrib)}
            for field in ("headEnd", "tailEnd", "prstDash"):
                item = line.find("a:"+field, NS)
                record["line_style"][field] = dict(item.attrib) if item is not None else None
        cnv = node.find("p:nvCxnSpPr/p:cNvCxnSpPr", NS)
        if typ == "cxnSp":
            record["connections"] = {side: dict(item.attrib) if item is not None else None
                                     for side in ("stCxn", "endCxn")
                                     for item in [cnv.find("a:"+side, NS) if cnv is not None else None]}
        if typ == "pic":
            media = []
            for blip in node.findall(".//a:blip", NS):
                rid = blip.get("{"+NS["r"]+"}embed") or blip.get("{"+NS["r"]+"}link")
                target = rels.get(rid, {}).get("target")
                content = data.get(target) if target else None
                media.append({"relationship_id": rid, "target": target,
                              "bytes": len(content) if content is not None else None,
                              "sha256": _sha(content) if content is not None else None})
            crop = node.find("p:blipFill/a:srcRect", NS)
            record["media"] = media
            record["crop"] = dict(crop.attrib) if crop is not None else None
        if typ == "graphicFrame":
            graphic_data = node.find("a:graphic/a:graphicData", NS)
            record["graphic_data"] = _xml_tree(graphic_data) if graphic_data is not None else None
        yield record
        if typ == "grpSp":
            group_matrix = _multiply(parent, _transform(xfrm, group=True))
            yield from _walk(node, part, data, rels, group_matrix,
                             ancestors+({"key": key, "id": ident, "name": name},), address+(index,))


def inspect_deck(path):
    """Return a complete immutable-source inventory in presentation slide order."""
    raw, data = _package(path)
    presentation = ET.fromstring(data["ppt/presentation.xml"])
    rels = _rels(data, "ppt/presentation.xml")
    slide_refs = presentation.findall("p:sldIdLst/p:sldId", NS)
    if not 1 <= len(slide_refs) <= 20:
        raise ValueError("Expected one to twenty slides")
    slides = []
    for index, ref in enumerate(slide_refs, 1):
        rid = ref.get("{"+NS["r"]+"}id")
        part = rels.get(rid, {}).get("target")
        if not part or not part.startswith("ppt/slides/"):
            raise ValueError("Invalid slide relationship")
        root = ET.fromstring(data[part])
        tree = root.find("p:cSld/p:spTree", NS)
        if tree is None:
            raise ValueError("Missing slide shape tree")
        objects = list(_walk(tree, part, data, _rels(data, part)))
        counts = dict(Counter(o["type"] for o in objects))
        slides.append({"slide": index, "part": part, "part_sha256": _sha(data[part]),
                       "object_count": len(objects), "leaf_object_count": sum(o["type"] != "grpSp" for o in objects),
                       "counts_by_type": counts, "text_object_count": sum(bool(o["text"]) for o in objects),
                       "custom_geometry_count": sum(o["geometry"]["kind"] == "custom" for o in objects),
                       "objects": objects})
    size = presentation.find("p:sldSz", NS)
    return {"schema": "paper-studio-pptx-inventory-v1", "input_path": str(Path(path).absolute()),
            "input_bytes": len(raw), "input_sha256": _sha(raw),
            "slide_size_emu": dict(size.attrib) if size is not None else None,
            "slide_count": len(slides), "media_entry_count": sum(n.startswith("ppt/media/") for n in data),
            "object_count": sum(s["object_count"] for s in slides), "slides": slides}


def _slide(inventory, slide):
    if type(slide) is not int or not 1 <= slide <= len(inventory["slides"]):
        raise ValueError("Slide must be an existing one-based integer")
    return inventory["slides"][slide-1]


def page_slide_inventory(inventory, slide, offset=0, limit=25, include_geometry=False):
    """Page over ALL object types in XML order, groups included. Max 100/page."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Use offset>=0 and limit=1..100")
    if type(include_geometry) is not bool:
        raise ValueError("include_geometry must be boolean")
    source = _slide(inventory, slide)
    records = copy.deepcopy(source["objects"][offset:offset+limit])
    if not include_geometry:
        for record in records:
            geom = record["geometry"]
            geom.pop("native", None)
            for path in geom.get("paths", []):
                path.pop("commands", None)
                path.pop("control_points", None)
            record.pop("graphic_data", None)
    end = offset+len(records)
    return {"slide": slide, "offset": offset, "limit": limit, "total": source["object_count"],
            "next_offset": end if end < source["object_count"] else None, "objects": records}


def inspect_shape(inventory, slide, *, shape_key=None, shape_name=None):
    """Select by structural key or exact UNIQUE name; ambiguous names fail."""
    if (shape_key is None) == (shape_name is None):
        raise ValueError("Supply exactly one of shape_key or shape_name")
    matches = [o for o in _slide(inventory, slide)["objects"]
               if (o["key"] == shape_key if shape_key is not None else o["name"] == shape_name)]
    if len(matches) != 1:
        raise ValueError("Selection must match exactly one object")
    return copy.deepcopy(matches[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = inspect_deck(args.input)
    if not args.output.is_absolute() or args.output.resolve() == args.input.resolve():
        raise ValueError("Output must be an explicit absolute path distinct from input")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(inventory, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: inventory[k] for k in ("input_sha256", "input_bytes", "slide_count", "object_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
