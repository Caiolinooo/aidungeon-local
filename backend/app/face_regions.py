"""Detecção e associação de rostos para refinamento localizado de cenas."""

from __future__ import annotations

import threading
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


def count_faces(content: bytes, expected: int) -> tuple[int, int] | None:
    """(rostos do tamanho do elenco, todos os rostos); None sem detector.

    Do tamanho do elenco = pelo menos 45% da altura mediana dos `expected`
    maiores: uma quarta pessoa ao lado do elenco conta, um pedestre minúsculo
    no fundo de uma feira só entra no total.
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
        # Rosto de figurante ao longe tem uns 15 px e escapa do detector; na
        # imagem ampliada 2x ele aparece. O total é o maior dos dois.
        large = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        zoomed = cv2.FaceDetectorYN.create(
            str(_MODEL), "", (width * 2, height * 2),
            score_threshold=0.6, nms_threshold=0.3, top_k=5000,
        )
        _, detected_large = zoomed.detect(large)
    except Exception:
        return None
    faces = _deduplicate(list(detected) if detected is not None else [])
    distant = len(_deduplicate(list(detected_large) if detected_large is not None else []))
    heights = sorted((float(face[3]) for face in faces), reverse=True)
    total = max(len(heights), distant)
    if expected <= 0 or len(heights) <= expected:
        return len(heights), total
    cast = heights[:expected]
    reference = cast[len(cast) // 2]
    return sum(1 for value in heights if value >= 0.45 * reference), total


# Reconhecedor SFace do OpenCV Zoo (Apache-2.0, 37 MB). Não vai no git: fica em
# `<pasta do banco>/models/` ou no caminho de `AIDUNGEON_FACE_MODEL`. Sem o
# arquivo a conferência de identidade é pulada.
_RECOGNIZER = "face_recognition_sface_2021dec.onnx"
# Cosseno a partir do qual o SFace trata dois rostos como a mesma pessoa.
_SAME_PERSON = 0.363
_reference_features: dict[str, object] = {}


def recognizer_path() -> Path | None:
    import os

    from app import db
    candidates = [
        os.environ.get("AIDUNGEON_FACE_MODEL") or "",
        str(Path(db.db_path()).parent / "models" / _RECOGNIZER),
        str(Path(__file__).with_name("models") / _RECOGNIZER),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _features(recognizer, image, *, largest: bool = False) -> list:
    import cv2

    height, width = image.shape[:2]
    detector = cv2.FaceDetectorYN.create(
        str(_MODEL), "", (width, height),
        score_threshold=0.6, nms_threshold=0.3, top_k=5000,
    )
    _, detected = detector.detect(image)
    faces = sorted(_deduplicate(list(detected) if detected is not None else []),
                   key=lambda face: -float(face[3]))
    if largest:
        faces = faces[:1]
    return [recognizer.feature(recognizer.alignCrop(image, face)) for face in faces]


_recognizer_lock = threading.Lock()
_recognizer: tuple[str, object] | None = None


def _recognizer_for(path: Path):
    """O reconhecedor carrega 37 MB: um por processo, não um por pintura."""
    global _recognizer
    import cv2

    if _recognizer is None or _recognizer[0] != str(path):
        _recognizer = (str(path), cv2.FaceRecognizerSF.create(str(path), ""))
    return _recognizer[1]


def repeated_person(content: bytes, references: list[bytes]) -> bool | None:
    """Uma das referências aparece em dois rostos da pintura.

    Com quatro referências o Flux2 às vezes pinta a mesma pessoa duas vezes e
    deixa outra de fora: a contagem de rostos fecha e o elenco está errado.
    None sem o reconhecedor, sem rostos ou em erro: quem chama segue como antes.

    Também None quando duas referências se parecem entre si: são duas imagens
    da mesma pessoa (elenco de um ou dois leva mestre e ficha) ou dois
    personagens parecidos, e aí "o mesmo rosto duas vezes" é o esperado. Sem
    isso um par de gêmeos custaria duas pinturas a mais em toda cena.
    """
    path = recognizer_path()
    if path is None or not _MODEL.exists() or len(references) < 2:
        return None
    try:
        import hashlib

        import cv2
        import numpy as np

        with _recognizer_lock:        # o objeto do OpenCV não é seguro entre threads
            recognizer = _recognizer_for(path)
            image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                return None
            painted = _features(recognizer, image)
            if len(painted) < 2:
                return None
            known = []
            for blob in references:
                key = hashlib.sha256(blob).hexdigest()
                if key not in _reference_features:
                    if len(_reference_features) > 64:
                        _reference_features.clear()
                    ref = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
                    found = _features(recognizer, ref, largest=True) if ref is not None else []
                    _reference_features[key] = found[0] if found else None
                feature = _reference_features[key]
                if feature is not None:      # criatura ou referência sem rosto legível fica fora
                    known.append(feature)

            def same(left, right) -> bool:
                return recognizer.match(left, right, cv2.FaceRecognizerSF_FR_COSINE) >= _SAME_PERSON

            if any(same(known[i], known[j])
                   for i in range(len(known)) for j in range(i + 1, len(known))):
                return None
            return any(sum(1 for face in painted if same(feature, face)) >= 2
                       for feature in known)
    except Exception:
        return None


def count_main_faces(content: bytes, expected: int) -> int | None:
    found = count_faces(content, expected)
    return None if found is None else found[0]


def scene_shift(before: bytes, after: bytes) -> float | None:
    """Quanto a imagem mudou entre duas versões, de 0 a 1 (None sem leitura).

    As duas reduzidas a 96x54 e comparadas pixel a pixel. Tirar figurantes do
    fundo mexe em 5 a 7% (medido em 4 edições boas); outra semente do mesmo
    lugar dá 19% e um lugar diferente, 30%.
    """
    try:
        import cv2
        import numpy as np

        small = []
        for content in (before, after):
            image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                return None
            small.append(cv2.resize(image, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16))
        return float(np.abs(small[0] - small[1]).mean()) / 255
    except Exception:
        return None

