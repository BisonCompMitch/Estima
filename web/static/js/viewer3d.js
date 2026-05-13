/**
 * BisonScope 3D Viewer — renders /api/geometry payloads.
 *
 * DXF payload:  { type:"dxf", segments:[], framing_segments:[], face_verts:[], face_indices:[] }
 * IFC payload:  { type:"ifc", groups:{ TypeName:{ verts:[], faces:[], color:"#hex" } } }
 *
 * Both IFC and 3D DXF use Z-up coordinates; remapped to Three.js Y-up here.
 */

import * as THREE from "https://esm.sh/three@0.164.1";
import { OrbitControls } from "https://esm.sh/three@0.164.1/examples/jsm/controls/OrbitControls.js";

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
    this._initScene();
    this._initCamera();
    this._initRenderer();
    this._initControls();
    this._initLights();
    this._initGrid();
    this._startLoop();
    this._initResize();
  }

  // ── public ──────────────────────────────────────────────────────────────

  /** Load a /api/geometry response. Dispatches on payload.type. */
  loadGeometry(payload) {
    this._clear();
    if (payload.type === "dxf") return this._loadDXF(payload);
    if (payload.type === "ifc") return this._loadIFC(payload);
    return {};
  }

  resetCamera() {
    if (this._current) this._fitCamera(this._current);
  }

  destroy() {
    this._clear();
    this._renderer.dispose();
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
