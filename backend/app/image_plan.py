"""Plano de imagem (contrato C4).

O Grok renderiza `face_refs` no provider quando o ControlNet existir (G6).
Até lá o retrato de pessoa real usa a referência como img2img, e o See
abre o prompt com o nome e o descritor.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FaceRef:
    image_path: str
    weight: float = 0.8
    method: str = "auto"  # "auto" | "ip_adapter_faceid" | "instantid"


@dataclass
class ImagePlan:
    kind: str  # "portrait" | "see" | "backdrop"
    prompt: str
    negative: str = ""
    width: int = 832
    height: int = 1216
    seed: int = -1
    init_image_id: str | None = None
    denoise: float | None = None
    face_refs: list[FaceRef] = field(default_factory=list)
    allow_explicit: bool = False
    cast_card_ids: list[str] = field(default_factory=list)
    caption: str = ""
