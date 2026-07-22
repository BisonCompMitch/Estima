/**
 * BisonScope 3D Viewer — renders /api/geometry payloads.
 *
 * DXF payload:  { type:"dxf", segments:[], framing_segments:[], face_verts:[], face_indices:[] }
 * IFC payload:  { type:"ifc", groups:{ TypeName:{ verts:[], faces:[], color:"#hex" } } }
 *
 * Both IFC and 3D DXF use Z-up coordinates; remapped to Three.js Y-up here.
 */

import * as THREE from "../vendor/three.module.js";
import { OrbitControls } from "../vendor/OrbitControls.js";

const WEB_IFC_API_URL = "../vendor/web-ifc-api.js";
const WEB_IFC_WASM_PATH = new URL("../vendor/", import.meta.url).href;

const IFC_SKIP_TYPES = new Set([
  "IFCSPACE", "IFCOPENINGELEMENT", "IFCVIRTUALELEMENT",
  "IFCANNOTATION", "IFCGRID", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY",
]);

const IFC_TYPE_COLORS = {
  IFCWALL: "#6B7FA8",
  IFCWALLSTANDARDCASE: "#6B7FA8",
  IFCCURTAINWALL: "#A8C8D8",
  IFCSLAB: "#B8956A",
  IFCROOF: "#9E5C38",
  IFCCOLUMN: "#4A7FA8",
  IFCBEAM: "#4A7FA8",
  IFCMEMBER: "#E8C840",
  IFCPLATE: "#D4A820",
  IFCDOOR: "#7A5814",
  IFCWINDOW: "#7EC8D3",
  IFCSTAIR: "#888888",
  IFCFOUNDATION: "#8B7355",
  IFCFOOTING: "#8B7355",
};

// ── coordinate remap ───────────────────────────────────────────────────────
// Z-up (IFC / 3D DXF) → Y-up (Three.js): swap Y and Z, negate new Z.
function remapZtoY(flat) {
  const arr = flat instanceof Float32Array ? flat : new Float32Array(flat);
  const out = new Float32Array(arr.length);
  for (let i = 0; i < arr.length; i += 3) {
    out[i]     = arr[i];
    out[i + 1] = arr[i + 2];
    out[i + 2] = -arr[i + 1];
  }
  return out;
}

// ── viewer class ───────────────────────────────────────────────────────────

export class BisonViewer {
  constructor(container) {
    this._container = container;
    this._current   = null;
    this._surfaceGroup = null;
    this._surfacePlaneData = {};
    this._selectedSurfaces = new Set();
    this._onSurfaceClick = null;
    this._initScene();
    this._initCamera();
    this._initRenderer();
    this._initControls();
    this._initLights();
    this._initGrid();
    this._startLoop();
    this._initResize();
    this._initSurfaceClickHandler();
  }

  // ── public ──────────────────────────────────────────────────────────────

  /** Load a /api/geometry response. Dispatches on payload.type. */
  loadGeometry(payload) {
    this._clear();
    if (payload.type === "dxf") return this._loadDXF(payload);
    if (payload.type === "ifc") return this._loadIFC(payload);
    return {};
  }

  async loadIfcFile(file, onProgress) {
    onProgress?.("Loading IFC parser...");
    const WebIFC = await import(WEB_IFC_API_URL);
    const api = new WebIFC.IfcAPI();
    api.SetWasmPath(WEB_IFC_WASM_PATH, true);
    await api.Init();

    onProgress?.("Reading IFC file...");
    const data = new Uint8Array(await file.arrayBuffer());
    const modelID = api.OpenModel(data, {
      COORDINATE_TO_ORIGIN: true,
      OPTIMIZE_PROFILES: true,
    });
    if (modelID < 0) throw new Error("IFC file could not be opened.");

    try {
      return await this._loadIfcModelFromApi(api, modelID, onProgress);
    } finally {
      api.CloseModel(modelID);
    }
  }

  resetCamera() {
    if (this._current) this._fitCamera(this._current);
  }

  destroy() {
    this._clear();
    this._renderer.dispose();
  }

  loadSurfacePlanes(planes) {
    if (this._surfaceGroup) {
      this._scene.remove(this._surfaceGroup);
      this._surfaceGroup.traverse(child => {
        child.geometry?.dispose();
        child.material?.dispose();
      });
      this._surfaceGroup = null;
    }
    this._surfacePlaneData = {};
    this._selectedSurfaces = new Set();
    if (!planes || !planes.length) return;
    const group = new THREE.Group();
    group.visible = false;
    for (const plane of planes) {
      if (!plane.verts?.length || !plane.faces?.length) continue;
      const positions = remapZtoY(plane.verts);
      const indices = new Uint32Array(plane.faces);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geo.setIndex(new THREE.BufferAttribute(indices, 1));
      geo.computeVertexNormals();
      const baseColor = plane.type === "roof" ? 0xF4A820 : 0x4488ff;
      const mat = new THREE.MeshStandardMaterial({
        color: baseColor, transparent: true, opacity: 0.38,
        side: THREE.DoubleSide, depthWrite: false,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.userData.surfaceId = plane.id;
      group.add(mesh);
      this._surfacePlaneData[plane.id] = { mesh, area: plane.area_sqft, type: plane.type, baseColor };
    }
    this._surfaceGroup = group;
    this._scene.add(group);
  }

  setSurfacesVisible(visible) {
    if (this._surfaceGroup) this._surfaceGroup.visible = visible;
  }

  deleteSelectedPlanes() {
    let removedArea = 0;
    for (const id of this._selectedSurfaces) {
      const entry = this._surfacePlaneData[id];
      if (!entry) continue;
      removedArea += entry.area;
      this._surfaceGroup.remove(entry.mesh);
      entry.mesh.geometry.dispose();
      entry.mesh.material.dispose();
      delete this._surfacePlaneData[id];
    }
    this._selectedSurfaces.clear();
    return removedArea;
  }

  getSelectedCount() { return this._selectedSurfaces.size; }

  set onSurfaceClick(fn) { this._onSurfaceClick = fn; }

  _initSurfaceClickHandler() {
    const canvas = this._renderer.domElement;
    const raycaster = new THREE.Raycaster();
    let downX = 0, downY = 0;
    canvas.addEventListener("pointerdown", (e) => { downX = e.clientX; downY = e.clientY; });
    canvas.addEventListener("pointerup", (e) => {
      const dx = e.clientX - downX, dy = e.clientY - downY;
      if (Math.sqrt(dx * dx + dy * dy) > 5) return;
      if (!this._surfaceGroup?.visible) return;
      const rect = canvas.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1,
      );
      raycaster.setFromCamera(ndc, this._camera);
      const meshes = Object.values(this._surfacePlaneData).map(d => d.mesh);
      const hits = raycaster.intersectObjects(meshes, false);
      if (!hits.length) return;
      const id = hits[0].object.userData.surfaceId;
      if (!id) return;
      const entry = this._surfacePlaneData[id];
      if (!entry) return;
      if (this._selectedSurfaces.has(id)) {
        this._selectedSurfaces.delete(id);
        entry.mesh.material.color.setHex(entry.baseColor);
        entry.mesh.material.opacity = 0.38;
      } else {
        this._selectedSurfaces.add(id);
        entry.mesh.material.color.setHex(0xff3333);
        entry.mesh.material.opacity = 0.55;
      }
      this._onSurfaceClick?.({ id, selectedCount: this._selectedSurfaces.size });
    });
  }

  // ── DXF ─────────────────────────────────────────────────────────────────

  _loadDXF(p) {
    const group = new THREE.Group();

    // Regular geometry — muted blue-gray lines
    if (p.segments?.length >= 6) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(remapZtoY(p.segments), 3));
      group.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0x4a6b8a })));
    }

    // CFS framing / 3D members — highlighted gold lines
    if (p.framing_segments?.length >= 6) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(remapZtoY(p.framing_segments), 3));
      group.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0xe8c840 })));
    }

    // 3DFACE surfaces — semi-transparent blue mesh
    if (p.face_verts?.length >= 9 && p.face_indices?.length >= 3) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(remapZtoY(p.face_verts), 3));
      geo.setIndex(p.face_indices);
      geo.computeVertexNormals();
      group.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
        color: 0x3b82f6, roughness: 0.75, metalness: 0.08,
        transparent: true, opacity: 0.65, side: THREE.DoubleSide,
      })));
    }

    this._current = group;
    this._scene.add(group);
    this._fitCamera(group);
    return { type: "dxf", unit: p.unit };
  }

  // ── IFC ─────────────────────────────────────────────────────────────────

  _loadIFC(p) {
    const group = new THREE.Group();

    for (const [etype, data] of Object.entries(p.groups || {})) {
      if (!data.verts?.length || !data.faces?.length) continue;

      const positions = remapZtoY(data.verts);
      const indices   = new Uint32Array(data.faces);

      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geo.setIndex(new THREE.BufferAttribute(indices, 1));
      geo.computeVertexNormals();
      geo.computeBoundingBox();
      geo.computeBoundingSphere();

      const hexColor = parseInt((data.color || "#aaaaaa").replace("#", ""), 16);
      const mat = new THREE.MeshStandardMaterial({
        color:       hexColor,
        roughness:   0.80,
        metalness:   0.07,
        transparent: true,
        opacity:     0.92,
        side:        THREE.DoubleSide,
      });

      const mesh = new THREE.Mesh(geo, mat);
      mesh.name = etype;
      group.add(mesh);
    }

    this._current = group;
    this._scene.add(group);
    this._fitCamera(group);
    return { type: "ifc", groups: Object.keys(p.groups || {}).length };
  }

  // ── scene setup ──────────────────────────────────────────────────────────

  async _loadIfcModelFromApi(api, modelID, onProgress) {
    onProgress?.("Generating IFC geometry...");
    const flatMeshes = api.LoadAllGeometry(modelID);
    const total = flatMeshes.size();
    const groups = {};
    let elementCount = 0;
    let placedGeometryCount = 0;
    let vertexCount = 0;

    for (let i = 0; i < total; i++) {
      const flatMesh = flatMeshes.get(i);
      const typeName = this._ifcTypeName(api, modelID, flatMesh.expressID);
      if (IFC_SKIP_TYPES.has(typeName)) {
        flatMesh.delete?.();
        continue;
      }

      const geometries = flatMesh.geometries;
      if (geometries.size() > 0) elementCount++;

      const group = groups[typeName] ||= {
        verts: [],
        faces: [],
        color: IFC_TYPE_COLORS[typeName] || "#AAAAAA",
      };

      for (let j = 0; j < geometries.size(); j++) {
        const placed = geometries.get(j);
        const geometry = api.GetGeometry(modelID, placed.geometryExpressID);
        const vertices = api.GetVertexArray(geometry.GetVertexData(), geometry.GetVertexDataSize());
        const indices = api.GetIndexArray(geometry.GetIndexData(), geometry.GetIndexDataSize());
        this._appendIfcGeometry(group, vertices, indices, placed.flatTransformation);
        vertexCount += Math.floor(vertices.length / 6);
        placedGeometryCount++;
        geometry.delete?.();
      }

      flatMesh.delete?.();

      if (i % 200 === 0) {
        const pct = total ? Math.round((i / total) * 100) : 0;
        onProgress?.(`Rendering IFC preview... ${pct}%`);
        await new Promise(resolve => requestAnimationFrame(resolve));
      }
    }

    const payload = {
      type: "ifc",
      groups: Object.fromEntries(Object.entries(groups).filter(([, data]) => data.verts.length && data.faces.length)),
      surface_planes: [],
      external_surface_sqft: 0,
    };

    if (!Object.keys(payload.groups).length) {
      throw new Error("No renderable IFC geometry was found.");
    }

    const info = this.loadGeometry(payload);
    return { ...info, elementCount, placedGeometryCount, vertexCount };
  }

  _ifcTypeName(api, modelID, expressID) {
    try {
      const typeCode = api.GetLineType(modelID, expressID);
      return String(api.GetNameFromTypeCode(typeCode) || "IFCUNKNOWN").toUpperCase();
    } catch {
      return "IFCUNKNOWN";
    }
  }

  _appendIfcGeometry(group, vertices, indices, matrix) {
    const m = matrix && matrix.length >= 16 ? matrix : null;
    const offset = group.verts.length / 3;

    for (let i = 0; i < vertices.length; i += 6) {
      const x = vertices[i];
      const y = vertices[i + 1];
      const z = vertices[i + 2];
      if (m) {
        group.verts.push(
          m[0] * x + m[4] * y + m[8] * z + m[12],
          m[1] * x + m[5] * y + m[9] * z + m[13],
          m[2] * x + m[6] * y + m[10] * z + m[14],
        );
      } else {
        group.verts.push(x, y, z);
      }
    }

    for (let i = 0; i < indices.length; i++) {
      group.faces.push(indices[i] + offset);
    }
  }

  _initScene() {
    this._scene = new THREE.Scene();
    this._scene.background = new THREE.Color(0x09111d);
  }

  _initCamera() {
    this._camera = new THREE.PerspectiveCamera(55, 1, 0.1, 200000);
    this._camera.position.set(12, 12, 12);
  }

  _initRenderer() {
    this._renderer = new THREE.WebGLRenderer({ antialias: true });
    const w = this._container.clientWidth  || 800;
    const h = this._container.clientHeight || 600;
    this._renderer.setSize(w, h);
    this._renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this._camera.aspect = w / h;
    this._camera.updateProjectionMatrix();
    this._container.appendChild(this._renderer.domElement);
  }

  _initControls() {
    this._controls = new OrbitControls(this._camera, this._renderer.domElement);
    this._controls.enableDamping = true;
  }

  _initLights() {
    this._scene.add(new THREE.HemisphereLight(0xc8d8f0, 0x1a2535, 0.90));
    const dir = new THREE.DirectionalLight(0xffffff, 1.1);
    dir.position.set(10, 16, 8);
    this._scene.add(dir);
  }

  _initGrid() {
    this._scene.add(new THREE.GridHelper(200, 60, 0x1e3a5f, 0x162840));
    this._scene.add(new THREE.AxesHelper(3));
  }

  _fitCamera(object) {
    const bbox = new THREE.Box3().setFromObject(object);
    if (bbox.isEmpty()) return;
    const center = bbox.getCenter(new THREE.Vector3());
    const size   = bbox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z, 1);
    const dist   = maxDim * 1.5;
    this._camera.position.set(center.x + dist, center.y + dist * 0.65, center.z + dist);
    this._camera.near = maxDim / 1000;
    this._camera.far  = maxDim * 300;
    this._camera.updateProjectionMatrix();
    this._controls.target.copy(center);
    this._controls.update();
  }

  _clear() {
    if (this._surfaceGroup) {
      this._scene.remove(this._surfaceGroup);
      this._surfaceGroup.traverse(child => {
        child.geometry?.dispose();
        child.material?.dispose();
      });
      this._surfaceGroup = null;
    }
    this._surfacePlaneData = {};
    this._selectedSurfaces = new Set();
    if (!this._current) return;
    this._scene.remove(this._current);
    this._current.traverse((child) => {
      child.geometry?.dispose();
      if (Array.isArray(child.material)) child.material.forEach(m => m.dispose());
      else child.material?.dispose();
    });
    this._current = null;
  }

  _startLoop() {
    const tick = () => {
      requestAnimationFrame(tick);
      this._controls.update();
      this._renderer.render(this._scene, this._camera);
    };
    tick();
  }

  _initResize() {
    new ResizeObserver(() => {
      const w = this._container.clientWidth;
      const h = this._container.clientHeight;
      if (!w || !h) return;
      this._camera.aspect = w / h;
      this._camera.updateProjectionMatrix();
      this._renderer.setSize(w, h);
    }).observe(this._container);
  }
}
