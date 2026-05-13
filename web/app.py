"""FastAPI web application for BisonScope."""

from __future__ import annotations

import base64
import os
import re
import tempfile
from array import array
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_HERE = Path(__file__).parent
_STATIC = _HERE / "static"

app = FastAPI(title="BisonScope", version="3.0")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ── IFC preview colour buckets (matches Patch1) ───────────────────────────
_IFC_PREVIEW_COLORS: dict[str, int] = {
    "Walls":     0x71B7FF,
    "Roofs":     0xF4B860,
    "Floors":    0x5CD6B8,
    "Ceilings":  0xC68CFF,
    "Trusses":   0xFF8F70,
    "Structure": 0x9AA6B2,
    "Other":     0x8EA0AF,
}


def _name_prefix(name: str | None) -> str | None:
    if not isinstance(name, str):
        return None
    m = re.match(r"[A-Za-z]+", name.strip())
    return m.group(0).upper() if m else None


def _ifc_preview_bucket(entity: object | None) -> str:
    if entity is None:
        return "Other"
    name = getattr(entity, "Name", None)
    prefix = _name_prefix(name)
    if prefix:
        if prefix.startswith("W"): return "Walls"
        if prefix.startswith("R"): return "Roofs"
        if prefix.startswith("C"): return "Ceilings"
        if prefix.startswith("F"): return "Floors"
        if prefix.startswith("T"): return "Trusses"
    etype = getattr(entity, "is_a", lambda: "Other")().upper()
    if "WALL"    in etype: return "Walls"
    if "ROOF"    in etype: return "Roofs"
    if "FLOOR"   in etype or "SLAB" in etype: return "Floors"
    if "CEILING" in etype: return "Ceilings"
    if "TRUSS"   in etype or "BEAM" in etype or "MEMBER" in etype: return "Structure"
    return "Other"


def _enc(values: array) -> str:
    return base64.b64encode(values.tobytes()).decode("ascii")


def _build_ifc_preview_payload(ifc_path: Path) -> dict:
    try:
        import ifcopenshell
        import ifcopenshell.geom as geom
    except ImportError:
        raise RuntimeError("ifcopenshell is not installed.")

    model = ifcopenshell.open(str(ifc_path))
    settings = geom.settings()
    try: settings.set("use-world-coords", True)
    except Exception: pass

    include = [e for e in model.by_type("IfcProduct") if getattr(e, "Representation", None)]
    if not include:
        raise ValueError("No IFC products with geometry found.")

    num_threads = max(1, min(4, os.cpu_count() or 1))
    iterator = geom.iterator(settings, model, num_threads, include=include)
    if not iterator.initialize():
        raise ValueError("IFC geometry iterator failed to initialize.")

    buckets: dict[str, dict] = {}
    stats = {"products": 0, "meshes": 0, "triangles": 0, "skipped": 0}
    bmin = [float("inf")] * 3
    bmax = [float("-inf")] * 3

    while True:
        shape = iterator.get()
        stats["products"] += 1
        entity = model.by_id(shape.id)
        bucket_name = _ifc_preview_bucket(entity)

        g = getattr(shape, "geometry", None)
        verts = tuple(getattr(g, "verts", ()) or ())
        faces = tuple(getattr(g, "faces", ()) or ())

        if len(verts) < 3 or len(faces) < 3:
            stats["skipped"] += 1
        else:
            b = buckets.setdefault(bucket_name, {
                "positions": array("f"), "indices": array("I"),
                "meshes": 0, "triangles": 0,
            })
            offset = len(b["positions"]) // 3
            b["positions"].extend(float(v) for v in verts)
            for i in range(0, len(verts), 3):
                x, y, z = float(verts[i]), float(verts[i+1]), float(verts[i+2])
                if x < bmin[0]: bmin[0] = x
                if y < bmin[1]: bmin[1] = y
                if z < bmin[2]: bmin[2] = z
                if x > bmax[0]: bmax[0] = x
                if y > bmax[1]: bmax[1] = y
                if z > bmax[2]: bmax[2] = z
            for f in faces:
                b["indices"].append(offset + int(f))
            b["meshes"] += 1
            b["triangles"] += len(faces) // 3
            stats["meshes"] += 1
            stats["triangles"] += len(faces) // 3

        if not iterator.next():
            break

    if not buckets:
        raise ValueError("IFC preview geometry could not be generated.")

    ordered = []
    for name in ("Walls", "Roofs", "Floors", "Ceilings", "Trusses", "Structure", "Other"):
        d = buckets.get(name)
        if not d or not d["positions"] or not d["indices"]:
            continue
        ordered.append({
            "name": name,
            "color": _IFC_PREVIEW_COLORS.get(name, _IFC_PREVIEW_COLORS["Other"]),
            "positionsB64": _enc(d["positions"]),
            "indicesB64":   _enc(d["indices"]),
            "vertexCount":   len(d["positions"]) // 3,
            "triangleCount": len(d["indices"])   // 3,
        })

    return {
        "kind": "ifc",
        "sourceName": ifc_path.name,
        "bounds": {
            "min": bmin if bmin[0] != float("inf") else None,
            "max": bmax if bmax[0] != float("-inf") else None,
        },
        "stats": stats,
        "meshes": ordered,
    }


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.post("/api/estimate")
async def estimate_endpoint(
    file: UploadFile = File(...),
    cost_per_sqft: float = Form(default=40.0),
    cost_per_linear_ft: float = Form(default=2.5),
    source_length_unit: str = Form(default=""),
    strict_mode: bool = Form(default=False),
) -> dict:
    _validate_upload(file)
    tmp_path = await _save_upload(file)
    try:
        from bison_scope_estimator.v3_logic import estimate_from_file_v3

        unit = source_length_unit.strip() or None
        result = estimate_from_file_v3(
            file_path=tmp_path,
            cost_per_sqft=cost_per_sqft,
            cost_per_linear_ft=cost_per_linear_ft,
            source_length_unit=unit,
            strict_mode=strict_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Estimation failed: {exc}")
    finally:
        _cleanup(tmp_path)

    m = result.measurements
    b = result.budget
    c = result.confidence
    return {
        "success": True,
        "file_name": file.filename,
        "format": m.source_format,
        "unit": m.source_unit,
        "area_sqft": round(m.area_sqft, 2),
        "area_basis": m.area_basis,
        "linear_ft": round(m.linear_ft, 2),
        "linear_basis": m.linear_basis,
        "framing_element_count": m.framing_element_count,
        "cost_per_sqft": cost_per_sqft,
        "cost_per_linear_ft": cost_per_linear_ft,
        "area_cost": round(b.area_cost, 2),
        "linear_cost": round(b.linear_cost, 2),
        "combined_cost": round(b.combined_cost, 2),
        "overall_cost": round(result.overall_cost, 2),
        "estimate_low": round(result.estimate_low, 2),
        "estimate_high": round(result.estimate_high, 2),
        "overall_basis": result.overall_basis,
        "area_weight": round(result.overall_area_weight, 3),
        "linear_weight": round(result.overall_linear_weight, 3),
        "confidence": {
            "area": c.area_confidence,
            "linear": c.linear_confidence,
            "overall": c.overall_confidence,
            "warnings": list(c.warnings),
        },
    }


@app.post("/api/ifc-preview")
async def ifc_preview_endpoint(file: UploadFile = File(...)) -> dict:
    """Return base64-encoded bucket meshes for the Three.js IFC viewer (Patch1 format)."""
    if not file.filename or not file.filename.lower().endswith(".ifc"):
        raise HTTPException(status_code=400, detail="Only .ifc files are accepted here.")
    tmp_path = await _save_upload(file)
    try:
        return _build_ifc_preview_payload(tmp_path)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"IFC preview failed: {exc}")
    finally:
        _cleanup(tmp_path)


@app.post("/api/geometry")
async def geometry_endpoint(file: UploadFile = File(...)) -> dict:
    _validate_upload(file)
    tmp_path = await _save_upload(file)
    try:
        from web.geometry_export import export_geometry

        return export_geometry(tmp_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Geometry export failed: {exc}")
    finally:
        _cleanup(tmp_path)


# ── helpers ──────────────────────────────────────────────────────────────────

def _validate_upload(file: UploadFile) -> None:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".dxf", ".ifc"}:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{suffix}'. Use .dxf or .ifc.")


async def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename).suffix.lower()
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        os.close(fd)
        contents = await file.read()
        Path(tmp).write_bytes(contents)
    except Exception:
        _cleanup(Path(tmp))
        raise
    return Path(tmp)


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
