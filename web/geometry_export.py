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
_SQ_M_TO_SQ_FT    = 10.7639104  # m² → sq ft

_SKIP_ETYPES = frozenset({
    "IfcSpace", "IfcOpeningElement", "IfcVirtualElement",
    "IfcAnnotation", "IfcGrid", "IfcSite",
    "IfcBuilding", "IfcBuildingStorey",
})

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


def _ifc_area_scale(ifc_file) -> float:
    """Returns multiplier from geometry-coordinate² to sq ft.

    ifcopenshell's geometry iterator applies the IFC length-unit scale
    automatically (mm → m, cm → m, etc.) so all SI-unit geometry arrives
    in metres.  Only IfcConversionBasedUnit files (feet, inches) keep their
    native unit in the mesh coordinates.
    """
    try:
        for ua in ifc_file.by_type("IfcUnitAssignment"):
            for u in (ua.Units or []):
                if getattr(u, "UnitType", None) != "LENGTHUNIT":
                    continue
                utype = u.is_a()
                if utype == "IfcSIUnit":
                    # Geometry is in metres regardless of prefix (mm/cm/m).
                    return _SQ_M_TO_SQ_FT
                if utype == "IfcConversionBasedUnit":
                    name = (getattr(u, "Name", None) or "").upper()
                    if "FOOT" in name or name in ("FT",):
                        return 1.0
                    if "INCH" in name:
                        return 1.0 / 144.0
                    return _SQ_M_TO_SQ_FT
    except Exception:
        pass
    return _SQ_M_TO_SQ_FT


def _convex_hull_2d(pts_np: "numpy.ndarray") -> "list[tuple[float, float]]":
    """Andrew's monotone chain on the XY plane. Returns CCW hull vertices."""
    pts = sorted(set(
        (round(float(p[0]), 3), round(float(p[1]), 3)) for p in pts_np
    ))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


_WALL_TYPES = frozenset({"IfcWall", "IfcWallStandardCase"})


def _wall_is_external(wall) -> "bool | None":
    """Returns True/False from Pset_WallCommon.IsExternal, or None if not found."""
    for rel in getattr(wall, "IsDefinedBy", []):
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pset = rel.RelatingPropertyDefinition
        if not pset.is_a("IfcPropertySet"):
            continue
        if "WallCommon" in (pset.Name or "") or "WallStandardCase" in (pset.Name or ""):
            for prop in (pset.HasProperties or []):
                if prop.Name == "IsExternal":
                    try:
                        v = prop.NominalValue
                        if v is not None:
                            return bool(v.wrappedValue)
                    except Exception:
                        pass
    return None


def _wall_opening_area_native(wall) -> float:
    """Sum of opening areas (doors + windows) in native IFC length units²."""
    total = 0.0
    for rel in getattr(wall, "HasOpenings", []):
        opening = rel.RelatedOpeningElement
        # Prefer actual door/window dimensions
        for fill_rel in getattr(opening, "HasFillings", []):
            filler = fill_rel.RelatedBuildingElement
            try:
                w = getattr(filler, "OverallWidth", None)
                h = getattr(filler, "OverallHeight", None)
                if w and h:
                    total += float(w) * float(h)
                    continue
            except Exception:
                pass
        # Fallback: BaseQuantities on the opening element
        for def_rel in getattr(opening, "IsDefinedBy", []):
            if not def_rel.is_a("IfcRelDefinesByProperties"):
                continue
            pset = def_rel.RelatingPropertyDefinition
            if pset.is_a("IfcElementQuantity"):
                for q in (pset.Quantities or []):
                    if q.Name in ("Area", "GrossArea", "NetArea") and q.is_a("IfcQuantityArea"):
                        try:
                            total += float(q.AreaValue)
                        except Exception:
                            pass
    return total


def _dist_pt_to_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    abx, aby = bx - ax, by - ay
    len_sq = abx * abx + aby * aby
    if len_sq < 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / len_sq))
    return math.sqrt((px - (ax + t * abx)) ** 2 + (py - (ay + t * aby)) ** 2)


def _hull_walls_only(
    shell_verts_flat: list,
    bmin: list,
    bmax: list,
    area_scale: float,
) -> "tuple[float, list]":
    """
    Fallback for models without IfcWall elements (e.g. CFS IfcMember).
    Computes exterior wall area as convex-hull perimeter × building height.
    No roof area included — matches user spec of wall-face-only.
    """
    import numpy as np

    if not shell_verts_flat:
        return 0.0, []

    pts = np.array(shell_verts_flat, dtype=float).reshape(-1, 3)
    if len(pts) < 3:
        return 0.0, []

    if len(pts) > 20_000:
        step = len(pts) // 20_000 + 1
        pts = pts[::step]

    z_min = float(bmin[2]) if math.isfinite(bmin[2]) else float(pts[:, 2].min())
    z_max = float(bmax[2]) if math.isfinite(bmax[2]) else float(pts[:, 2].max())
    height = z_max - z_min
    if height < 1e-9:
        return 0.0, []

    hull = _convex_hull_2d(pts[:, :2])
    n = len(hull)
    if n < 3:
        return 0.0, []

    planes: list = []
    total_native = 0.0

    for i in range(n):
        p0, p1 = hull[i], hull[(i + 1) % n]
        edge_len = math.sqrt((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2)
        if edge_len < 1e-9:
            continue
        panel_native = edge_len * height
        total_native += panel_native
        w_sqft = panel_native * area_scale
        planes.append({
            "id": f"hull_wall_{i}",
            "type": "wall",
            "area_sqft": round(w_sqft, 2),
            "verts": [
                p0[0], p0[1], z_min,
                p1[0], p1[1], z_min,
                p1[0], p1[1], z_max,
                p0[0], p0[1], z_max,
            ],
            "faces": [0, 1, 2, 0, 2, 3],
        })

    return round(total_native * area_scale, 1), planes


def _walls_from_elements(
    ifc_file,
    area_scale: float,
    walls: list,
    wall_geom: "dict[int, dict]",
    shell_verts_flat: list,
) -> "tuple[float, list]":
    """
    Compute exterior wall area from actual IfcWall elements.
    Area = wall_length × wall_height − opening_area, one face only.
    Exterior detection: Pset_WallCommon.IsExternal, then hull-proximity fallback.
    """
    import numpy as np

    # Build 2D convex hull for exterior-detection fallback
    hull_edges: list = []
    diag = 1.0
    if shell_verts_flat:
        pts = np.array(shell_verts_flat, dtype=float).reshape(-1, 3)
        if len(pts) > 20_000:
            step = len(pts) // 20_000 + 1
            pts = pts[::step]
        hull = _convex_hull_2d(pts[:, :2])
        if len(hull) >= 2:
            n = len(hull)
            hull_edges = [(hull[i], hull[(i + 1) % n]) for i in range(n)]
            xs = [p[0] for p in hull]
            ys = [p[1] for p in hull]
            diag = max(1e-9, math.hypot(max(xs) - min(xs), max(ys) - min(ys)))

    ext_threshold = diag * 0.15

    total_sqft = 0.0
    planes: list = []

    for wall in walls:
        wid = wall.id()
        if wid not in wall_geom:
            continue

        wg = wall_geom[wid]
        wbmin, wbmax = wg["bmin"], wg["bmax"]

        dx = wbmax[0] - wbmin[0]
        dy = wbmax[1] - wbmin[1]
        dz = wbmax[2] - wbmin[2]

        # Length along wall face = hypotenuse of XY bbox extent
        wall_length = math.hypot(dx, dy)
        wall_height = dz

        if wall_length < 1e-6 or wall_height < 1e-6:
            continue

        cx = (wbmin[0] + wbmax[0]) * 0.5
        cy = (wbmin[1] + wbmax[1]) * 0.5

        # Determine exterior
        is_ext = _wall_is_external(wall)
        if is_ext is None:
            if hull_edges:
                dist = min(
                    _dist_pt_to_seg(cx, cy, e[0][0], e[0][1], e[1][0], e[1][1])
                    for e in hull_edges
                )
                is_ext = dist <= ext_threshold
            else:
                is_ext = True

        if not is_ext:
            continue

        opening_area_native = _wall_opening_area_native(wall)
        wall_area_native = max(0.0, wall_length * wall_height - opening_area_native)
        wall_sqft = wall_area_native * area_scale
        if wall_sqft < 1e-6:
            continue

        total_sqft += wall_sqft

        # Build visualization quad (orient along dominant horizontal axis)
        z_bot, z_top = wbmin[2], wbmax[2]
        if dx >= dy:
            y_mid = cy
            verts = [wbmin[0], y_mid, z_bot, wbmax[0], y_mid, z_bot,
                     wbmax[0], y_mid, z_top, wbmin[0], y_mid, z_top]
        else:
            x_mid = cx
            verts = [x_mid, wbmin[1], z_bot, x_mid, wbmax[1], z_bot,
                     x_mid, wbmax[1], z_top, x_mid, wbmin[1], z_top]

        planes.append({
            "id": f"wall_{wid}",
            "type": "wall",
            "area_sqft": round(wall_sqft, 2),
            "verts": verts,
            "faces": [0, 1, 2, 0, 2, 3],
        })

    return round(total_sqft, 1), planes


def _wall_surface_sqft(
    ifc_file,
    area_scale: float,
    wall_geom: "dict[int, dict]",
    shell_verts_flat: list,
    bmin: list,
    bmax: list,
) -> "tuple[float, list]":
    """
    Entry point for exterior shell area calculation.
    Uses IfcWall elements when available; falls back to hull-perimeter method
    for models without wall elements (e.g. CFS IfcMember-only models).
    """
    walls = (
        list(ifc_file.by_type("IfcWall")) +
        list(ifc_file.by_type("IfcWallStandardCase"))
    )

    walls_with_geom = [w for w in walls if w.id() in wall_geom]

    if walls_with_geom:
        return _walls_from_elements(ifc_file, area_scale, walls_with_geom, wall_geom, shell_verts_flat)

    return _hull_walls_only(shell_verts_flat, bmin, bmax, area_scale)


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
            "surface_planes": [],
            "external_surface_sqft": 0.0,
        }

    ifc_file = ifcopenshell.open(str(path))
    area_scale = _ifc_area_scale(ifc_file)

    settings = geom.settings()
    settings.set("use-world-coords", True)
    settings.set("weld-vertices", True)

    groups: dict[str, dict[str, Any]] = {}
    shell_verts_flat: list[float] = []
    wall_geom: dict[int, dict] = {}
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

        if etype not in _SKIP_ETYPES:
            shell_verts_flat.extend(raw_verts)

        # Collect per-element bounding box for wall elements
        if etype in _WALL_TYPES:
            wbmin = [math.inf, math.inf, math.inf]
            wbmax = [-math.inf, -math.inf, -math.inf]
            for i in range(0, len(raw_verts), 3):
                x, y, z = raw_verts[i], raw_verts[i + 1], raw_verts[i + 2]
                if x < wbmin[0]: wbmin[0] = x
                if y < wbmin[1]: wbmin[1] = y
                if z < wbmin[2]: wbmin[2] = z
                if x > wbmax[0]: wbmax[0] = x
                if y > wbmax[1]: wbmax[1] = y
                if z > wbmax[2]: wbmax[2] = z
            wall_geom[element.id()] = {"bmin": wbmin, "bmax": wbmax}

        has_more = iterator.next()

    total_surf_sqft, surface_planes = _wall_surface_sqft(
        ifc_file, area_scale, wall_geom, shell_verts_flat, bmin, bmax
    )

    return {
        "type": "ifc",
        "bounds": _safe_bounds(bmin, bmax),
        "element_types": {k: {"color": v["color"]} for k, v in groups.items()},
        "groups": groups,
        "surface_planes": surface_planes,
        "external_surface_sqft": total_surf_sqft,
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
