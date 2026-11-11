"""Split determinístico e extração das quatro vistas de um clipe 360°."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import cv2
from PIL import Image, ImageOps


class TurnaroundExtractionError(RuntimeError):
    """O clipe não contém quatro frames legíveis para a folha de referência."""


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


def build_split_frame(anchor_png: bytes, master_png: bytes,
                      output_path: str | Path, *, size: tuple[int, int] = (1344, 768)
                      ) -> tuple[int, int]:
    """Corpo do mestre à esquerda e rosto canônico à direita, sem outro modelo.

    A âncora conserva os detalhes faciais originais; o mestre fornece corpo e
    roupa. `contain` não corta cabeça, mãos ou calçados nem inventa pixels.
    """
    if not anchor_png or not master_png:
        raise TurnaroundExtractionError("split requires anchor and master PNGs")
    width, height = size
    if width < 3 or height < 1:
        raise ValueError("split frame is too small")
    with Image.open(BytesIO(anchor_png)) as source:
        anchor = ImageOps.exif_transpose(source).convert("RGB")
    with Image.open(BytesIO(master_png)) as source:
        master = ImageOps.exif_transpose(source).convert("RGB")
    canvas = Image.new("RGB", size, (224, 224, 224))
    left_width = width // 3
    margin = max(2, width // 336)
    body = ImageOps.contain(
        master, (max(1, left_width - 2 * margin), max(1, height - 2 * margin)),
        Image.Resampling.LANCZOS,
    )
    face = ImageOps.contain(
        anchor, (max(1, width - left_width - 2 * margin), max(1, height - 2 * margin)),
        Image.Resampling.LANCZOS,
    )
    canvas.paste(body, ((left_width - body.width) // 2, (height - body.height) // 2))
    right_x = left_width + margin
    right_width = width - left_width - margin
    canvas.paste(face, (
        right_x + (right_width - face.width) // 2,
        (height - face.height) // 2,
    ))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="PNG")
    return size


def build_master_frame(master_png: bytes, output_path: str | Path, *,
                       size: tuple[int, int] = (448, 768)) -> tuple[int, int]:
    """Só o mestre de corpo inteiro, em pé, num quadro vertical.

    O quadro dividido (mestre + close-up grande) não girava no LTXV 2B: a
    figura da esquerda ficava parada e o close-up encolhia e ganhava pernas.
    Com uma figura só, o vídeo mantém a pessoa e o corpo vira de verdade.
    """
    if not master_png:
        raise TurnaroundExtractionError("frame requires the master PNG")
    with Image.open(BytesIO(master_png)) as source:
        master = ImageOps.exif_transpose(source).convert("RGB")
    width, height = size
    canvas = Image.new("RGB", size, (224, 224, 224))
    margin = max(2, height // 64)
    body = ImageOps.contain(
        master, (max(1, width - 2 * margin), max(1, height - 2 * margin)),
        Image.Resampling.LANCZOS)
    canvas.paste(body, ((width - body.width) // 2, (height - body.height) // 2))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="PNG")
    return size


# Células da ficha (3 colunas × 2 linhas): de cima, frente, perfil direito e
# costas de corpo inteiro; embaixo, close-ups de frente, três-quartos e perfil.
_SHEET_VIEW_CELLS = ((0, 0), (1, 0), (2, 0), (0, 1))


def extract_sheet_views(sheet_png: bytes, view_paths: list[str | Path],
                        sheet_path: str | Path) -> tuple[int, int]:
    """Vistas frente/perfil/costas/rosto recortadas da ficha e uma folha 2×2.

    As vistas saíam de quadros do vídeo, que não gira: eram quatro frentes e a
    última com defeito. A ficha já traz os ângulos certos da mesma pessoa.
    """
    if len(view_paths) != 4:
        raise ValueError("turnaround requires exactly four view paths")
    try:
        with Image.open(BytesIO(sheet_png)) as source:
            sheet = source.convert("RGB")
    except Exception as exc:
        raise TurnaroundExtractionError("could not read the design sheet") from exc
    cell_w, cell_h = sheet.width // 3, sheet.height // 2
    if cell_w < 1 or cell_h < 1:
        raise TurnaroundExtractionError("design sheet is too small")
    for (column, row), output in zip(_SHEET_VIEW_CELLS, view_paths):
        view = sheet.crop((column * cell_w, row * cell_h,
                           (column + 1) * cell_w, (row + 1) * cell_h))
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        view.save(destination, format="PNG")
    grid = Image.new("RGB", (cell_w * 2, cell_h * 2))
    for index, output in enumerate(view_paths):
        with Image.open(output) as view:
            grid.paste(view.convert("RGB"), ((index % 2) * cell_w, (index // 2) * cell_h))
    destination = Path(sheet_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    grid.save(destination, format="PNG")
    return cell_w, cell_h


def extract_turnaround(video_path: str | Path, view_paths: list[str | Path],
                       sheet_path: str | Path) -> tuple[int, int]:
    """Grava vistas 0/90/180/270 e uma folha RGB 2×2, sem rótulos.

    Os ângulos correspondem aos quartos do clipe. O chamador escolhe nomes
    únicos e remove os arquivos se a publicação no banco não concluir.
    """
    if len(view_paths) != 4:
        raise ValueError("turnaround requires exactly four view paths")
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise TurnaroundExtractionError("could not open turnaround clip")
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count < 4:
            raise TurnaroundExtractionError("turnaround clip has fewer than four frames")
        size: tuple[int, int] | None = None
        for index, output in enumerate(view_paths):
            frame_index = min(index * frame_count // 4, frame_count - 1)
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                raise TurnaroundExtractionError(
                    f"could not read turnaround frame {frame_index}")
            height, width = frame.shape[:2]
            if size is None:
                size = (width, height)
            elif size != (width, height):
                raise TurnaroundExtractionError("turnaround frames have inconsistent sizes")
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            destination = Path(output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="PNG")
    finally:
        capture.release()

    assert size is not None
    sheet = Image.new("RGB", (size[0] * 2, size[1] * 2))
    for index, output in enumerate(view_paths):
        with Image.open(output) as view:
            sheet.paste(view.convert("RGB"), ((index % 2) * size[0],
                                               (index // 2) * size[1]))
    destination = Path(sheet_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG")
    return size
