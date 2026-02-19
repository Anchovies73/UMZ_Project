import * as THREE from '../vendor/three/build/three.module.js';
import { GLTFLoader } from '../vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { EffectComposer } from '../vendor/three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from '../vendor/three/examples/jsm/postprocessing/RenderPass.js';
import { OutlinePass } from '../vendor/three/examples/jsm/postprocessing/OutlinePass.js';

// =====================
// CONFIG
// =====================
const MODEL_URL = './assets/model/model.glb';
const ANIM_URL = './assets/anim/three_animation1.json';

const MODEL_AXIS_FIX_X = -Math.PI / 2;
const GLTF_CAMERA_NAME = 'Camera';

// =====================
// THREE BASE
// =====================
const canvas = document.getElementById('c');

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.1;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x8ccEF4);

// Фолбэк камера (если в glTF нет камеры или не нашли по имени)
const fallbackCamera = new THREE.PerspectiveCamera(
  55,
  window.innerWidth / window.innerHeight,
  0.01,
  5000
);

// Камера, которой реально рендерим
let activeCamera = fallbackCamera;

// Свет
scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.9));
const keyLight = new THREE.DirectionalLight(0xffffff, 1.2);
keyLight.position.set(6, 10, 4);
scene.add(keyLight);

// =====================
// HELPERS
// =====================
function renameNodesFromGltfId(root) {
  root.traverse((o) => {
    // не переименовываем камеру, чтобы её можно было стабильно найти по имени
    if (o.isCamera && o.name === GLTF_CAMERA_NAME) return;
    const id = o?.userData?.gltf_id;
    if (typeof id === 'string' && id.length > 0) o.name = id;
  });
}

function frameFallbackCamera(object3D, cam) {
  const box = new THREE.Box3().setFromObject(object3D);
  const size = box.getSize(new THREE.Vector3()).length();
  const center = box.getCenter(new THREE.Vector3());

  cam.position.copy(center).add(new THREE.Vector3(size * 0.25, size * 0.15, size * 0.25));
  cam.lookAt(center);
  cam.updateProjectionMatrix();
}

function updateCameraAspect(cam) {
  const aspect = window.innerWidth / window.innerHeight;

  if (cam && cam.isPerspectiveCamera) {
    cam.aspect = aspect;
    cam.updateProjectionMatrix();
  } else if (cam && cam.isOrthographicCamera) {
    cam.updateProjectionMatrix();
  }
}

// ----- alpha_tracks runtime helpers -----
function sampleNumberTrack(times, values, t) {
  const n = times.length;
  if (n === 0) return 1.0;
  if (n === 1) return values[0];

  if (t <= times[0]) return values[0];
  if (t >= times[n - 1]) return values[n - 1];

  for (let i = 0; i < n - 1; i++) {
    const t0 = times[i], t1 = times[i + 1];
    if (t >= t0 && t <= t1) {
      const k = (t1 - t0) > 0 ? (t - t0) / (t1 - t0) : 0;
      return values[i] + (values[i + 1] - values[i]) * k;
    }
  }
  return values[n - 1];
}

function ensureUniqueMaterialsForSubtree(rootObj) {
  rootObj.traverse((o) => {
    if (!o.isMesh) return;

    if (Array.isArray(o.material)) {
      o.material = o.material.map((m) => (m ? m.clone() : m));
    } else if (o.material) {
      o.material = o.material.clone();
    }
  });
}

function applyAlphaToSubtree(rootObj, alpha, eps = 1e-4) {
  const a = THREE.MathUtils.clamp(alpha, 0, 1);

  rootObj.traverse((o) => {
    if (!o.isMesh) return;

    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
    for (const mat of mats) {
      if (!mat) continue;

      mat.opacity = a;
      mat.transparent = a < 1.0 - eps;
      mat.depthWrite = a >= 1.0 - eps;
      mat.needsUpdate = true;
    }
  });
}

// ====== visibility filtering with parent support ======
function setMeshRenderInvisible(mesh) {
  if (!mesh.isMesh) return;

  const mats = Array.isArray(mesh.material) ? mesh.material : (mesh.material ? [mesh.material] : []);
  for (const mat of mats) {
    if (!mat) continue;

    // "не рисуем", но узел остаётся видимым для детей/трансформов
    mat.transparent = true;
    mat.opacity = 0.0;
    mat.depthWrite = false;

    // критично: не писать цвет (реально пропадает из кадра)
    if ('colorWrite' in mat) mat.colorWrite = false;

    mat.needsUpdate = true;
  }

  mesh.castShadow = false;
  mesh.receiveShadow = false;
}

function applySelectiveVisibilityWithParents(modelRoot, animData) {
  const mode = animData?.visible_nodes_mode ?? 'ALL';

  // ALL: показываем все ноды, у которых есть gltf_id (остальные не трогаем)
  if (mode !== 'SELECTED') {
    modelRoot.traverse((o) => {
      if (o.isCamera) return;
      const id = o?.userData?.gltf_id;
      if (id == null) return;
      o.visible = true;
    });
    return;
  }

  const list = Array.isArray(animData?.visible_nodes) ? animData.visible_nodes : [];
  const visibleSet = new Set(list.map(String));

  // 1) найдём таргетные узлы
  const targetNodes = new Set();
  modelRoot.traverse((o) => {
    if (o.isCamera) return;
    const id = o?.userData?.gltf_id;
    if (id == null) return;
    if (visibleSet.has(String(id))) targetNodes.add(o);
  });

  // 2) соберём всех предков таргетных узлов
  const requiredParents = new Set();
  for (const node of targetNodes) {
    let p = node.parent;
    while (p && p !== modelRoot.parent) {
      requiredParents.add(p);
      p = p.parent;
    }
  }

  // 3) применим видимость
  modelRoot.traverse((o) => {
    if (o.isCamera) return;

    const id = o?.userData?.gltf_id;
    if (id == null) return; // служебные ноды не трогаем

    const isTarget = targetNodes.has(o);
    const isRequiredParent = requiredParents.has(o);

    if (isTarget) {
      o.visible = true;
    } else if (isRequiredParent) {
      // предок нужен как контейнер трансформов/иерархии
      o.visible = true;

      // но если это mesh — геометрию не рисуем (требование)
      if (o.isMesh) {
        // чтобы не сломать общие материалы у других мешей
        ensureUniqueMaterialsForSubtree(o);
        setMeshRenderInvisible(o);
      }
    } else {
      // не нужен ни сам, ни как предок
      o.visible = false;
    }
  });
}

// =====================
// RUN
// =====================
let composer = null;
let outlinePass = null;
let mixer = null;
let action = null;
let alphaItems = []; // [{ obj, times: number[], values: number[] }]
let readyToRender = false;
const clock = new THREE.Clock();

new GLTFLoader().load(
  MODEL_URL,
  async (gltf) => {
    const modelWrapper = new THREE.Group();
    modelWrapper.rotation.x = MODEL_AXIS_FIX_X;

    const modelRoot = gltf.scene;
    renameNodesFromGltfId(modelRoot);

    modelWrapper.add(modelRoot);

    // Mixer анимирует modelRoot, включая камеру, если она внутри него
    mixer = new THREE.AnimationMixer(modelRoot);

    // 1) Пытаемся взять камеру из glTF
    const gltfCamByName = modelRoot.getObjectByName(GLTF_CAMERA_NAME);
    const gltfCam = (gltfCamByName && gltfCamByName.isCamera)
      ? gltfCamByName
      : (gltf.cameras && gltf.cameras.length ? gltf.cameras[0] : null);

    if (gltfCam && gltfCam.isCamera) {
      activeCamera = gltfCam;
      updateCameraAspect(activeCamera);
      console.log('Using glTF camera for render:', activeCamera.name, activeCamera.uuid);
    } else {
      // 2) Фолбэк камера, если glTF камеры нет
      activeCamera = fallbackCamera;
      frameFallbackCamera(modelWrapper, activeCamera);
      updateCameraAspect(activeCamera);
      console.log('Using fallback camera for render');
    }

    // Load anim JSON once: tracks + alpha_tracks + visible_nodes
    const res = await fetch(ANIM_URL);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const animData = await res.json();

    // ВАЖНО: фильтрация видимости с поддержкой родителей
    applySelectiveVisibilityWithParents(modelRoot, animData);

    // ===== Post-processing: EffectComposer + OutlinePass =====
    composer = new EffectComposer(renderer);
    composer.renderTarget1.texture.colorSpace = THREE.SRGBColorSpace;
    if (composer.renderTarget2) composer.renderTarget2.texture.colorSpace = THREE.SRGBColorSpace;
    composer.addPass(new RenderPass(scene, activeCamera));

    outlinePass = new OutlinePass(
      new THREE.Vector2(window.innerWidth, window.innerHeight),
      scene,
      activeCamera
    );
    outlinePass.visibleEdgeColor.set(0xff0033);
    outlinePass.hiddenEdgeColor.set(0xff0033);
    outlinePass.edgeThickness = 6.0;
    outlinePass.edgeStrength = 9.0;
    outlinePass.edgeGlow = 0.5;
    outlinePass.pulsePeriod = 2;

    // Collect all renderable meshes under nodes marked with userData.red
    const outlineMeshes = new Set();
    modelRoot.traverse((o) => {
      const red = o?.userData?.red;
      if (typeof red !== 'string' || red.length === 0) return;
      // traverse includes o itself — adds o if mesh, plus all mesh descendants
      o.traverse((child) => {
        if (child.isMesh || child.isSkinnedMesh) outlineMeshes.add(child);
      });
    });
    outlinePass.selectedObjects = [...outlineMeshes];

    composer.addPass(outlinePass);
    // ===== End OutlinePass setup =====

    // Build clip from animData.tracks
    const tracks = [];
    for (const t of (animData.tracks || [])) {
      if (!t || !t.type || !t.name) continue;

      const times = new Float32Array(t.times || []);
      const values = new Float32Array(t.values || []);

      if (t.type === 'vector') tracks.push(new THREE.VectorKeyframeTrack(t.name, times, values));
      if (t.type === 'quaternion') tracks.push(new THREE.QuaternionKeyframeTrack(t.name, times, values));
      if (t.type === 'number') tracks.push(new THREE.NumberKeyframeTrack(t.name, times, values));
    }

    const duration = (typeof animData.duration === 'number') ? animData.duration : -1;
    const clip = new THREE.AnimationClip(animData.name || 'clip', duration, tracks);

    action = mixer.clipAction(clip);
    action.reset();
    action.play();

    // чтобы камера/мир сразу оказался в 0-й позе
    mixer.setTime(0);
    modelRoot.updateMatrixWorld(true);
    activeCamera.updateMatrixWorld(true);

    console.log('Camera after setTime(0):', activeCamera.position.toArray(), activeCamera.quaternion.toArray());

    // Prepare alpha items (manual runtime, with material cloning to avoid shared-material issues)
    alphaItems = [];
    for (const tr of (animData.alpha_tracks || [])) {
      if (!tr || !tr.node || !Array.isArray(tr.times) || !Array.isArray(tr.values)) continue;

      const obj = modelRoot.getObjectByName(tr.node);
      if (!obj) continue;

      ensureUniqueMaterialsForSubtree(obj);
      alphaItems.push({ obj, times: tr.times, values: tr.values });
    }

    scene.add(modelWrapper);
    readyToRender = true;
  },
  undefined,
  (err) => console.error('Failed to load model:', err)
);

window.addEventListener('resize', () => {
  renderer.setSize(window.innerWidth, window.innerHeight);
  updateCameraAspect(activeCamera);
  if (composer) composer.setSize(window.innerWidth, window.innerHeight);
  if (outlinePass) outlinePass.setSize(window.innerWidth, window.innerHeight);
});

function animate() {
  requestAnimationFrame(animate);

  const dt = clock.getDelta();
  if (mixer) mixer.update(dt);

  // alpha_tracks manual update (after mixer.update)
  if (action && alphaItems.length) {
    const t = action.time;
    for (const it of alphaItems) {
      const a = sampleNumberTrack(it.times, it.values, t);
      applyAlphaToSubtree(it.obj, a);
    }
  }

  if (!readyToRender) return;
  if (composer) composer.render();
  else renderer.render(scene, activeCamera);
}

animate();
