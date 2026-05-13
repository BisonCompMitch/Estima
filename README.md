# BisonScope Estimator

Python app to estimate a project budget from a `DXF` or `IFC` file using:

- total area (`sq ft`)
- total framing length (`ft`)
- user-provided cost rates

It compares:

- area-based cost (`area_sqft * cost_per_sqft`)
- framing-length cost (`linear_ft * cost_per_linear_ft`)
- combined cost (area + linear)

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Git Bash activation:

```bash
source .venv/Scripts/activate
```

## Run GUI (default)

```powershell
python estimate.py
```

You can also force GUI mode:

```powershell
python estimate.py --gui
```

In GUI mode you can click `Preview 3D` to open a quick interactive DXF or IFC preview.
Preview rendering uses `matplotlib` (software-rendered), so it does not depend on GPU OpenGL drivers.
Inside preview, use `Shell`, `Full`, and `Detail` modes, `Fast` / `Std` / `HQ` quality levels, and `Top` / `Iso` camera buttons to tune clarity vs speed.

## Run V2 (strict mode + mapping rules + audit export)

```powershell
python BisonScopeV2.py
```

V2 defaults to GUI and adds:

- strict mode toggle (disables fallback calculations)
- configurable mapping rules file (`bisonscope_v2_rules.json`)
- audit table and CSV export

CLI example:

```powershell
python BisonScopeV2.py --cli .\path\to\model.ifc --cost-per-sqft 40 --cost-per-linear-ft 2.5 --strict --json --export-audit-csv .\audit.csv
```

## Run V3 (PyVista/VTK preview)

```powershell
python BisonScopeV3.py
```

V3 keeps the original estimate flow from `estimate.py` and swaps the preview window to an embedded PyVista/VTK renderer.

V3 adds:

- strict mode for authored-quantity-only estimates
- confidence reporting for area and framing totals
- a confidence-weighted overall estimate for each project
- DXF block insert traversal for nested geometry
- explicit IFC area-source priority: `IfcSpace` quantities, then generic area fallbacks, then geometry fallbacks

If the preview dependencies are missing, install them with:

```powershell
python -m pip install pyvista pyvistaqt vtk
```

V3 CLI example:

```powershell
python BisonScopeV3.py .\path\to\model.ifc --cost-per-sqft 40 --cost-per-linear-ft 2.5 --strict --json
```

## Run CLI

```powershell
python estimate.py .\path\to\model.dxf --cost-per-sqft 195 --cost-per-linear-ft 45
python estimate.py .\path\to\model.ifc --cost-per-sqft 195 --cost-per-linear-ft 45
```

Optional unit override:

```powershell
python estimate.py --cli .\model.ifc --cost-per-sqft 195 --cost-per-linear-ft 45 --source-length-unit m
```

Supported unit values for `--source-length-unit`:

- `ft`, `m`, `in`, `mm`, `cm`, `yd`

## Notes

- DXF handling reads common geometric entities (`LINE`, `ARC`, `CIRCLE`, `LWPOLYLINE`, `POLYLINE`, `ELLIPSE`).
- DXF handling includes `3DFACE` support for both area extraction and edge-network length extraction.
- `Preview 3D` supports DXF (`3DFACE` + wire entities) and IFC mesh geometry.
- DXF framing length prioritizes entities on framing-like layers (for example names containing `frame`, `stud`, `track`, `steel`, `joist`, `beam`, `column`). If none are found, it falls back to all geometry length.
- IFC framing length prioritizes framing elements (`IfcMember`, `IfcBeam`, `IfcColumn`, plus framing keyword matches in object metadata), then derives length from element quantities or element geometry (for example `IfcExtrudedAreaSolid.Depth`) per element.
- IFC falls back to all `IfcQuantityLength` totals only when framing element extraction returns no usable lengths.
- IFC scaling is read from `UnitsInContext` using IFC `LENGTHUNIT` and `AREAUNIT` scale factors, so conversion-based project units are sized correctly. You can still override with `--source-length-unit` when needed.
- IFC floor area extraction uses priority order: `IfcSpace` floor areas, then storey/building floor quantities, then slab floor quantities.
- If no explicit floor quantities are found, IFC floor area falls back to framing geometry footprint hull multiplied by storey count.
- `Preview 3D` now caches parsed preview geometry by file signature (path + modified timestamp + size), so reopening the same model is near-instant.
- IFC preview uses multithreaded geometry extraction plus sampled entity inclusion so multi-level models render faster while preserving full-building coverage.
- DXF preview deduplicates overlapping segments and adaptively tessellates arcs/circles for better performance.
- The app reports `area_basis` so you can verify exactly what area source was used.
- For DXF files, feet (`ft`) are assumed unless overridden.
- If `ifcopenshell` install fails on your Python version, use Python 3.11 or 3.12 for best package compatibility.
