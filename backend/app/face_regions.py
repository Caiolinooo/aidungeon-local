"""Detecção e associação de rostos para refinamento localizado de cenas."""

from __future__ import annotations

from itertools import permutations
from pathlib import Path

_MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"


class FaceDetectionError(Exception):
    pass


def _expected_center(index: int, count: int) -> tuple[float, float]:
    if count <= 2:
        return (index + 0.5) / count, 0.35
    rows = (count + 1) // 2
    col, row = index % 2, index // 2
    return (col + 0.5) / 2, (row + 0.35) / rows


def _deduplicate(faces: list) -> list:
    kept = []
    for face in sorted(faces, key=lambda item: float(item[-1]), reverse=True):
        x, y, width, height = map(float, face[:4])
        cx, cy = x + width / 2, y + height / 2
        duplicate = False
        for other in kept:
            ox, oy, ow, oh = map(float, other[:4])
            if abs(cx - (ox + ow / 2)) < 0.65 * min(width, ow) and \
                    abs(cy - (oy + oh / 2)) < 0.65 * min(height, oh):
                duplicate = True
                break
        if not duplicate:
            kept.append(face)
    return kept[:8]


def match_faces(content: bytes, *, width: int, height: int,
                count: int, expected_people: int | None = None,
                ) -> list[tuple[float, float, float, float]]:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise FaceDetectionError("a composição gerada não é uma imagem válida")
    actual_height, actual_width = image.shape[:2]
    detector = cv2.FaceDetectorYN.create(
        str(_MODEL), "", (actual_width, actual_height),
        score_threshold=0.6, nms_threshold=0.3, top_k=5000,
    )
    _, detected = detector.detect(image)
    faces = _deduplicate(list(detected) if detected is not None else [])
    if expected_people is not None and len(faces) != expected_people:
        raise FaceDetectionError(
            f"esperava {expected_people} pessoas na composição; detector encontrou {len(faces)}"
        )
    if len(faces) < count:
        raise FaceDetectionError(
            f"esperava {count} rostos para as referências; detector encontrou {len(faces)}"
        )

    candidates = []
    for assignment in permutations(range(len(faces)), count):
        cost = 0.0
        for index, face_index in enumerate(assignment):
            x, y, face_width, face_height = map(float, faces[face_index][:4])
            actual = (
                (x + face_width / 2) / actual_width,
                (y + face_height / 2) / actual_height,
            )
            expected = _expected_center(index, count)
            cost += (actual[0] - expected[0]) ** 2 + (actual[1] - expected[1]) ** 2
        candidates.append((cost, assignment))
    _, best = min(candidates)
    scale_x, scale_y = width / actual_width, height / actual_height
    return [
        (
            float(faces[face_index][0]) * scale_x,
            float(faces[face_index][1]) * scale_y,
            float(faces[face_index][2]) * scale_x,
            float(faces[face_index][3]) * scale_y,
        )
        for face_index in best
    ]


def count_main_faces(content: bytes, expected: int) -> int | None:
    """Quantos rostos do tamanho do elenco a imagem tem; None sem detector.

    Conta só rosto comparável aos `expected` maiores (pelo menos 45% da altura
    mediana deles): uma quarta pessoa ao lado do elenco conta, um pedestre
    minúsculo no fundo de uma feira não.
    """
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
        if image is None or not _MODEL.exists():
            return None
        height, width = image.shape[:2]
        detector = cv2.FaceDetectorYN.create(
            str(_MODEL), "", (width, height),
            score_threshold=0.6, nms_threshold=0.3, top_k=5000,
        )
        _, detected = detector.detect(image)
    except Exception:
        return None
    faces = _deduplicate(list(detected) if detected is not None else [])
    heights = sorted((float(face[3]) for face in faces), reverse=True)
    if expected <= 0 or len(heights) <= expected:
        return len(heights)
    cast = heights[:expected]
    reference = cast[len(cast) // 2]
    return sum(1 for value in heights if value >= 0.45 * reference)
