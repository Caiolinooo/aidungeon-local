import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm'
import { EMOTIONS } from './vrmConfig.js'

// Visualizador 3D do avatar VRM de um NPC (v0.4.0).
//
// Um único canvas por vez (a aba só renderiza este componente para o NPC
// selecionado). Props:
//   url     — caminho do .vrm (ex.: '/vrm/base.vrm')
//   colors  — tints multiplicativos { hair, skin, eyes, top, bottom } em '#rrggbb'
//             ('#ffffff' = cor original do modelo)
//   emotion — preset de expressão alvo ('neutral'|'happy'|...) com lerp suave
//
// Heurística de classificação de materiais (modelo .vrm genérico não garante
// nomes): casa por nome do MATERIAL e, em falta, pelo nome da MESH, em
// minúsculas, na ordem de prioridade abaixo (a primeira que casa vence):
//   eyes   — 'eye' | 'iris'
//   hair   — 'hair'                       (fallback: materiais das meshes sob
//                                          o osso 'head', se nada casar)
//   top    — 'torso' | 'cloth' | 'top' | 'upper' | 'shirt' | 'jacket'
//   bottom — 'leg' | 'skirt' | 'pants' | 'bottom' | 'lower' | 'shorts'
//   skin   — 'skin' | 'face' | 'body' | 'head'
// O tint é aplicado multiplicando material.color (MToon/MeshStandard) e
// material.emissive (quando existe) pela cor alvo; a cor original é cacheada
// no load e restaurada quando o NPC troca (o VRM anterior é descarregado).

const EMOTION_LERP_SPEED = 6 // ~quanto maior, mais rápido o lerp de expressão
const BLINK_INTERVAL_MIN = 2.2 // segundos entre piscadas (sorteado)
const BLINK_INTERVAL_SPAN = 3.5
const BLINK_DURATION = 0.18 // duração total (fecha+abre) da piscada

// Classifica mesh/material numa categoria de tint (ver heurística no cabeçalho).
function classifyMaterial(meshName, matName) {
  const hay = `${matName} ${meshName}`.toLowerCase()
  if (/(eye|iris)/.test(hay)) return 'eyes'
  if (/hair/.test(hay)) return 'hair'
  if (/(torso|cloth|top|upper|shirt|jacket)/.test(hay)) return 'top'
  if (/(leg|skirt|pants|bottom|lower|shorts)/.test(hay)) return 'bottom'
  if (/(skin|face|body|head)/.test(hay)) return 'skin'
  return null
}

export default function VrmAvatar({ url, colors, emotion }) {
  const mountRef = useRef(null)
  // Estado mutável do render loop (não dispara re-render do React).
  const st = useRef({
    renderer: null,
    scene: null,
    camera: null,
    controls: null,
    clock: null,
    raf: 0,
    vrm: null,
    vrmRoot: null,
    chestBaseRotation: null, // rotação original do peito p/ o "breathing"
    originalColors: new Map(), // material.uuid -> { color, emissive }
    materialSlots: [], // [{ material, category }] p/ reaplicar tints
    weights: {}, // peso atual de cada preset de expressão
    targetEmotion: 'neutral',
    colors: null,
    nextBlinkAt: 0,
    blinkT: -1, // progresso da piscada em curso (-1 = parado)
    disposed: false,
  })

  // ---------------------------------------------------------------- setup 3D
  useEffect(() => {
    const s = st.current
    s.disposed = false
    const mount = mountRef.current

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2)) // cap 2
    mount.appendChild(renderer.domElement)
    s.renderer = renderer

    s.scene = new THREE.Scene()
    s.camera = new THREE.PerspectiveCamera(28, 1, 0.05, 20)
    s.camera.position.set(0, 1.4, 1.1)

    const dir = new THREE.DirectionalLight(0xffffff, Math.PI)
    dir.position.set(0.5, 1, 1)
    s.scene.add(dir)
    s.scene.add(new THREE.HemisphereLight(0xffffff, 0x555566, Math.PI * 0.6))

    // OrbitControls somente rotação (sem pan/zoom), conforme spec.
    const controls = new OrbitControls(s.camera, renderer.domElement)
    controls.enablePan = false
    controls.enableZoom = false
    controls.enableDamping = true
    s.controls = controls

    s.clock = new THREE.Clock()
    s.nextBlinkAt = BLINK_INTERVAL_MIN + Math.random() * BLINK_INTERVAL_SPAN
    for (const e of EMOTIONS) s.weights[e] = 0
    s.weights.blink = 0

    // Redimensiona com o container.
    const resize = () => {
      const w = mount.clientWidth || 1
      const h = mount.clientHeight || 1
      renderer.setSize(w, h)
      s.camera.aspect = w / h
      s.camera.updateProjectionMatrix()
    }
    const ro = new ResizeObserver(resize)
    ro.observe(mount)
    resize()

    // ---------------------------------------------------------- render loop
    // Sem alocações por frame: vetores/objetos temporários criados 1 vez.
    const tick = () => {
      s.raf = requestAnimationFrame(tick)
      const dt = Math.min(s.clock.getDelta(), 0.1)
      const t = s.clock.elapsedTime
      const vrm = s.vrm

      if (vrm) {
        // Lerp suave dos pesos de expressão em direção à emoção alvo.
        const k = Math.min(1, dt * EMOTION_LERP_SPEED)
        const em = vrm.expressionManager
        if (em) {
          for (const e of EMOTIONS) {
            const target = e === s.targetEmotion ? 1 : 0
            s.weights[e] += (target - s.weights[e]) * k
            em.setValue(e, s.weights[e])
          }
          // Piscada automática (saccade timer): curva triangular 0->1->0.
          if (s.blinkT < 0 && t >= s.nextBlinkAt) s.blinkT = 0
          if (s.blinkT >= 0) {
            s.blinkT += dt
            const p = s.blinkT / BLINK_DURATION
            em.setValue('blink', p < 1 ? 1 - Math.abs(2 * p - 1) : 0)
            if (p >= 1) {
              s.blinkT = -1
              s.nextBlinkAt = t + BLINK_INTERVAL_MIN + Math.random() * BLINK_INTERVAL_SPAN
            }
          }
        }
        // "Breathing": leve oscilação senoidal na rotação do peito.
        const chest =
          vrm.humanoid?.getNormalizedBoneNode('upperChest') ||
          vrm.humanoid?.getNormalizedBoneNode('chest')
        if (chest && s.chestBaseRotation) {
          chest.rotation.x = s.chestBaseRotation.x + Math.sin(t * 1.6) * 0.015
          chest.rotation.z = s.chestBaseRotation.z + Math.sin(t * 0.8) * 0.008
        }
        vrm.update(dt)
      }

      s.controls.update()
      s.renderer.render(s.scene, s.camera)
    }
    tick()

    return () => {
      s.disposed = true
      cancelAnimationFrame(s.raf)
      ro.disconnect()
      controls.dispose()
      unloadVrm(s)
      renderer.dispose()
      renderer.domElement.remove()
      s.renderer = null
      s.scene = null
      s.camera = null
      s.controls = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ------------------------------------------------------- carregar o .vrm
  useEffect(() => {
    const s = st.current
    let cancelled = false
    unloadVrm(s)

    const loader = new GLTFLoader()
    loader.register((parser) => new VRMLoaderPlugin(parser))
    loader.load(
      url,
      (gltf) => {
        if (cancelled || s.disposed) {
          VRMUtils.deepDispose(gltf.scene)
          return
        }
        const vrm = gltf.userData.vrm
        VRMUtils.removeUnnecessaryVertices(gltf.scene)
        s.scene.add(vrm.scene)
        s.vrm = vrm
        s.vrmRoot = gltf.scene

        // Olhar em direção à câmera (o update acontece em vrm.update()).
        if (vrm.lookAt) vrm.lookAt.target = s.camera

        // Cache da rotação original do peito (base do "breathing").
        const chest =
          vrm.humanoid?.getNormalizedBoneNode('upperChest') ||
          vrm.humanoid?.getNormalizedBoneNode('chest')
        if (chest) s.chestBaseRotation = chest.rotation.clone()

        // Enquadra o busto: câmera na altura da cabeça, olhando para ela.
        const headPos = new THREE.Vector3()
        const head = vrm.humanoid?.getNormalizedBoneNode('head')
        if (head) {
          head.getWorldPosition(headPos)
        } else {
          headPos.set(0, 1.4, 0)
        }
        s.camera.position.set(headPos.x, headPos.y + 0.05, headPos.z + 0.85)
        s.controls.target.copy(headPos)
        s.controls.update()

        // Cache das cores originais + classificação dos materiais.
        cacheMaterials(s, vrm)
        applyColors(s)
      },
      undefined,
      (err) => {
        console.error('Falha ao carregar o modelo VRM:', err)
      },
    )

    return () => {
      cancelled = true
    }
  }, [url])

  // ------------------------------------------- props -> estado do render loop
  useEffect(() => {
    const s = st.current
    s.targetEmotion = EMOTIONS.includes(emotion) ? emotion : 'neutral'
  }, [emotion])

  useEffect(() => {
    const s = st.current
    s.colors = colors
    applyColors(s)
  }, [colors])

  return <div ref={mountRef} className="h-72 w-full overflow-hidden rounded-lg bg-zinc-900" />
}

// Descarrega o VRM atual (troca de NPC/modelo ou unmount).
function unloadVrm(s) {
  if (!s.vrmRoot) return
  if (s.scene) s.scene.remove(s.vrmRoot)
  VRMUtils.deepDispose(s.vrmRoot) // geometries + materials + texturas
  s.vrm = null
  s.vrmRoot = null
  s.chestBaseRotation = null
  s.originalColors.clear()
  s.materialSlots = []
}

// Percorre as meshes do VRM, cacheia as cores originais dos materiais e
// classifica cada material numa categoria de tint (heurística no cabeçalho).
function cacheMaterials(s, vrm) {
  s.originalColors.clear()
  s.materialSlots = []
  const seen = new Set()
  vrm.scene.traverse((obj) => {
    if (!obj.isMesh) return
    const mats = Array.isArray(obj.material) ? obj.material : [obj.material]
    for (const mat of mats) {
      if (!mat || seen.has(mat.uuid)) continue
      seen.add(mat.uuid)
      s.originalColors.set(mat.uuid, {
        color: mat.color ? mat.color.clone() : null,
        emissive: mat.emissive ? mat.emissive.clone() : null,
      })
      s.materialSlots.push({ material: mat, category: classifyMaterial(obj.name, mat.name) })
    }
  })

  // Fallback do cabelo: se nenhum material casou com 'hair', usa os
  // materiais das meshes que estão sob o osso 'head'.
  if (!s.materialSlots.some((slot) => slot.category === 'hair')) {
    const head = vrm.humanoid?.getNormalizedBoneNode('head')
    if (head) {
      const headMats = new Set()
      head.traverse((obj) => {
        if (!obj.isMesh) return
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material]
        for (const mat of mats) if (mat) headMats.add(mat.uuid)
      })
      for (const slot of s.materialSlots) {
        if (headMats.has(slot.material.uuid) && slot.category !== 'eyes') slot.category = 'hair'
      }
    }
  }
}

// Aplica os tints multiplicativos sobre as cores originais cacheadas.
function applyColors(s) {
  if (!s.colors || s.materialSlots.length === 0) return
  const tint = new THREE.Color()
  for (const { material, category } of s.materialSlots) {
    const original = s.originalColors.get(material.uuid)
    if (!original || !category) continue
    const hex = s.colors[category] || '#ffffff'
    if (original.color && material.color) {
      material.color.copy(original.color).multiply(tint.set(hex))
    }
    // MToon pode ter emissive; acompanha o tint para não destoar.
    if (original.emissive && material.emissive) {
      material.emissive.copy(original.emissive).multiply(tint.set(hex))
    }
  }
}
