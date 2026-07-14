"""IFC parser for quantity and geometry-based framing totals."""

from __future__ import annotations

import re
import os
from pathlib import Path
import sys
from typing import Iterable

from ..conversions import length_to_feet, normalize_length_unit
from ..models import MeasurementTotals

_CFS_STRONG_KEYWORDS = (
    "cfs",
    "cold formed",
    "cold-formed",
    "light gauge",
    "light-gauge",
    "lightgauge",
    "lgs",
)
_CFS_ROLE_KEYWORDS = (
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
_CFS_STRUCTURAL_EXCLUDE_KEYWORDS = (
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
_CFS_MATERIAL_PATTERN = re.compile(
    r"(?:\b\d+(?:\.\d+)?(?:ga|gauge|bmt)\b|\b(?:c|h|s|z)\d+(?:\.\d+)?\b|\b(?:cfs|cold[- ]formed|light[- ]gauge|lgs)\b)",
    re.IGNORECASE,
)

# Standard IFC PredefinedType values that unambiguously indicate CFS framing.
_CFS_MEMBER_PREDEFINED_TYPES: frozenset[str] = frozenset({
    "stud",
    "purlin",
    "joist",
    "rafter",
    "brace",
    "chord",
    "track",
    "sill",
    "header",
    "girt",
    "runner",
    "angle",
    "channel",
    "zed",
    "hat",
})

_METERS_TO_FEET = 3.28083989501312
_SQUARE_METERS_TO_SQUARE_FEET = 10.763910416709722
_FRAMING_PROXY_NAME_PATTERN = re.compile(r"^[ftwrc]\d", re.IGNORECASE)
_FLOOR_AREA_NAME_PRIORITY = (
    "grossfloorarea",
    "netfloorarea",
    "floorarea",
    "grossarea",
    "netarea",
    "area",
)
_SHELL_SHADOW_CLASSES = (
    "IfcRoof",
    "IfcSlab",
    "IfcWall",
    "IfcWallStandardCase",
    "IfcCurtainWall",
)

_CFS_FRAMING_CLASSES: frozenset[str] = frozenset({
    "ifcmember",
    "ifcbeam",
    "ifccolumn",
    "ifcbuildingelementproxy",
    "ifcplate",
})

# Property set names that carry authoritative span/length properties.
_MEMBER_PSET_NAMES = (
    "pset_membercommon",
    "pset_beamcommon",
    "pset_columncommon",
    "pset_framecommon",
    "qto_memberbaseq",
    "qto_beambaseq",
    "qto_columnbaseq",
)
_MEMBER_LENGTH_PROP_NAMES = (
    "span",
    "length",
    "height",
    "depth",
    "overalllength",
    "overallheight",
    "overalldepth",
)


def _ifc_geometry_disabled() -> bool:
    return os.environ.get("BISONSCOPE_DISABLE_IFC_GEOM", "").strip().lower() in {"1", "true", "yes"}


def _detect_ifc_length_unit(ifc_file: object) -> str | None:
    """Best-effort extraction of the file's length unit."""
    try:
        assignments = ifc_file.by_type("IfcUnitAssignment")
    except Exception:
        return None

    for assignment in assignments:
        units = getattr(assignment, "Units", None) or []
        for unit in units:
            if unit.is_a("IfcSIUnit") and getattr(unit, "UnitType", "") == "LENGTHUNIT":
                name = (getattr(unit, "Name", "") or "").upper()
                prefix = (getattr(unit, "Prefix", "") or "").upper()
                if name in {"METRE", "METER"}:
                    if prefix == "MILLI":
                        return "mm"
                    if prefix == "CENTI":
                        return "cm"
                    return "m"
            if unit.is_a("IfcConversionBasedUnit") and getattr(unit, "UnitType", "") == "LENGTHUNIT":
                unit_name = (getattr(unit, "Name", "") or "").lower()
                if "foot" in unit_name or "feet" in unit_name:
                    return "ft"
                if "inch" in unit_name:
                    return "in"
                if "yard" in unit_name:
                    return "yd"
    return None


def _resolve_ifc_scale_to_feet(ifc_file: object, source_length_unit: str | None) -> tuple[float, str]:
    """
    Resolve the length scale from IFC units to feet.

    Returns:
        (feet_per_ifc_length_unit, source_unit_label)
    """
    if source_length_unit:
        normalized = normalize_length_unit(source_length_unit)
        return length_to_feet(1.0, normalized), normalized

    if _ifc_geometry_disabled():
        detected = _detect_ifc_length_unit(ifc_file)
        if detected:
            return length_to_feet(1.0, detected), detected
        return length_to_feet(1.0, "m"), "m"

    try:
        import ifcopenshell.util.unit as unit_utils

        unit_scale_to_meters = float(unit_utils.calculate_unit_scale(ifc_file, "LENGTHUNIT"))
        if unit_scale_to_meters <= 0.0:
            raise ValueError("Non-positive IFC unit scale")
        feet_per_ifc_unit = unit_scale_to_meters * _METERS_TO_FEET
        detected = _detect_ifc_length_unit(ifc_file)
        if detected:
            return feet_per_ifc_unit, detected
        return feet_per_ifc_unit, f"ifc-project-units ({unit_scale_to_meters:.8g} m/unit)"
    except Exception:
        # Conservative fallback when unit metadata is incomplete.
        return length_to_feet(1.0, "m"), "m"


def _resolve_ifc_area_scale_to_sqft(
    ifc_file: object,
    source_length_unit: str | None,
    length_scale_to_feet: float,
) -> float:
    """Resolve area scale from IFC project units to square feet."""
    if source_length_unit:
        unit = normalize_length_unit(source_length_unit)
        return length_to_feet(1.0, unit) ** 2

    if _ifc_geometry_disabled():
        return length_scale_to_feet ** 2

    try:
        import ifcopenshell.util.unit as unit_utils

        area_scale_to_square_meters = float(unit_utils.calculate_unit_scale(ifc_file, "AREAUNIT"))
        if area_scale_to_square_meters <= 0.0:
            raise ValueError("Non-positive IFC area unit scale")
        return area_scale_to_square_meters * _SQUARE_METERS_TO_SQUARE_FEET
    except Exception:
        # Fallback assumes area unit is derived from the project length unit.
        return length_scale_to_feet ** 2


def _iter_object_quantities(obj: object) -> Iterable[object]:
    for relation in getattr(obj, "IsDefinedBy", None) or []:
        if not relation.is_a("IfcRelDefinesByProperties"):
            continue
        prop_def = getattr(relation, "RelatingPropertyDefinition", None)
        if prop_def is None or not prop_def.is_a("IfcElementQuantity"):
            continue
        for quantity in getattr(prop_def, "Quantities", None) or []:
            yield quantity


def _iter_element_quantities(element: object) -> Iterable[object]:
    yield from _iter_object_quantities(element)


def _object_area_map(obj: object) -> dict[str, float]:
    area_map: dict[str, float] = {}
    for quantity in _iter_object_quantities(obj):
        if not quantity.is_a("IfcQuantityArea"):
            continue
        name = _normalized_text(getattr(quantity, "Name", ""))
        value = float(getattr(quantity, "AreaValue", 0.0) or 0.0)
        if value > 0.0:
            area_map[name] = value
    return area_map


def _select_floor_area_from_map(area_map: dict[str, float], allow_generic_area: bool) -> float:
    for preferred_name in _FLOOR_AREA_NAME_PRIORITY:
        if preferred_name == "area" and not allow_generic_area:
            continue
        if preferred_name in area_map:
            return area_map[preferred_name]

    for key, value in area_map.items():
        key_l = _normalized_text(key)
        if ("floor" in key_l and "area" in key_l) or ("gross" in key_l and "area" in key_l) or ("net" in key_l and "area" in key_l):
            return value

    if allow_generic_area:
        for key, value in area_map.items():
            if "area" in _normalized_text(key):
                return value
    return 0.0


def _sum_floor_area_from_objects(objects: Iterable[object], allow_generic_area: bool) -> float:
    total = 0.0
    for obj in objects:
        area_map = _object_area_map(obj)
        total += _select_floor_area_from_map(area_map, allow_generic_area=allow_generic_area)
    return total


def _estimate_space_geometry_floor_area_native(ifc_file: object) -> float:
    """
    Estimate floor area from IfcSpace geometry when authored quantities are missing.

    This is more explicit than the generic framing-footprint fallback, but it still
    remains an approximation because it uses projected geometry.
    """
    if _ifc_geometry_disabled():
        return 0.0

    spaces = list(ifc_file.by_type("IfcSpace"))
    if not spaces:
        return 0.0

    try:
        from shapely.geometry import MultiPoint
    except ImportError as exc:
        raise RuntimeError("IFC space-geometry fallback requires 'shapely'.") from exc

    try:
        import ifcopenshell.geom as geom
    except ImportError as exc:
        raise RuntimeError("IFC space-geometry fallback requires ifcopenshell geometry module.") from exc

    settings = geom.settings()
    settings.set("use-world-coords", True)
    settings.set("disable-opening-subtractions", True)

    iterator = geom.iterator(settings, ifc_file, 1, include=spaces)
    total_m2 = 0.0
    if iterator.initialize():
        while True:
            shape = iterator.get()
            verts = getattr(shape.geometry, "verts", None) or []
            if len(verts) >= 9:
                points_xy = [
                    (float(verts[i]), float(verts[i + 1]))
                    for i in range(0, len(verts), 3)
                    if i + 1 < len(verts)
                ]
                if len(points_xy) >= 3:
                    total_m2 += float(MultiPoint(points_xy).convex_hull.area)
            if not iterator.next():
                break

    return total_m2


def _estimate_shell_shadow_floor_area_sqft(ifc_file: object) -> float:
    """
    Estimate total floor area from the exterior shell projection.

    This is a fallback for models that expose shell geometry but do not carry
    reliable authored floor-area quantities.
    """
    if _ifc_geometry_disabled():
        return 0.0

    shell_elements: list[object] = []
    for class_name in _SHELL_SHADOW_CLASSES:
        shell_elements.extend(list(ifc_file.by_type(class_name)))
    if not shell_elements:
        return 0.0

    try:
        from shapely.geometry import MultiPoint
    except ImportError as exc:
        raise RuntimeError("IFC shell-shadow fallback requires 'shapely'.") from exc

    try:
        import ifcopenshell.geom as geom
    except ImportError as exc:
        raise RuntimeError("IFC shell-shadow fallback requires ifcopenshell geometry module.") from exc

    settings = geom.settings()
    settings.set("use-world-coords", True)
    settings.set("disable-opening-subtractions", True)

    iterator = geom.iterator(settings, ifc_file, 1, include=shell_elements)
    points_xy: list[tuple[float, float]] = []
    if iterator.initialize():
        while True:
            shape = iterator.get()
            verts = getattr(shape.geometry, "verts", None) or []
            for i in range(0, len(verts), 3):
                if i + 1 < len(verts):
                    points_xy.append((float(verts[i]), float(verts[i + 1])))
            if not iterator.next():
                break

    if len(points_xy) < 3:
        return 0.0

    footprint_area_m2 = float(MultiPoint(points_xy).convex_hull.area)
    if footprint_area_m2 <= 0.0:
        return 0.0

    storey_count = max(len(ifc_file.by_type("IfcBuildingStorey")), 1)
    return footprint_area_m2 * _SQUARE_METERS_TO_SQUARE_FEET * float(storey_count)


def _resolve_area_total_native(ifc_file: object, strict_mode: bool = False) -> tuple[float, str, bool]:
    spaces = list(ifc_file.by_type("IfcSpace"))
    spaces_total = _sum_floor_area_from_objects(spaces, allow_generic_area=False)
    if spaces_total > 0.0:
        return spaces_total, "ifcspace-floorarea-quantities", False

    spaces_total = _sum_floor_area_from_objects(spaces, allow_generic_area=True)
    if spaces_total > 0.0:
        return spaces_total, "ifcspace-area-quantities-fallback", True

    context_objects = [*ifc_file.by_type("IfcBuildingStorey"), *ifc_file.by_type("IfcBuilding")]
    context_total = _sum_floor_area_from_objects(context_objects, allow_generic_area=False)
    if context_total > 0.0:
        return context_total, "ifcbuildingstorey-or-building-floorarea-quantities", False

    context_total = _sum_floor_area_from_objects(context_objects, allow_generic_area=True)
    if context_total > 0.0:
        return context_total, "ifcbuildingstorey-or-building-area-quantities-fallback", True

    # IfcSlab authored area (floor slabs without explicit IfcSpace zones).
    slabs = list(ifc_file.by_type("IfcSlab"))
    slab_total = _sum_floor_area_from_objects(slabs, allow_generic_area=False)
    if slab_total > 0.0:
        return slab_total, "ifcslab-floorarea-quantities", False

    slab_total = _sum_floor_area_from_objects(slabs, allow_generic_area=True)
    if slab_total > 0.0:
        return slab_total, "ifcslab-area-quantities-fallback", True

    space_geometry_total = _estimate_space_geometry_floor_area_native(ifc_file)
    if space_geometry_total > 0.0:
        return space_geometry_total, "ifcspace-geometry-footprint", True

    shell_shadow_total = _estimate_shell_shadow_floor_area_sqft(ifc_file)
    if shell_shadow_total > 0.0:
        return shell_shadow_total, "ifcshell-geometry-footprint-fallback", True

    if strict_mode:
        raise ValueError(
            "IFC strict mode could not find authored floor-area quantities. "
            "Add IfcSpace, storey, slab, or building area quantities, or disable strict mode."
        )

    return 0.0, "no-floor-area-quantities", True


def _estimate_floor_area_sqft_from_framing_footprint(ifc_file: object) -> float:
    """
    Estimate total floor area from framing geometry footprint.

    This is only used when no explicit floor area quantities exist.
    """
    if _ifc_geometry_disabled():
        return 0.0

    try:
        from shapely.geometry import MultiPoint
    except ImportError as exc:
        raise RuntimeError("IFC floor-footprint fallback requires 'shapely'.") from exc

    try:
        import ifcopenshell.geom as geom
    except ImportError as exc:
        raise RuntimeError("IFC floor-footprint fallback requires ifcopenshell geometry module.") from exc

    settings = geom.settings()
    settings.set("use-world-coords", True)

    points_xy: list[tuple[float, float]] = []
    iterator = geom.iterator(settings, ifc_file, 1)
    if iterator.initialize():
        while True:
            shape = iterator.get()
            element = ifc_file.by_id(shape.id)
            if element is not None and _is_framing_element(element):
                verts = getattr(shape.geometry, "verts", None) or []
                for i in range(0, len(verts), 3):
                    points_xy.append((float(verts[i]), float(verts[i + 1])))
            if not iterator.next():
                break

    if len(points_xy) < 3:
        return 0.0

    # Geometry iterator outputs SI meters by default, so footprint area is m^2.
    footprint_area_m2 = float(MultiPoint(points_xy).convex_hull.area)
    if footprint_area_m2 <= 0.0:
        return 0.0

    storey_count = max(len(ifc_file.by_type("IfcBuildingStorey")), 1)
    return footprint_area_m2 * _SQUARE_METERS_TO_SQUARE_FEET * float(storey_count)


def _normalized_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _iter_material_texts(material: object, seen: set[int]) -> list[str]:
    if material is None:
        return []

    entity_id = _safe_entity_id(material)
    if entity_id is not None and entity_id in seen:
        return []
    if entity_id is not None:
        seen.add(entity_id)

    texts = [
        _normalized_text(getattr(material, "Name", None)),
        _normalized_text(getattr(material, "Description", None)),
        _normalized_text(getattr(material, "Category", None)),
        _normalized_text(getattr(material, "ProfileName", None)),
    ]

    kind = _normalized_text(getattr(material, "is_a", lambda: "")())
    if kind == "ifcmaterialprofilesetusage":
        texts.extend(_iter_material_texts(getattr(material, "ForProfileSet", None), seen))
    elif kind == "ifcmaterialprofileset":
        for profile in getattr(material, "MaterialProfiles", None) or []:
            texts.extend(_iter_material_texts(profile, seen))
    elif kind == "ifcmaterialprofile":
        texts.extend(_iter_material_texts(getattr(material, "Profile", None), seen))
    elif kind == "ifcmateriallayersetusage":
        texts.extend(_iter_material_texts(getattr(material, "ForLayerSet", None), seen))
    elif kind == "ifcmateriallayerset":
        for layer in getattr(material, "MaterialLayers", None) or []:
            texts.extend(_iter_material_texts(layer, seen))
    elif kind == "ifcmateriallayer":
        texts.extend(
            [
                _normalized_text(getattr(material, "Name", None)),
                _normalized_text(getattr(material, "Description", None)),
                _normalized_text(getattr(material, "Category", None)),
            ]
        )

    return [text for text in texts if text]


def _element_blob_text(element: object) -> str:
    parts = [
        _normalized_text(getattr(element, "Name", None)),
        _normalized_text(getattr(element, "ObjectType", None)),
        _normalized_text(getattr(element, "Tag", None)),
        _normalized_text(getattr(element, "PredefinedType", None)),
        _normalized_text(getattr(element, "Description", None)),
        _normalized_text(getattr(element, "LongName", None)),
        _normalized_text(getattr(element, "is_a", lambda: "")()),
    ]

    material_texts: list[str] = []
    for association in getattr(element, "HasAssociations", None) or []:
        if _normalized_text(getattr(association, "is_a", lambda: "")()) != "ifcrelassociatesmaterial":
            continue
        material_texts.extend(_iter_material_texts(getattr(association, "RelatingMaterial", None), set()))

    parts.extend(material_texts)
    return " ".join(text for text in parts if text)


def _has_cfs_material_signal(blob: str) -> bool:
    return bool(_CFS_MATERIAL_PATTERN.search(blob))


def _has_cfs_role_signal(blob: str) -> bool:
    return any(keyword in blob for keyword in _CFS_ROLE_KEYWORDS)


def _has_structural_steel_signal(blob: str) -> bool:
    return any(keyword in blob for keyword in _CFS_STRUCTURAL_EXCLUDE_KEYWORDS)


def _safe_entity_id(entity: object) -> int | None:
    try:
        return int(entity.id())
    except Exception:
        return None


def _element_predefined_type(element: object) -> str:
    """Return the effective predefined type string, resolving USERDEFINED via ObjectType."""
    pred = _normalized_text(getattr(element, "PredefinedType", None))
    if pred in ("", "notdefined", "userdefined"):
        # Some authoring tools store the actual type in ObjectType when PredefinedType is USERDEFINED.
        return _normalized_text(getattr(element, "ObjectType", None))
    return pred


def _is_framing_element(element: object) -> bool:
    try:
        class_name = _normalized_text(element.is_a())
    except Exception:
        class_name = ""

    if class_name not in _CFS_FRAMING_CLASSES:
        return False

    # PredefinedType matching is the most authoritative CFS signal.
    pred_type = _element_predefined_type(element)
    if pred_type in _CFS_MEMBER_PREDEFINED_TYPES:
        # Still reject if structural-steel material signals are present.
        blob = _element_blob_text(element)
        if not _has_structural_steel_signal(blob):
            return True

    blob = _element_blob_text(element)
    name = _normalized_text(getattr(element, "Name", None))
    strong_cfs = _has_cfs_material_signal(blob) or any(keyword in blob for keyword in _CFS_STRONG_KEYWORDS)
    if strong_cfs:
        return True

    if _has_structural_steel_signal(blob):
        return False

    if class_name in {"ifcmember", "ifcbuildingelementproxy"}:
        return bool(
            _FRAMING_PROXY_NAME_PATTERN.match(name)
            or _has_cfs_role_signal(blob)
        )

    return False


def _element_length_from_psets(element: object) -> float:
    """
    Read authoritative span/length from common CFS member property sets.

    Pset_MemberCommon and similar sets often carry the true manufacturing length,
    which is more reliable than geometry-derived extrusion depths.
    """
    for relation in getattr(element, "IsDefinedBy", None) or []:
        if not relation.is_a("IfcRelDefinesByProperties"):
            continue
        prop_def = getattr(relation, "RelatingPropertyDefinition", None)
        if prop_def is None:
            continue

        pset_name = _normalized_text(getattr(prop_def, "Name", ""))

        if prop_def.is_a("IfcPropertySet"):
            if not any(token in pset_name for token in _MEMBER_PSET_NAMES):
                continue
            for prop in getattr(prop_def, "HasProperties", None) or []:
                if not prop.is_a("IfcPropertySingleValue"):
                    continue
                prop_name = _normalized_text(getattr(prop, "Name", ""))
                if not any(token in prop_name for token in _MEMBER_LENGTH_PROP_NAMES):
                    continue
                nominal = getattr(prop, "NominalValue", None)
                if nominal is None:
                    continue
                wrapped = getattr(nominal, "wrappedValue", None)
                if wrapped is None:
                    continue
                try:
                    v = float(wrapped)
                    if v > 0.0:
                        return v
                except (ValueError, TypeError):
                    continue

        elif prop_def.is_a("IfcElementQuantity"):
            if not any(token in pset_name for token in _MEMBER_PSET_NAMES):
                continue
            for qty in getattr(prop_def, "Quantities", None) or []:
                if not qty.is_a("IfcQuantityLength"):
                    continue
                qty_name = _normalized_text(getattr(qty, "Name", ""))
                if not any(token in qty_name for token in _MEMBER_LENGTH_PROP_NAMES):
                    continue
                v = float(getattr(qty, "LengthValue", 0.0) or 0.0)
                if v > 0.0:
                    return v

    return 0.0


def _element_length_from_quantities(element: object) -> float:
    lengths: list[float] = []
    for quantity in _iter_element_quantities(element):
        if not quantity.is_a("IfcQuantityLength"):
            continue
        name = _normalized_text(getattr(quantity, "Name", ""))
        if (
            "length" not in name
            and "height" not in name
            and "depth" not in name
            and "span" not in name
        ):
            continue
        value = float(getattr(quantity, "LengthValue", 0.0) or 0.0)
        if value > 0.0:
            lengths.append(value)

    if not lengths:
        return 0.0
    # Use the largest length from this element to avoid double counting parallel quantity names.
    return max(lengths)


def _distance(points_a: tuple[float, float, float], points_b: tuple[float, float, float]) -> float:
    dx = points_b[0] - points_a[0]
    dy = points_b[1] - points_a[1]
    dz = points_b[2] - points_a[2]
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def _point_xyz(point: object) -> tuple[float, float, float] | None:
    coords = getattr(point, "Coordinates", None)
    if not coords:
        return None
    values = [float(value) for value in coords]
    if len(values) == 2:
        return values[0], values[1], 0.0
    if len(values) >= 3:
        return values[0], values[1], values[2]
    return None


def _ifc_polyline_length(curve: object) -> float:
    points = []
    for point in getattr(curve, "Points", None) or []:
        xyz = _point_xyz(point)
        if xyz is not None:
            points.append(xyz)
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(len(points) - 1):
        total += _distance(points[i], points[i + 1])
    return total


def _ifc_indexed_polycurve_length(curve: object) -> float:
    points_ref = getattr(curve, "Points", None)
    coord_list = getattr(points_ref, "CoordList", None) or []
    points: list[tuple[float, float, float]] = []
    for item in coord_list:
        coords = [float(value) for value in item]
        if len(coords) == 2:
            points.append((coords[0], coords[1], 0.0))
        elif len(coords) >= 3:
            points.append((coords[0], coords[1], coords[2]))

    if len(points) < 2:
        return 0.0

    total = 0.0
    for i in range(len(points) - 1):
        total += _distance(points[i], points[i + 1])
    return total


def _curve_length(curve: object, seen: set[int]) -> float:
    if curve is None:
        return 0.0

    entity_id = _safe_entity_id(curve)
    if entity_id is not None and entity_id in seen:
        return 0.0
    if entity_id is not None:
        seen.add(entity_id)

    kind = _normalized_text(curve.is_a())
    if kind == "ifcpolyline":
        return _ifc_polyline_length(curve)
    if kind == "ifcindexedpolycurve":
        return _ifc_indexed_polycurve_length(curve)
    if kind == "ifccompositecurve":
        total = 0.0
        for segment in getattr(curve, "Segments", None) or []:
            total += _curve_length(getattr(segment, "ParentCurve", None), seen)
        return total
    if kind == "ifctrimmedcurve":
        # For member centerlines, basis curves are often enough when explicit trim lengths are unavailable.
        return _curve_length(getattr(curve, "BasisCurve", None), seen)
    return 0.0


def _collect_item_lengths(item: object, seen: set[int]) -> list[float]:
    if item is None:
        return []

    entity_id = _safe_entity_id(item)
    if entity_id is not None and entity_id in seen:
        return []
    if entity_id is not None:
        seen.add(entity_id)

    lengths: list[float] = []
    kind = _normalized_text(item.is_a())

    if kind == "ifcextrudedareasolid":
        value = float(getattr(item, "Depth", 0.0) or 0.0)
        if value > 0.0:
            lengths.append(value)

    if kind == "ifcsweptdisksolid":
        length = _curve_length(getattr(item, "Directrix", None), set())
        if length > 0.0:
            lengths.append(length)

    if kind in {"ifcpolyline", "ifcindexedpolycurve", "ifccompositecurve", "ifctrimmedcurve"}:
        length = _curve_length(item, set())
        if length > 0.0:
            lengths.append(length)

    if kind == "ifcmappeditem":
        mapping = getattr(item, "MappingSource", None)
        mapped_representation = getattr(mapping, "MappedRepresentation", None)
        for mapped_item in getattr(mapped_representation, "Items", None) or []:
            lengths.extend(_collect_item_lengths(mapped_item, seen))

    if kind in {"ifcbooleanresult", "ifcbooleanclippingresult"}:
        lengths.extend(_collect_item_lengths(getattr(item, "FirstOperand", None), seen))
        lengths.extend(_collect_item_lengths(getattr(item, "SecondOperand", None), seen))

    return lengths


def _element_length_from_geometry(element: object) -> float:
    representation = getattr(element, "Representation", None)
    if representation is None:
        return 0.0

    candidates: list[float] = []
    seen: set[int] = set()
    for shape_rep in getattr(representation, "Representations", None) or []:
        for item in getattr(shape_rep, "Items", None) or []:
            candidates.extend(_collect_item_lengths(item, seen))

    if not candidates:
        return 0.0
    return max(candidates)


def _framing_length_sum(ifc_file: object, strict_mode: bool = False) -> tuple[float, bool, int]:
    """Return (total_linear_ft_native, used_geometry_fallback, element_count)."""
    total = 0.0
    used_geometry_fallback = False
    used_pset_source = False
    seen_ids: set[int] = set()
    element_count = 0

    for element in ifc_file.by_type("IfcElement"):
        entity_id = _safe_entity_id(element)
        if entity_id is not None and entity_id in seen_ids:
            continue
        if entity_id is not None:
            seen_ids.add(entity_id)

        if not _is_framing_element(element):
            continue

        element_count += 1

        # Priority order: pset properties > QTO quantities > geometry
        element_length = _element_length_from_psets(element)
        if element_length > 0.0:
            used_pset_source = True
        else:
            element_length = _element_length_from_quantities(element)

        if element_length <= 0.0:
            element_length = _element_length_from_geometry(element)
            if element_length > 0.0:
                used_geometry_fallback = True

        if element_length > 0.0:
            total += element_length

    if strict_mode and used_geometry_fallback:
        raise ValueError(
            "IFC strict mode requires explicit framing length quantities. "
            "Geometry-derived framing lengths are not allowed."
        )
    return total, used_geometry_fallback, element_count


def _all_quantity_length_sum(ifc_file: object) -> float:
    total = 0.0
    for quantity in ifc_file.by_type("IfcQuantityLength"):
        name = _normalized_text(getattr(quantity, "Name", ""))
        if ("length" not in name) and ("perimeter" not in name) and ("linear" not in name):
            continue
        total += float(getattr(quantity, "LengthValue", 0.0) or 0.0)
    return total


def _build_linear_basis(framing_used_geometry: bool, framing_linear_total: float) -> str:
    if framing_linear_total <= 0.0:
        return "no-cfs-framing-found"
    if framing_used_geometry:
        return "cfs-framing-element-geometry-or-quantities"
    return "cfs-framing-element-quantities"


def parse_ifc(path: Path, source_length_unit: str | None = None, strict_mode: bool = False) -> MeasurementTotals:
    """Parse an IFC and return normalized totals in square feet and feet."""
    try:
        import ifcopenshell
    except ImportError as exc:
        raise RuntimeError(
            "IFC parsing requires 'ifcopenshell' in this Python environment:\n"
            f"{sys.executable}\n"
            "Install it with: python -m pip install ifcopenshell"
        ) from exc

    ifc_file = ifcopenshell.open(str(path))

    length_scale_to_feet, effective_unit = _resolve_ifc_scale_to_feet(ifc_file, source_length_unit)
    area_scale_to_sqft = _resolve_ifc_area_scale_to_sqft(ifc_file, source_length_unit, length_scale_to_feet)

    area_total_native, area_basis, area_used_fallback = _resolve_area_total_native(ifc_file, strict_mode=strict_mode)
    if area_total_native > 0.0:
        area_sqft = float(area_total_native) * area_scale_to_sqft
    else:
        area_sqft = _estimate_floor_area_sqft_from_framing_footprint(ifc_file)
        if area_sqft > 0.0:
            storey_count = max(len(ifc_file.by_type("IfcBuildingStorey")), 1)
            area_basis = f"framing-geometry-footprint-x{storey_count}-storeys-fallback"
            area_used_fallback = True
        else:
            area_basis = "no-floor-area-found"
            area_used_fallback = True

    framing_linear_total_native, framing_used_geometry, framing_count = _framing_length_sum(
        ifc_file, strict_mode=strict_mode
    )
    linear_basis = _build_linear_basis(framing_used_geometry, framing_linear_total_native)

    if strict_mode and area_used_fallback:
        raise ValueError(
            "IFC strict mode requires authored floor-area quantities. "
            "Geometry-derived floor area fallback is not allowed."
        )
    if strict_mode and linear_basis not in {"cfs-framing-element-quantities"}:
        raise ValueError(
            "IFC strict mode could not find authored CFS framing length quantities. "
            "Geometry-derived or non-CFS framing lengths are not allowed."
        )

    return MeasurementTotals(
        source_path=path,
        source_format="IFC",
        source_unit=effective_unit,
        area_sqft=area_sqft,
        area_basis=area_basis,
        linear_ft=float(framing_linear_total_native) * length_scale_to_feet,
        linear_basis=linear_basis,
        framing_element_count=framing_count,
    )
