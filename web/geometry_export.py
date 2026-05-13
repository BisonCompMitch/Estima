"""3D geometry extraction for the BisonScope web viewer.

Returns plain dicts (JSON-serialisable) that the Three.js frontend can render.

DXF output schema
-----------------
{
  "type": "dxf",
  "unit": "ft",
  "bounds": {"min": [x,y,z], "max": [x,y,z]},
  "segments":         [x1,y1,z1, x2,y2,z2, ...],   # ordinary geometry
  "framing_segments": [x1,y1,z1, x2,y2,z2, ...],   # CFS-layer lines
  "face_verts":       [x1,y1,z1, ...],              # 3DFACE triangles
  "face_indices":     [0,1,2, ...],
}

IFC output schema
-----------------
{
  "type": "ifc",
  "bounds": {"min": [x,y,z], "max": [x,y,z]},
  "element_types": {"IfcWall": {"color": "#7B8DBF"}, ...},
  "groups": {
    "IfcWall": {"verts": [x1,y1,z1,...], "faces": [0,1,2,...], "color": "#7B8DBF"},
    ...
  }
}
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

_MAX_TOTAL_VERTS_IFC = 300_000   # cap to keep JSON response manageable
_MAX_SEGMENTS_DXF = 100_000      # cap individual segment pairs

_IFC_ELEMENT_COLORS: dict[str, str] = {
    "IfcWall":              "#6B7FA8",
    "IfcWallStandardCase":  "#6B7FA8",
    "IfcCurtainWall":       "#A8C8D8",
    "IfcSlab":              "#B8956A",
    "IfcRoof":              "#9E5C38",
    "IfcColumn":            "#4A7FA8",
    "IfcBeam":              "#4A7FA8",
    "IfcMember":            "#E8C840",   # CFS framing — highlighted yellow
    "IfcPlate":             "#D4A820",
    "IfcDoor":              "#7A5814",
    "IfcWindow":            "#7EC8D3",
    "IfcStair":             "#888888",
    "IfcSpace":             "#22C55E",
    "IfcFoundation":        "#8B7355",
    "IfcFooting":           "#8B7355",
    "_default":             "#AAAAAA",
}


# ── public entry point ────────────────────────────────────────────────────────

def export_geometry(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext == ".dxf":
        return _export_dxf(path)
    if ext == ".ifc":
        return _export_ifc(path)
    raise ValueError(f"Unsupported file type: {ext}")


# ── DXF ──────────────────────────────────────────────────────────────────────

def _export_dxf(path: Path) -> dict[str, Any]:
    import ezdxf
    from bison_scope_estimator.parsers.dxf_parser import (
        _detect_dxf_unit,
        _entity_is_3d_member,
        _entity_layer_name,
        _iter_dxf_entities,
        _looks_like_framing_layer,
    )

    doc = ezdxf.readfile(str(path))
    unit = _detect_dxf_unit(doc) or "ft"
    msp = doc.modelspace()

    segs: list[float] = []
    framing_segs: list[float] = []
    face_verts: list[float] = []
    face_idxs: list[int] = []

    bmin = [math.inf, math.inf, math.inf]
    bmax = [-math.inf, -math.inf, -math.inf]

    def _upd(x: float, y: float, z: float) -> None:
        if x < bmin[0]: bmin[0] = x
        if y < bmin[1]: bmin[1] = y
        if z < bmin[2]: bmin[2] = z
        if x > bmax[0]: bmax[0] = x
        if y > bmax[1]: bmax[1] = y
        if z > bmax[2]: bmax[2] = z

    for entity in _iter_dxf_entities(msp):
        kind = entity.dxftype().upper()
        is_framing = _looks_like_framing_layer(_entity_layer_name(entity))
        target = framing_segs if is_framing else segs
        total_pairs = (len(segs) + len(framing_segs)) // 6
        if total_pairs >= _MAX_SEGMENTS_DXF:
            break

        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            sx, sy, sz = float(s.x), float(s.y), float(getattr(s, "z", 0.0))
            ex, ey, ez = float(e.x), float(e.y), float(getattr(e, "z", 0.0))
            if _entity_is_3d_member(entity, kind):
                target = framing_segs
            target += [sx, sy, sz, ex, ey, ez]
            _upd(sx, sy, sz); _upd(ex, ey, ez)

        elif kind == "ARC":
            cx = float(entity.dxf.center.x)
            cy = float(entity.dxf.center.y)
            cz = float(getattr(entity.dxf.center, "z", 0.0))
            r = float(entity.dxf.radius)
            a0 = math.radians(float(entity.dxf.start_angle))
            a1 = math.radians(float(entity.dxf.end_angle))
            sweep = (a1 - a0) % (2.0 * math.pi)
            if sweep < 1e-9:
                sweep = 2.0 * math.pi
            n_seg = max(8, int(sweep / math.pi * 16))
            prev = None
            for k in range(n_seg + 1):
                ang = a0 + sweep * k / n_seg
                pt = (cx + r * math.cos(ang), cy + r * math.sin(ang), cz)
                _upd(*pt)
                if prev is not None:
                    target += list(prev) + list(pt)
                prev = pt

        elif kind == "CIRCLE":
            cx = float(entity.dxf.center.x)
            cy = float(entity.dxf.center.y)
            cz = float(getattr(entity.dxf.center, "z", 0.0))
            r = float(entity.dxf.radius)
            n_seg = 32
            prev = None
            for k in range(n_seg + 1):
                ang = 2.0 * math.pi * k / n_seg
                pt = (cx + r * math.cos(ang), cy + r * math.sin(ang), cz)
                _upd(*pt)
                if prev is not None:
                    target += list(prev) + list(pt)
                prev = pt

        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            pts = _dxf_poly_pts(entity, kind)
            if len(pts) >= 2:
                poly_target = framing_segs if _entity_is_3d_member(entity, kind) else target
                for i in range(len(pts) - 1):
                    p0 = pts[i] + (0.0,) if len(pts[i]) < 3 else pts[i]
                    p1 = pts[i + 1] + (0.0,) if len(pts[i + 1]) < 3 else pts[i + 1]
                    poly_target += list(p0) + list(p1)
                    _upd(*p0[:3]); _upd(*p1[:3])

        elif kind == "3DFACE":
            _add_3dface(entity, face_verts, face_idxs, bmin, bmax)

    bounds = _safe_bounds(bmin, bmax)
    return {
        "type": "dxf",
        "unit": unit,
        "bounds": bounds,
        "segments": segs,
        "framing_segments": framing_segs,
        "face_verts": face_verts,
        "face_indices": face_idxs,
    }


def _dxf_poly_pts(entity: object, kind: str) -> list[tuple]:
    if kind == "LWPOLYLINE":
        try:
            return [tuple(p[:2]) for p in entity.get_points("xy")]
        except Exception:
            return []
    else:
        try:
            return [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
        except Exception:
            return []


def _add_3dface(entity: object, verts: list[float], idxs: list[int], bmin: list, bmax: list) -> None:
    raw = [
        (float(entity.dxf.vtx0.x), float(entity.dxf.vtx0.y), float(entity.dxf.vtx0.z)),
        (float(entity.dxf.vtx1.x), float(entity.dxf.vtx1.y), float(entity.dxf.vtx1.z)),
        (float(entity.dxf.vtx2.x), float(entity.dxf.vtx2.y), float(entity.dxf.vtx2.z)),
        (float(entity.dxf.vtx3.x), float(entity.dxf.vtx3.y), float(entity.dxf.vtx3.z)),
    ]
    unique: list[tuple] = []
    for pt in raw:
        if not unique or pt != unique[-1]:
            unique.append(pt)
    if unique and unique[0] == unique[-1]:
        unique.pop()
    if len(unique) < 3:
        return
    base = len(verts) // 3
    for pt in unique:
        verts += list(pt)
        if pt[0] < bmin[0]: bmin[0] = pt[0]
        if pt[1] < bmin[1]: bmin[1] = pt[1]
        if pt[2] < bmin[2]: bmin[2] = pt[2]
        if pt[0] > bmax[0]: bmax[0] = pt[0]
        if pt[1] > bmax[1]: bmax[1] = pt[1]
        if pt[2] > bmax[2]: bmax[2] = pt[2]
    for k in range(1, len(unique) - 1):
        idxs += [base, base + k, base + k + 1]


# ── IFC ──────────────────────────────────────────────────────────────────────

def _export_ifc(path: Path) -> dict[str, Any]:
    try:
        import ifcopenshell
        import ifcopenshell.geom as geom
    except ImportError:
        return {
            "type": "ifc",
            "error": "ifcopenshell geometry module not available — install with: pip install ifcopenshell",
            "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]},
            "element_types": {},
            "groups": {},
        }

    ifc_file = ifcopenshell.open(str(path))

    settings = geom.settings()
    settings.set("use-world-coords", True)
    settings.set("weld-vertices", True)

    # Collect meshes grouped by IFC class, respecting total-vertex budget.
    groups: dict[str, dict[str, Any]] = {}
    bmin = [math.inf, math.inf, math.inf]
    bmax = [-math.inf, -math.inf, -math.inf]
    total_verts = 0

    try:
        iterator = geom.iterator(settings, ifc_file, 1)
        has_more = iterator.initialize()
    except Exception:
        has_more = False

    while has_more:
        shape = iterator.get()
        element = ifc_file.by_id(shape.id)
        if element is None:
            has_more = iterator.next()
            continue

        etype = element.is_a()
        color = _ifc_color(etype)

        raw_verts = list(shape.geometry.verts)
        raw_faces = list(shape.geometry.faces)

        if not raw_verts or not raw_faces:
            has_more = iterator.next()
            continue

        if total_verts + len(raw_verts) // 3 > _MAX_TOTAL_VERTS_IFC:
            has_more = iterator.next()
            continue

        total_verts += len(raw_verts) // 3

        if etype not in groups:
            groups[etype] = {"verts": [], "faces": [], "color": color}

        offset = len(groups[etype]["verts"]) // 3
        groups[etype]["verts"].extend(raw_verts)
        groups[etype]["faces"].extend(f + offset for f in raw_faces)

        for i in range(0, len(raw_verts), 3):
            x, y, z = raw_verts[i], raw_verts[i + 1], raw_verts[i + 2]
            if x < bmin[0]: bmin[0] = x
            if y < bmin[1]: bmin[1] = y
            if z < bmin[2]: bmin[2] = z
            if x > bmax[0]: bmax[0] = x
            if y > bmax[1]: bmax[1] = y
            if z > bmax[2]: bmax[2] = z

        has_more = iterator.next()

    return {
        "type": "ifc",
        "bounds": _safe_bounds(bmin, bmax),
        "element_types": {k: {"color": v["color"]} for k, v in groups.items()},
        "groups": groups,
    }


def _ifc_color(etype: str) -> str:
    return _IFC_ELEMENT_COLORS.get(etype, _IFC_ELEMENT_COLORS["_default"])


def _safe_bounds(bmin: list, bmax: list) -> dict:
    if not math.isfinite(bmin[0]):
        return {"min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0]}
    return {
        "min": [round(v, 6) for v in bmin],
        "max": [round(v, 6) for v in bmax],
    }
