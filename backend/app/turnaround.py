"""Vistas pintadas uma a uma, folhas montadas em grade fixa, giro de reserva
e portão de qualidade do clipe 360°.

As vistas eram recortes de uma ficha 3×2 pintada pelo modelo, que não
respeita a grade: close-ups por cima do corpo, painel faltando, pés fora do
corte. Agora cada vista é uma imagem própria, conferida aqui (pessoa
presente, não vazia, pés e cabeça dentro do quadro), e as duas folhas são
montadas com Pillow numa grade que não muda.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps


class TurnaroundExtractionError(RuntimeError):
    """Imagem ou clipe ilegível para montar as folhas ou o giro."""


# Ordem das vistas em `view_image_ids`, na folha 2×2 e no giro de reserva.
VIEW_KINDS = ("front", "profile", "back", "face")
FULL_BODY_KINDS = ("master", "front", "profile", "back")
# Célula das folhas e quadro do Wan/giro: 448×768. Mestre e vistas de corpo
# inteiro são pintados em 672×1152 (a mesma proporção), então caber na
# célula é só reduzir: sem corte, sem faixa de fundo emendada.
CELL = (448, 768)
CYCLE_SIZE = CELL
FULL_BODY_SIZE = (672, 1152)
FACE_SIZE = (768, 1024)


def reference_photo_png(photo: bytes, *, min_side: int = 512) -> bytes:
    """Foto enviada como PNG RGB, com o lado menor em pelo menos `min_side`.

    O grafo leva cada referência a uma contagem fixa de pixels com
    `nearest-exact`. Uma foto pequena (miniatura de 150 px) chegava ao modelo
    em blocos; aqui ela sobe com Lanczos antes do upload. Foto ilegível
    devolve `b""`.
    """
    try:
        with Image.open(BytesIO(photo)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except Exception:
        return b""
    short = min(image.size)
    if 0 < short < min_side:
        scale = min_side / short
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _image(png: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(png)) as source:
            return ImageOps.exif_transpose(source).convert("RGB")
    except Exception as exc:
        raise TurnaroundExtractionError("could not read the image") from exc


def _save(image: Image.Image, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")


# ------------------------------------------------------------ figura
def figure_mask(rgb: np.ndarray) -> np.ndarray:
    """Pixels de figura num fundo de estúdio liso.

    O fundo de cada linha vem das bordas laterais, o que aguenta o degradê
    do chão. Linha cuja borda salta em relação à linha de cima (figura
    encostando na lateral) herda o fundo da última linha boa.
    """
    blurred = cv2.GaussianBlur(np.ascontiguousarray(rgb, dtype=np.uint8), (5, 5), 0)
    arr = blurred.astype(np.int16)
    height, width = arr.shape[:2]
    edge = max(2, width // 25)
    sides = np.concatenate([arr[:, :edge], arr[:, width - edge:]], axis=1)
    row_bg = np.median(sides, axis=1)
    current = np.median(arr[: max(2, height // 20), :edge].reshape(-1, 3), axis=0)
    for y in range(height):
        if np.abs(row_bg[y] - current).max() <= 25:
            current = row_bg[y]
        else:
            row_bg[y] = current
    diff = np.abs(arr - row_bg[:, None, :]).max(axis=2)
    mask = (diff > 30).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return mask.astype(bool)


def _components(mask: np.ndarray, *, min_fraction: float = 0.002) -> list[dict]:
    """Componentes conexos, do maior para o menor, sem poeira."""
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8)
    floor = mask.size * min_fraction
    found = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if area >= floor:
            found.append({"x": x, "y": y, "w": w, "h": h, "area": area})
    found.sort(key=lambda item: item["area"], reverse=True)
    return found


_FACE_MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
_FRONTAL_EYES = 0.3   # distância entre olhos / largura do rosto: acima disso, de frente
_detectors: dict[tuple[int, int], Any] = {}


def face_pose(rgb: np.ndarray) -> tuple[bool, float, float, float, float] | None:
    """(tem rosto, olhos/largura, desvio do nariz, altura do rosto/altura da
    imagem, espaço abaixo do rosto em alturas de rosto) do rosto mais confiável.

    O desvio é (nariz − meio dos olhos) / largura do rosto: negativo com o
    rosto virado para a esquerda da imagem, positivo para a direita. YuNet,
    o mesmo modelo de `face_regions`. Sem modelo ou sem `FaceDetectorYN` no
    OpenCV devolve None e as checagens de rosto saem.
    """
    if not _FACE_MODEL.is_file() or not hasattr(cv2, "FaceDetectorYN"):
        return None
    height, width = rgb.shape[:2]
    try:
        detector = _detectors.get((width, height))
        if detector is None:
            detector = cv2.FaceDetectorYN.create(
                str(_FACE_MODEL), "", (width, height), score_threshold=0.6)
            _detectors[(width, height)] = detector
        _, faces = detector.detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    except cv2.error:
        return None
    if faces is None or not len(faces):
        return False, 0.0, 0.0, 0.0, 0.0
    best = max(faces, key=lambda row: float(row[-1]))
    face_width = max(1.0, float(best[2]))
    eyes = abs(float(best[6]) - float(best[4])) / face_width
    yaw = (float(best[8]) - (float(best[4]) + float(best[6])) / 2) / face_width
    face_height = max(1.0, float(best[3]))
    below = (height - (float(best[1]) + face_height)) / face_height
    return True, eyes, yaw, face_height / max(1, height), below


# Medido nas pinturas reais da L4. Espaço abaixo do rosto, em alturas de
# rosto: close-up de cabeça e ombros 0,6–2,4 (o do Caio, de chapéu, 2,4);
# meio corpo 3,1–6,9. Altura do rosto num corpo inteiro: 0,09–0,11.
_CLOSEUP_BELOW = 2.8
_FULL_BODY_FACE = 0.16


def face_info(rgb: np.ndarray, *,
              max_yaw: float = 0.12) -> tuple[str, float, float] | None:
    """(`"frontal"`, `"turned"` ou `""` sem rosto; altura relativa do rosto;
    espaço abaixo dele em alturas de rosto)."""
    pose = face_pose(rgb)
    if pose is None:
        return None
    found, eyes, yaw, size, below = pose
    if not found:
        return "", 0.0, 0.0
    kind = "frontal" if eyes > _FRONTAL_EYES and abs(yaw) <= max_yaw else "turned"
    return kind, size, below


def check_view(png: bytes, kind: str) -> list[str]:
    """Problemas baratos de uma vista; lista vazia é vista aceita.

    `empty`: quase nada além do fundo. `extra_figure`: outra figura grande
    (gente duplicada, grade de painéis). Corpo inteiro: `feet_cut`,
    `head_cut`, `side_cut`, `too_small` e `not_standing`. Close-up:
    `head_cut`, `too_small` e `not_closeup` (corpo inteiro no lugar do rosto).
    Rosto (YuNet): frente, mestre e close-up precisam de rosto de frente
    (`no_face`); costas não podem mostrar rosto (`face_visible`); perfil não
    pode estar de frente (`not_profile`).
    """
    try:
        image = _image(png)
    except TurnaroundExtractionError:
        return ["unreadable"]
    rgb = np.asarray(image)
    height, width = rgb.shape[:2]
    figures = _components(figure_mask(rgb))
    if not figures or figures[0]["area"] < 0.02 * height * width:
        return ["empty"]
    main = figures[0]
    problems: list[str] = []
    if any(other["area"] >= 0.15 * main["area"] for other in figures[1:]):
        problems.append("extra_figure")
    top_margin = max(1, height // 200)
    if main["y"] <= top_margin:
        problems.append("head_cut")
    if kind in FULL_BODY_KINDS:
        if main["y"] + main["h"] >= height - max(2, height // 100):
            problems.append("feet_cut")
        if main["x"] <= 1 or main["x"] + main["w"] >= width - 1:
            problems.append("side_cut")
        if main["h"] < 0.55 * height:
            problems.append("too_small")
        if main["h"] < 1.6 * main["w"]:
            problems.append("not_standing")
    else:
        if main["area"] < 0.12 * height * width:
            problems.append("too_small")
        if main["w"] < 0.3 * width or main["h"] > 2.2 * main["w"]:
            problems.append("not_closeup")
    info = face_info(rgb)
    if info is not None:
        face, size, below = info
        if kind == "face" and face and below > _CLOSEUP_BELOW and "not_closeup" not in problems:
            problems.append("not_closeup")
        if kind in FULL_BODY_KINDS and size > _FULL_BODY_FACE:
            problems.append("not_full_body")
        if kind in ("master", "front", "face") and face != "frontal":
            problems.append("no_face")
        elif kind == "back" and face:
            problems.append("face_visible")
        elif kind == "profile" and face == "frontal":
            problems.append("not_profile")
    return problems


# ------------------------------------------------------------ folhas
def _cell(png: bytes, size: tuple[int, int] = CELL) -> Image.Image:
    """Vista na célula por recorte central da proporção (sem faixa emendada).

    Corpo inteiro já vem na proporção da célula; o close-up 3:4 perde só as
    pontas dos ombros.
    """
    return ImageOps.fit(_image(png), size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def compose_reference_sheet(view_pngs: list[bytes], path: str | Path) -> tuple[int, int]:
    """Folha 2×2 fixa: frente, perfil / costas, close-up. Devolve a célula."""
    if len(view_pngs) != 4:
        raise ValueError("the reference sheet needs exactly four views")
    width, height = CELL
    grid = Image.new("RGB", (width * 2, height * 2))
    for index, png in enumerate(view_pngs):
        grid.paste(_cell(png), ((index % 2) * width, (index // 2) * height))
    _save(grid, path)
    return CELL


def compose_design_sheet(master_png: bytes, view_pngs: list[bytes],
                         path: str | Path) -> tuple[int, int]:
    """Ficha 1×4 fixa: mestre, perfil, costas, close-up. Devolve o tamanho."""
    if len(view_pngs) != 4:
        raise ValueError("the design sheet needs the four views")
    width, height = CELL
    panels = [master_png, view_pngs[1], view_pngs[2], view_pngs[3]]
    sheet = Image.new("RGB", (width * len(panels), height))
    for index, png in enumerate(panels):
        sheet.paste(_cell(png), (index * width, 0))
    _save(sheet, path)
    return sheet.size


def build_master_frame(master_png: bytes, output_path: str | Path, *,
                       size: tuple[int, int] = CYCLE_SIZE) -> tuple[int, int]:
    """Primeiro (e último) quadro do Wan: o mestre inteiro, reduzido.

    Antes o mestre entrava encolhido num canvas cinza fixo e o fundo dele,
    mais claro, desenhava um retângulo que o vídeo carregava nos primeiros
    frames. Agora é recorte central da proporção, sem moldura.
    """
    if not master_png:
        raise TurnaroundExtractionError("frame requires the master PNG")
    _save(_cell(master_png, size), output_path)
    return size


# ------------------------------------------------------------ giro de reserva
def _encode(frames, output_path: str | Path, size: tuple[int, int], fps: float) -> int:
    """Frames RGB (PIL ou ndarray) em MP4 H.264. Devolve quantos foram."""
    width, height = size
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    count = 0
    # Sem communicate(): no Python 3.12 ele dá flush no stdin já fechado e
    # levanta ValueError. stderr vai para um arquivo, sem risco de pipe cheio.
    with tempfile.TemporaryFile() as error_log:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=error_log)
        except OSError as exc:
            raise TurnaroundExtractionError("ffmpeg is not available") from exc
        try:
            for frame in frames:
                data = frame.tobytes() if isinstance(frame, Image.Image) else \
                    np.ascontiguousarray(frame, dtype=np.uint8).tobytes()
                process.stdin.write(data)
                count += 1
            process.stdin.close()
            returncode = process.wait(timeout=120)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            process.kill()
            process.wait()
            raise TurnaroundExtractionError("ffmpeg failed to encode the clip") from exc
        if returncode != 0:
            error_log.seek(0)
            raise TurnaroundExtractionError(
                f"ffmpeg failed: {error_log.read(200).decode(errors='replace')}")
    return count


def build_view_cycle(view_pngs: list[bytes], output_path: str | Path, *,
                     size: tuple[int, int] = CYCLE_SIZE, fps: int = 24,
                     hold_s: float = 0.6, fade_s: float = 0.4) -> int:
    """MP4 em loop: frente, perfil, costas, perfil espelhado, com fusão.

    Sai das vistas pintadas (frente, perfil, costas), sem modelo de vídeo: é
    o giro imediato e o que fica quando o clipe do Wan falha ou é recusado.
    Devolve a duração em ms.
    """
    if len(view_pngs) < 3:
        raise TurnaroundExtractionError("the view cycle needs front, profile and back")
    front, profile, back = (_cell(png, size) for png in view_pngs[:3])
    keys = [front, profile, back, ImageOps.mirror(profile)]
    hold = max(1, round(hold_s * fps))
    fade = max(1, round(fade_s * fps))

    def frames():
        for index, key in enumerate(keys):
            following = keys[(index + 1) % len(keys)]
            for _ in range(hold):
                yield key
            for step in range(1, fade + 1):
                yield Image.blend(key, following, step / (fade + 1))

    count = _encode(frames(), output_path, size, fps)
    return int(round(count * 1000 / fps))


# ------------------------------------------------------------ clipe do Wan
def _read_frames(video_path: str | Path) -> tuple[list[np.ndarray], float]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        return [], 0.0
    frames: list[np.ndarray] = []
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        capture.release()
    return frames, fps


def _descriptor(rgb: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray | None:
    """Figura recortada, 16×40, em Lab: compara pose e cores, não tamanho."""
    if mask is None:
        mask = figure_mask(rgb)
    figures = _components(mask)
    if not figures:
        return None
    main = figures[0]
    crop = rgb[main["y"]:main["y"] + main["h"], main["x"]:main["x"] + main["w"]]
    small = cv2.resize(crop, (16, 40), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_RGB2LAB).astype(np.float32) / 255.0


# Calibrado em 20 clipes reais da L4 (11/11/2026). Bons: Wan livres que
# voltam à frente, Wan com primeiro/último quadro fixos (Renata e Caio, com o
# mestre ocupando a altura do quadro). Ruins: fundo em painéis (s1234),
# arcos e barbatanas (lock2/wan2), quadros quebrados em faixas (101), manchas
# de 12 passos, texto no chão (81/20), fantasmas do LTXV guiado, o meio giro
# do LTXV, giros desfeitos (s7) e vai-e-volta (lock2/wan0, flf99).
_ANALYSIS_SCALE = 4
_TAIL_FRAMES = 4         # o último latente fixo sai escurecido no decode
_SKIP_START = 8          # o quadro inicial ainda assenta
_BACK_RUN = 8            # costas: ao menos 8 quadros seguidos sem rosto
_FRONT_RUN = 3           # volta: 3 quadros seguidos de rosto de frente
_BORDER_DELTA = 25       # bons ≤ 0,8 % das bordas mudadas; ruins ≥ 2,6 %
_BORDER_FRACTION = 0.015
_FLOOR_DELTA = 60
_FLOOR_FRACTION = 0.02
_ZONE_WIDTH = 1.6        # faixa da figura (× meia largura mediana)
_OUTSIDE_FRACTION = 0.008  # forma solta fora da faixa: bons ≤ 0,1 %; ruins ≥ 1,5 %
_ATTACHED_FRACTION = 0.03  # parte da própria figura fora da faixa (aba, casaco)
_JUMP_RATIO = 2.0        # área da figura entre quadros: bons ≤ 1,63×; ruins ≥ 3,4×
_SHARPNESS_RATIO = 0.4   # nitidez mediana da figura / início: bons ≥ 0,61; fantasma 0,23


_LOOP_YAW = 0.06         # de frente no clipe: nariz no meio dos olhos
_BLIP_FRAMES = 6         # rosto de lado por menos que isso, entre costas, é ruído


def _faces_timeline(frames: list[np.ndarray]) -> list[str] | None:
    """Por quadro: F (de frente), < ou > (virado para esse lado), . (sem rosto)."""
    timeline = []
    for frame in frames:
        pose = face_pose(frame)
        if pose is None:
            return None
        found, eyes, yaw, _size, _below = pose
        if not found:
            timeline.append(".")
        elif eyes > _FRONTAL_EYES and abs(yaw) <= _LOOP_YAW:
            timeline.append("F")
        else:
            timeline.append("<" if yaw < 0 else ">")
    return timeline


def _side(marks: str) -> int:
    left, right = marks.count("<"), marks.count(">")
    return 0 if left == right else (-1 if left > right else 1)


def _loop_from_faces(timeline: list[str], to_front: list[float],
                     usable: int) -> tuple[int | None, list[str]]:
    """(loop_end, problemas) pela linha do tempo do rosto.

    Costas = a primeira sequência sem rosto com pelo menos metade da maior
    (e `_BACK_RUN` quadros). Máscara e caveira (o Caio) perdem o rosto por uns
    quadros já no perfil; esse buraco curto não é as costas. Volta completa =
    o rosto some virado para um lado e reaparece vindo do outro; o mesmo lado
    na ida e na volta é meia-volta desfeita (o Wan s7 fazia frente → perfil →
    costas → o mesmo perfil → frente). O primeiro trecho de rosto de frente
    depois das costas fecha o loop no quadro mais parecido com o 0; uma
    segunda volta, que o Wan às vezes dá para cair no último quadro fixo,
    fica de fora. Outra sequência longa sem rosto antes da volta é vai-e-volta.
    """
    text = "".join(timeline[:usable])
    # Rosto de lado por poucos quadros no meio das costas (o YuNet às vezes
    # acha um rosto no cabelo) não separa duas costas.
    text = re.sub(r"(?<=\.)[<>]{1,%d}(?=\.)" % (_BLIP_FRAMES - 1),
                  lambda m: "." * len(m.group(0)), text)
    runs = [match.span() for match in re.finditer(r"\.+", text)]
    longest = max((end - start for start, end in runs), default=0)
    if longest < _BACK_RUN:
        return None, ["no_back_view"]
    need = max(_BACK_RUN, longest // 2)
    back_start, back_end = next((start, end) for start, end in runs if end - start >= need)
    out_side = _side(text[:back_start])
    match = re.search(r"F{%d,}" % _FRONT_RUN, text[back_end:])
    if not match:
        return None, []
    index = back_end + match.start()
    # O rosto volta de frente antes do corpo: o loop fecha no quadro mais
    # parecido com o 0 entre todos os de frente até a próxima costas (ou o
    # fim), não só no primeiro trecho F (staging: emendava num 3/4).
    following = re.search(r"\.{%d,}" % _BACK_RUN, text[index:])
    stop = index + following.start() if following else len(text)
    # lado de chegada: o trecho virado logo antes do F (furos de 1 quadro ok)
    arrival = re.search(r"[<>][<>.]*$", text[back_end:index].rstrip("."))
    in_side = _side(arrival.group(0)) if arrival else 0
    if out_side and in_side and in_side == out_side:
        return None, ["half_turn"]
    again = max(_BACK_RUN, int(0.4 * (back_end - back_start)))
    if any(end - start >= again for start, end in runs if back_end <= start < index):
        return None, ["wobble"]
    best = min((j for j in range(index, stop) if text[j] == "F"), key=lambda j: to_front[j])
    return best, []


def analyze_clip(video_path: str | Path) -> dict[str, Any]:
    """Volta completa, ponto de loop e defeitos do giro 360°.

    Volta completa = o rosto some por uma sequência de quadros (costas) e
    volta de frente. `loop_end` é o quadro, nesse retorno, mais parecido com
    o 0: o clipe cortado em [0, loop_end) emenda sem salto. Personagem sem
    rosto detectável no quadro 0 (máscara, criatura) cai na distância da
    figura ao quadro 0. Os defeitos só contam no trecho que fica.
    """
    frames, fps = _read_frames(video_path)
    report: dict[str, Any] = {"frames": len(frames), "fps": fps, "problems": []}
    problems: list[str] = report["problems"]
    if len(frames) < 16:
        problems.append("unreadable")
        report["clean"] = False
        return report
    height, width = frames[0].shape[:2]
    scale = _ANALYSIS_SCALE
    small = [cv2.resize(frame, (width // scale, height // scale),
                        interpolation=cv2.INTER_AREA) for frame in frames]
    masks = [figure_mask(frame) for frame in small]
    descriptors = [_descriptor(frame, mask) for frame, mask in zip(small, masks)]
    if descriptors[0] is None:
        problems.append("no_figure")
        report["clean"] = False
        return report

    def distance(a, b):
        return float(np.abs(a - b).mean()) if a is not None and b is not None else 1.0

    to_front = [distance(item, descriptors[0]) for item in descriptors]
    usable = len(frames) - _TAIL_FRAMES
    timeline = _faces_timeline(frames)
    report["faces"] = "".join(timeline) if timeline else None
    # Começo com rosto (de frente ou de lado: a caveira do Caio oscila entre
    # F e > já no quadro 0) usa a linha do tempo do rosto, que pega a meia
    # volta desfeita no fim; o caminho da figura não pegava.
    if timeline and sum(mark != "." for mark in timeline[:6]) >= 4:
        loop_end, turn_problems = _loop_from_faces(timeline, to_front, usable)
        problems.extend(turn_problems)
    else:
        peak_at = int(np.argmax(to_front[:usable]))
        peak = to_front[peak_at]
        loop_end = None
        for index in range(peak_at + 1, usable):
            if to_front[index] <= 0.3 * peak:
                loop_end = min(range(index, usable), key=lambda j: to_front[j])
                break
        if peak < 0.03:
            problems.append("no_turn")
    if loop_end is None or loop_end < 24:
        problems.append("no_return")
        loop_end = usable
    report["loop_end"] = loop_end

    kept = small[:loop_end]
    kept_masks = masks[:loop_end]
    h, w = kept[0].shape[:2]
    side, top, bottom = max(2, w // 10), max(2, h // 20), max(2, h // 25)
    boxes = []
    for mask in kept_masks:
        figures = _components(mask)
        boxes.append(figures[0] if figures else None)
    present = [box for box in boxes if box]
    centre = float(np.median([box["x"] + box["w"] / 2 for box in present])) if present else w / 2
    # Mediana, não máximo: mancha grudada na figura alarga a caixa e não pode
    # alargar junto a faixa que a esconde.
    half = float(np.median([box["w"] / 2 for box in present])) if present else w / 4
    top_line = min((box["y"] for box in present), default=0) - max(2, h // 33)
    zone = np.zeros((h, w), bool)
    zone[max(0, top_line):, int(max(0, centre - _ZONE_WIDTH * half)):
         int(min(w, centre + _ZONE_WIDTH * half + 1))] = True
    # Bordas (laterais e topo) e chão, sempre fora da faixa da figura: com o
    # mestre ocupando a altura do quadro, cabelo e pés entram nessas faixas
    # quando ela gira, e isso não é defeito.
    edges = np.zeros((h, w), bool)
    edges[:, :side] = edges[:, w - side:] = edges[:top] = True
    edges &= ~zone
    floor_band = np.zeros((h, w), bool)
    floor_band[h - bottom:] = True
    floor_band &= ~zone
    blurred = np.stack([cv2.GaussianBlur(frame, (5, 5), 0).astype(np.int16) for frame in kept])
    reference = np.median(blurred, axis=0)
    worst_border = worst_floor = worst_outside = 0.0
    worst_jump = 1.0
    previous_area = 0
    inner = np.zeros((h, w), bool)
    inner[:, int(w * 0.08):w - int(w * 0.08)] = True
    for index, (frame, mask, box) in enumerate(zip(blurred, kept_masks, boxes)):
        area = box["area"] if box else 0
        if previous_area and area:
            worst_jump = max(worst_jump, area / previous_area, previous_area / area)
        previous_area = area or previous_area
        if index < _SKIP_START:
            continue
        delta = frame - reference
        if edges.any():
            delta = delta - np.median(delta[edges], axis=0)   # cintilação global de luz
        change = np.abs(delta).max(axis=2)
        if edges.any():
            worst_border = max(worst_border, float((change[edges] > _BORDER_DELTA).mean()))
        if floor_band.any():
            worst_floor = max(worst_floor, float((change[floor_band] > _FLOOR_DELTA).mean()))
        # Forma solta fora da faixa (texto, painel, barbatana separada) conta
        # cheia; o que sai da própria figura (aba do chapéu, casaco abrindo no
        # giro do Caio) só conta acima de `_ATTACHED_FRACTION`.
        labels_count, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
        # As colunas da beira ficam com a métrica de borda (mediana no tempo):
        # a vinheta do estúdio vira "figura" ali na máscara de um quadro só.
        outside = mask & ~zone & inner
        if box and labels_count > 1:
            main_label = int(np.bincount(labels[mask].ravel()).argmax()) if mask.any() else 0
            attached = outside & (labels == main_label)
            detached = outside & (labels != main_label)
        else:
            attached, detached = np.zeros_like(outside), outside
        worst_outside = max(worst_outside, float(detached.mean()),
                            float(attached.mean()) * _OUTSIDE_FRACTION / _ATTACHED_FRACTION)
    # Fantasma e borrão: a figura perde o detalhe (variância do Laplaciano)
    # em relação aos primeiros quadros, que são o mestre nítido.
    sharpness = []
    for frame, box in zip(frames[:loop_end], boxes):
        if not box:
            sharpness.append(0.0)
            continue
        x, y = box["x"] * scale, box["y"] * scale
        crop = cv2.cvtColor(frame[y:y + box["h"] * scale, x:x + box["w"] * scale],
                            cv2.COLOR_RGB2GRAY)
        sharpness.append(float(cv2.Laplacian(crop, cv2.CV_64F).var()) if crop.size else 0.0)
    start_sharpness = float(np.median(sharpness[:_SKIP_START])) or 1.0
    sharp_ratio = float(np.median(sharpness)) / start_sharpness
    report["sharpness"] = round(sharp_ratio, 2)
    if sharp_ratio < _SHARPNESS_RATIO:
        problems.append("smeared")
    report.update({
        "border": round(worst_border, 4), "floor": round(worst_floor, 4),
        "outside": round(worst_outside, 4), "jump": round(worst_jump, 2),
    })
    if worst_border > _BORDER_FRACTION:
        problems.append("background_changes")
    if worst_floor > _FLOOR_FRACTION:
        problems.append("floor_changes")
    if worst_outside > _OUTSIDE_FRACTION:
        problems.append("stray_shapes")
    if worst_jump > _JUMP_RATIO:
        problems.append("figure_jumps")
    report["clean"] = not problems
    return report


def finalize_loop(source_path: str | Path, output_path: str | Path, end: int) -> int:
    """Regrava o clipe com os quadros [0, end): o último emenda no primeiro.

    Devolve a duração em ms.
    """
    frames, fps = _read_frames(source_path)
    if not frames or end < 2:
        raise TurnaroundExtractionError("clip has no frames to keep")
    kept = frames[:end]
    height, width = kept[0].shape[:2]
    fps = fps or 24.0
    count = _encode(kept, output_path, (width, height), fps)
    return int(round(count * 1000 / fps))
