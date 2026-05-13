"""DXF parser for area and framing linear totals."""

from __future__ import annotations

import math
from pathlib import Path
import sys
from typing import Iterable

from ..conversions import area_to_square_feet, length_to_feet, normalize_length_unit
from ..models import MeasurementTotals

_CFS_LAYER_STRONG_KEYWORDS = (
    "cfs",
    "cold formed",
    "cold-formed",
    "light gauge",
    "light-gauge",
    "lightgauge",
    "lgs",
)
_CFS_LAYER_ROLE_KEYWORDS = (
    "stud",
    "track",
    "joist",
    "rafter",
    "header",
    "sill",
    "girt",
    "purlin",
    "brace",
)
_CFS_LAYER_GENERIC_KEYWORDS = (
    "frame",
    "framing",
    "wallframe",
)
_CFS_LAYER_EXCLUDE_KEYWORDS = (
    "red iron",
    "red-iron",
    "structural steel",
    "structural-steel",
    "wide flange",
    "wide-flange",
    "w flange",
    "w-flange",
    "ibeam",
    "i beam",
    "i-beam",
    "hss",
    "h beam",
    "h-beam",
    "hbeam",
    "girder",
    "truss",
    "portal frame",
)

# DXF $INSUNITS header values → normalized unit string.
_INSUNITS_TO_UNIT: dict[int, str] = {
    1: "in",   # Inches
    2: "ft",   # Feet
    4: "mm",   # Millimeters
    5: "cm",   # Centimeters
    6: "m",    # Meters
    10: "yd",  # Yards
    21: "ft",  # US Survey Feet
}


def _detect_dxf_unit(doc: object) -> str | None:
    """Extract $INSUNITS from the DXF header and return a normalized unit string."""
    try:
        header = getattr(doc, "header", None)
        if header is None:
            return None
        insunits = header.get("$INSUNITS", 0)
        return _INSUNITS_TO_UNIT.get(int(insunits))
    except Exception:
        return None


def _distance_2d(start: tuple[float, float], end: tuple[float, float]) -> float:
    return math.hypot(end[0] - start[0], end[1] - start[1])


def _distance_3d(start: tuple[float, float, float], end: tuple[float, float, float]) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dz = end[2] - start[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _polyline_length(points: list[tuple[float, float]], closed: bool) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(len(points) - 1):
        total += _distance_2d(points[i], points[i + 1])
    if closed:
        total += _distance_2d(points[-1], points[0])
    return total


def _polygon_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    area_sum = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area_sum += (x1 * y2) - (x2 * y1)
    return abs(area_sum) * 0.5


def _ellipse_circumference(a: float, b: float) -> float:
    if a <= 0.0 or b <= 0.0:
        return 0.0
    h = ((a - b) ** 2) / ((a + b) ** 2)
    return math.pi * (a + b) * (1.0 + (3.0 * h) / (10.0 + math.sqrt(4.0 - 3.0 * h)))


def _triangle_area_3d(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> float:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return 0.5 * math.sqrt(cross[0] * cross[0] + cross[1] * cross[1] + cross[2] * cross[2])


def _point_xyz(point: object) -> tuple[float, float, float]:
    return float(point.x), float(point.y), float(getattr(point, "z", 0.0))


def _points_equal_3d(a: tuple[float, float, float], b: tuple[float, float, float], tol: float = 1e-9) -> bool:
    return (
        math.isclose(a[0], b[0], abs_tol=tol)
        and math.isclose(a[1], b[1], abs_tol=tol)
        and math.isclose(a[2], b[2], abs_tol=tol)
    )


def _face_vertices(entity: object) -> list[tuple[float, float, float]]:
    raw = [_point_xyz(entity.dxf.vtx0), _point_xyz(entity.dxf.vtx1), _point_xyz(entity.dxf.vtx2), _point_xyz(entity.dxf.vtx3)]
    vertices: list[tuple[float, float, float]] = []
    for point in raw:
        if not vertices or (not _points_equal_3d(point, vertices[-1])):
            vertices.append(point)
    if len(vertices) >= 2 and _points_equal_3d(vertices[0], vertices[-1]):
        vertices.pop()
    return vertices


def _face_area(entity: object) -> float:
    vertices = _face_vertices(entity)
    if len(vertices) < 3:
        return 0.0
    if len(vertices) == 3:
        return _triangle_area_3d(vertices[0], vertices[1], vertices[2])
    return _triangle_area_3d(vertices[0], vertices[1], vertices[2]) + _triangle_area_3d(vertices[0], vertices[2], vertices[3])


def _face_edge_lengths(
    entity: object,
) -> list[tuple[tuple[tuple[float, float, float], tuple[float, float, float]], float]]:
    vertices = _face_vertices(entity)
    if len(vertices) < 2:
        return []
    edges: list[tuple[tuple[tuple[float, float, float], tuple[float, float, float]], float]] = []
    for i in range(len(vertices)):
        a = vertices[i]
        b = vertices[(i + 1) % len(vertices)]
        length = _distance_3d(a, b)
        if length > 0.0:
            edges.append((_edge_key(a, b), length))
    return edges


def _edge_key(a: tuple[float, float, float], b: tuple[float, float, float], precision: int = 6) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    ra = (round(a[0], precision), round(a[1], precision), round(a[2], precision))
    rb = (round(b[0], precision), round(b[1], precision), round(b[2], precision))
    if ra <= rb:
        return ra, rb
    return rb, ra


def _safe_get_xy_points(raw_points: Iterable[object]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for point in raw_points:
        x = None
        y = None
        if hasattr(point, "x") and hasattr(point, "y"):
            x = float(point.x)
            y = float(point.y)
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x = float(point[0])
            y = float(point[1])
        if x is not None and y is not None:
            points.append((x, y))
    return points


def _polyline_points(polyline: object) -> list[tuple[float, float]]:
    try:
        return _safe_get_xy_points(polyline.points())
    except Exception:
        pass
    try:
        return _safe_get_xy_points(v.dxf.location for v in polyline.vertices)
    except Exception:
        pass
    return []


def _is_closed(entity: object) -> bool:
    candidates = [getattr(entity, "closed", None), getattr(entity, "is_closed", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        if callable(candidate):
            try:
                return bool(candidate())
            except Exception:
                continue
        return bool(candidate)
    return False


def _entity_layer_name(entity: object) -> str:
    try:
        return str(getattr(entity.dxf, "layer", "") or "").strip().lower()
    except Exception:
        return ""


def _looks_like_framing_layer(layer_name: str) -> bool:
    if not layer_name:
        return False

    if any(keyword in layer_name for keyword in _CFS_LAYER_STRONG_KEYWORDS):
        return True

    if any(keyword in layer_name for keyword in _CFS_LAYER_EXCLUDE_KEYWORDS):
        return False

    if any(keyword in layer_name for keyword in _CFS_LAYER_ROLE_KEYWORDS):
        return True

    if any(keyword in layer_name for keyword in _CFS_LAYER_GENERIC_KEYWORDS):
        return any(
            keyword in layer_name
            for keyword in (*_CFS_LAYER_STRONG_KEYWORDS, *_CFS_LAYER_ROLE_KEYWORDS)
        )

    return False


def _iter_dxf_entities(entities: Iterable[object], active_blocks: tuple[str, ...] = ()) -> Iterable[object]:
    for entity in entities:
        kind = str(entity.dxftype()).upper()
        if kind != "INSERT":
            yield entity
            continue

        block_name = str(getattr(entity.dxf, "name", "") or "").strip().lower()
        if block_name and block_name in active_blocks:
            continue

        nested_entities = None
        try:
            nested_entities = list(entity.virtual_entities())
        except Exception:
            nested_entities = None

        if nested_entities:
            next_blocks = active_blocks + ((block_name,) if block_name else ())
            yield from _iter_dxf_entities(nested_entities, next_blocks)
            continue

        try:
            block = entity.block()
        except Exception:
            block = None
        if block is None:
            yield entity
            continue

        next_blocks = active_blocks + ((block_name,) if block_name else ())
        yield from _iter_dxf_entities(block, next_blocks)


def _hatch_area(entity: object) -> float:
    """Extract enclosed area from a HATCH entity's boundary paths."""
    total = 0.0
    try:
        paths = getattr(entity, "paths", None)
        if paths is None:
            return 0.0
        for path in paths:
            # LWPolylinePath carries vertices directly.
            vertices = getattr(path, "vertices", None)
            if vertices is not None:
                pts = [(float(v[0]), float(v[1])) for v in vertices if len(v) >= 2]
                if len(pts) >= 3:
                    total += _polygon_area(pts)
                continue
            # EdgePath carries individual edge objects (line, arc, spline, ellipse).
            edges = getattr(path, "edges", None)
            if edges is None:
                continue
            edge_pts: list[tuple[float, float]] = []
            for edge in edges:
                edge_type = _normalized_edge_type(edge)
                if edge_type == "line":
                    start = getattr(edge, "start", None)
                    end = getattr(edge, "end", None)
                    if start is not None:
                        edge_pts.append((float(start[0]), float(start[1])))
                    if end is not None:
                        edge_pts.append((float(end[0]), float(end[1])))
                elif edge_type == "arc":
                    center = getattr(edge, "center", None)
                    radius = float(getattr(edge, "radius", 0.0))
                    start_angle = math.radians(float(getattr(edge, "start_angle", 0.0)))
                    end_angle = math.radians(float(getattr(edge, "end_angle", 360.0)))
                    if center is not None and radius > 0.0:
                        # Approximate arc as 8 points.
                        sweep = (end_angle - start_angle) % (2.0 * math.pi)
                        if sweep < 1e-9:
                            sweep = 2.0 * math.pi
                        for k in range(9):
                            angle = start_angle + sweep * k / 8.0
                            edge_pts.append((
                                float(center[0]) + radius * math.cos(angle),
                                float(center[1]) + radius * math.sin(angle),
                            ))
            if len(edge_pts) >= 3:
                total += _polygon_area(edge_pts)
    except Exception:
        pass
    return total


def _normalized_edge_type(edge: object) -> str:
    try:
        return type(edge).__name__.lower().replace("edge", "").replace("hatch", "")
    except Exception:
        return ""


def _entity_is_3d_member(entity: object, kind: str) -> bool:
    """Return True if the entity has meaningful Z coordinates — i.e. it is a 3D structural member."""
    try:
        if kind == "LINE":
            return abs(float(entity.dxf.start.z)) > 1e-6 or abs(float(entity.dxf.end.z)) > 1e-6
        if kind == "POLYLINE":
            for v in entity.vertices:
                if abs(float(v.dxf.location.z)) > 1e-6:
                    return True
        if kind == "LWPOLYLINE":
            return abs(float(getattr(entity.dxf, "elevation", 0.0))) > 1e-6
    except Exception:
        pass
    return False


def _entity_length_area(entity: object, kind: str) -> tuple[float, float]:
    if kind == "LINE":
        start = entity.dxf.start
        end = entity.dxf.end
        length = _distance_3d(
            (float(start.x), float(start.y), float(getattr(start, "z", 0.0))),
            (float(end.x), float(end.y), float(getattr(end, "z", 0.0))),
        )
        return length, 0.0

    if kind == "ARC":
        radius = float(entity.dxf.radius)
        sweep = (float(entity.dxf.end_angle) - float(entity.dxf.start_angle)) % 360.0
        return math.radians(sweep) * radius, 0.0

    if kind == "CIRCLE":
        radius = float(entity.dxf.radius)
        return 2.0 * math.pi * radius, math.pi * (radius ** 2)

    if kind == "ELLIPSE":
        major_axis = entity.dxf.major_axis
        major_radius = math.hypot(float(major_axis.x), float(major_axis.y))
        minor_radius = abs(major_radius * float(entity.dxf.ratio))
        circumference = _ellipse_circumference(major_radius, minor_radius)
        start_param = float(entity.dxf.start_param)
        end_param = float(entity.dxf.end_param)
        sweep = (end_param - start_param) % (2.0 * math.pi)
        if math.isclose(sweep, 0.0, abs_tol=1e-9):
            sweep = 2.0 * math.pi
        length = circumference * (sweep / (2.0 * math.pi))
        area = 0.0
        if math.isclose(sweep, 2.0 * math.pi, rel_tol=1e-7, abs_tol=1e-7):
            area = math.pi * major_radius * minor_radius
        return length, area

    if kind == "LWPOLYLINE":
        points = _safe_get_xy_points(entity.get_points("xy"))
        closed = _is_closed(entity)
        length = _polyline_length(points, closed)
        area = _polygon_area(points) if closed else 0.0
        return length, area

    if kind == "POLYLINE":
        points = _polyline_points(entity)
        closed = _is_closed(entity)
        length = _polyline_length(points, closed)
        area = _polygon_area(points) if closed else 0.0
        return length, area

    if kind == "HATCH":
        return 0.0, _hatch_area(entity)

    return 0.0, 0.0


def parse_dxf(path: Path, source_length_unit: str | None = None, strict_mode: bool = False) -> MeasurementTotals:
    """Parse a DXF and return normalized totals in square feet and feet."""
    try:
        import ezdxf
    except ImportError as exc:
        raise RuntimeError(
            "DXF parsing requires 'ezdxf' in this Python environment:\n"
            f"{sys.executable}\n"
            "Install it with: python -m pip install ezdxf"
        ) from exc

    doc = ezdxf.readfile(str(path))
    modelspace = doc.modelspace()

    # Unit resolution: explicit override > $INSUNITS header > default feet.
    if source_length_unit:
        unit = normalize_length_unit(source_length_unit)
        unit_source = "explicit"
    else:
        detected = _detect_dxf_unit(doc)
        if detected:
            unit = detected
            unit_source = "insunits"
        else:
            unit = "ft"
            unit_source = "default"

    area_total_native = 0.0
    framing_linear_total_native = 0.0
    framing_face_edges: dict[tuple[tuple[float, float, float], tuple[float, float, float]], float] = {}
    framing_entity_count = 0

    for entity in _iter_dxf_entities(modelspace):
        kind = entity.dxftype().upper()
        if kind == "3DFACE":
            area_total_native += _face_area(entity)
            layer_name = _entity_layer_name(entity)
            is_framing_layer = _looks_like_framing_layer(layer_name)
            for key, length in _face_edge_lengths(entity):
                if is_framing_layer:
                    framing_face_edges[key] = max(framing_face_edges.get(key, 0.0), length)
                    framing_entity_count += 1
            continue

        length_native, area_native = _entity_length_area(entity, kind)
        if length_native <= 0.0 and area_native <= 0.0:
            continue

        area_total_native += area_native
        if length_native > 0.0:
            layer_name = _entity_layer_name(entity)
            is_3d = _entity_is_3d_member(entity, kind)
            if is_3d or _looks_like_framing_layer(layer_name):
                framing_linear_total_native += length_native
                framing_entity_count += 1

    framing_linear_total_native += sum(framing_face_edges.values())

    use_cfs_framing_total = framing_linear_total_native > 0.0
    if strict_mode and area_total_native <= 0.0:
        raise ValueError(
            "DXF strict mode could not find a closed floor boundary, HATCH area, or 3DFACE area. "
            "Add explicit floor boundary geometry or disable strict mode."
        )
    if strict_mode and not use_cfs_framing_total:
        raise ValueError(
            "DXF strict mode could not find CFS framing-specific geometry. "
            "Add CFS framing layers or disable strict mode."
        )
    linear_basis = "3d-members-and-cfs-layer-geometry" if use_cfs_framing_total else "no-framing-found"

    area_basis = "dxf-closed-geometry-and-3dface-area"
    if area_total_native <= 0.0:
        area_basis = "no-floor-area-found"

    return MeasurementTotals(
        source_path=path,
        source_format="DXF",
        source_unit=unit,
        area_sqft=area_to_square_feet(area_total_native, unit),
        area_basis=area_basis,
        linear_ft=length_to_feet(framing_linear_total_native, unit),
        linear_basis=linear_basis,
        framing_element_count=framing_entity_count,
    )
