"""Plano de imagem (contrato C4).

Foto enviada e referência de pessoa real entram no Forge como ControlNet
`reference_adain+attn` (See e fundo). O retrato ainda usa a mesma foto
como img2img, com denoise baixo quando o usuário enviou o arquivo.
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
