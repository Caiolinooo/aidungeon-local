"""Importador de backup do AI Dungeon (.zip) — PLANO.md §2.1/§4.6.

Mapeamento (backup → banco):
    metadata.json
      adventure.title/description/tags/thirdPerson  → adventures.*
      adventure.memory        → adventures.plot_essentials
      adventure.authorsNote   → adventures.authors_note
      state.instructions.scenario → adventures.ai_instructions
      state.storySummary      → adventures.story_summary
      state.imageStyle        → adventures.image_style (v2)
      state.storyCards[]      → story_cards
         .keys (", Baldur's Gate ,") → triggers (split ',' + trim, ignora vazios)
         .value / .description  → entry
         .type / .title / .isPinned / .useForCharacterCreation
      state.memories[]        → memories (já vêm resumidas — copiadas como estão;
                                seq_start/seq_end derivados de actionIds, best-effort)
    actions-NNN.json (concatenar por partNumber)
      actions[] → actions com seq = ORDEM DO ARRAY (não o id do backup!)

Cria scenario implícito + adventure com source='import'. O zip é extraído em
diretório temporário NOVO a cada importação, com validação de path traversal.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from app import db


class BackupFormatError(Exception):
    """Arquivo inválido ou formato não reconhecido (→ HTTP 400)."""


# ------------------------------------------------------------------ zip
def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    for name in zf.namelist():
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            raise BackupFormatError(f"caminho inseguro no zip: {name!r}")
    zf.extractall(dest)


def import_zip(zip_path: str | Path, cards_payload: Any | None = None) -> dict:
    """Importa um .zip de backup. `cards_payload` = JSON solto de cards (merge/validação).

    Aceita dois formatos: backup individual (metadata.json na raiz) e backup
    completo do app (uma pasta NNN-slug/ por aventura, cada uma com seu
    metadata.json) — este último devolve {"multi": True, "results": [...]}.
    """
    tmp = tempfile.mkdtemp(prefix="aidungeon_import_")
    try:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extract(zf, tmp)
        except zipfile.BadZipFile as exc:
            raise BackupFormatError("arquivo não é um zip válido") from exc

        root = Path(tmp)
        if (root / "metadata.json").exists():
            return import_dir(root, cards_payload=cards_payload)

        folders = sorted(p for p in root.iterdir()
                         if p.is_dir() and (p / "metadata.json").exists())
        if not folders:
            raise BackupFormatError("metadata.json ausente no backup")
        results = []
        for folder in folders:
            try:
                results.append(import_dir(folder))
            except BackupFormatError as exc:
                results.append({"title": folder.name, "error": str(exc)})
        imported = sum(1 for r in results if "error" not in r)
        if imported == 0:
            raise BackupFormatError(
                "nenhuma aventura importável no backup: "
                + "; ".join(r["error"] for r in results[:3]))
        return {"multi": True, "imported": imported, "results": results}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- parsing
def _load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackupFormatError(f"{what} ausente no backup") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupFormatError(f"{what} não é JSON válido") from exc


def _split_keys(keys: Any) -> list[str]:
    """', Baldur's Gate ,' → [\"Baldur's Gate\"] (split ',' + trim, ignora vazios)."""
    if not isinstance(keys, str):
        return []
    return [part.strip() for part in keys.split(",") if part.strip()]


def _load_action_parts(root: Path) -> list[dict]:
    parts = []
    for path in sorted(root.glob("actions-*.json")):
        data = _load_json(path, path.name)
        if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
            raise BackupFormatError(f"{path.name}: formato inesperado (sem lista 'actions')")
        parts.append(data)
    if not parts:
        raise BackupFormatError("nenhum actions-*.json encontrado no backup")
    parts.sort(key=lambda d: d.get("partNumber", 0))
    return parts


# --------------------------------------------------------------- importação
def import_dir(root: str | Path, cards_payload: Any | None = None) -> dict:
    root = Path(root)
    metadata = _load_json(root / "metadata.json", "metadata.json")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("adventure"), dict):
        raise BackupFormatError("metadata.json sem objeto 'adventure'")
    src_adv = metadata["adventure"]
    state = metadata.get("state") or {}
    warnings: list[str] = []

    # ---- ações: concatenar por partNumber; seq = ORDEM DO ARRAY ------------
    parts = _load_action_parts(root)
    raw_actions: list[dict] = []
    for part in parts:
        raw_actions.extend(part["actions"])
    backup_ids = [str(a.get("id")) for a in raw_actions]
    if backup_ids != [str(i) for i in range(len(backup_ids))]:
        warnings.append("ids do backup não são contíguos; seq atribuído pela ordem do array")

    # ---- campos mapeados ----------------------------------------------------
    title = src_adv.get("title") or "Imported Adventure"
    description = src_adv.get("description") or ""
    tags = src_adv.get("tags") if isinstance(src_adv.get("tags"), list) else []
    third_person = bool(src_adv.get("thirdPerson"))
    ai_instructions = (state.get("instructions") or {}).get("scenario") or ""
    plot_essentials = src_adv.get("memory") or ""
    authors_note = src_adv.get("authorsNote") or ""
    story_summary = state.get("storySummary") or ""
    image_style = state.get("imageStyle") or ""
    opening_text = ""
    if raw_actions and raw_actions[0].get("type") == "start":
        opening_text = raw_actions[0].get("text") or ""

    with db.session() as conn:
        # scenario implícito + adventure (source='import')
        scenario = db.insert_scenario(
            conn, title=title, description=description,
            ai_instructions=ai_instructions, plot_essentials=plot_essentials,
            authors_note=authors_note, opening_text=opening_text,
            tags=tags, third_person=third_person,
            created_at=src_adv.get("createdAt"), updated_at=src_adv.get("updatedAt"),
        )
        adv = db.insert_adventure(
            conn, scenario_id=scenario["id"], title=title, description=description,
            ai_instructions=ai_instructions, plot_essentials=plot_essentials,
            authors_note=authors_note, story_summary=story_summary,
            image_style=image_style,
            tags=tags, third_person=third_person, source="import",
            created_at=src_adv.get("createdAt"), updated_at=src_adv.get("updatedAt"),
        )

        id_to_seq: dict[str, int] = {}
        actions_imported = 0
        for seq, raw in enumerate(raw_actions):
            type_ = raw.get("type")
            if type_ not in db.ACTION_TYPES:
                warnings.append(f"action[{seq}] com tipo desconhecido {type_!r} → 'story'")
                type_ = "story"
            db.insert_action(conn, adv["id"], seq, type_, raw.get("text") or "",
                             created_at=raw.get("createdAt"))
            id_to_seq[str(raw.get("id"))] = seq
            actions_imported += 1

        # ---- story cards ------------------------------------------------------
        cards_imported = 0
        src_cards = state.get("storyCards") or []
        if not isinstance(src_cards, list):
            warnings.append("state.storyCards não é lista; ignorado")
            src_cards = []
        for i, card in enumerate(src_cards):
            if not isinstance(card, dict):
                warnings.append(f"storyCards[{i}] inválido; ignorado")
                continue
            if not (card.get("title") or "").strip():
                warnings.append(f"storyCards[{i}] sem título")
            db.insert_card(
                conn, adv["id"],
                title=card.get("title") or "",
                type=card.get("type") or "other",
                triggers=_split_keys(card.get("keys")),
                entry=card.get("value") or card.get("description") or "",
                pinned=bool(card.get("isPinned")),
                use_for_character_creation=bool(card.get("useForCharacterCreation")),
                created_at=card.get("createdAt"), updated_at=card.get("updatedAt"),
            )
            cards_imported += 1

        # ---- memories (copiadas como estão; seqs derivados best-effort) -------
        memories_imported = 0
        src_mems = state.get("memories") or []
        if not isinstance(src_mems, list):
            warnings.append("state.memories não é lista; ignorado")
            src_mems = []
        for i, mem in enumerate(src_mems):
            text = mem.get("text") if isinstance(mem, dict) else str(mem)
            if not text:
                warnings.append(f"memories[{i}] sem texto; ignorada")
                continue
            seq_start = seq_end = None
            action_ids = mem.get("actionIds") if isinstance(mem, dict) else None
            if isinstance(action_ids, list) and action_ids:
                mapped = [id_to_seq[str(a)] for a in action_ids if str(a) in id_to_seq]
                if mapped:
                    seq_start, seq_end = min(mapped), max(mapped)
                else:
                    warnings.append(f"memories[{i}]: actionIds não mapeáveis para seq")
            db.insert_memory(conn, adv["id"], text, seq_start, seq_end)
            memories_imported += 1

    # ---- JSON solto de story cards: merge/validação (redundante no backup) --
    if cards_payload is not None:
        loose = cards_payload
        if isinstance(loose, dict):
            loose = loose.get("storyCards") or loose.get("cards") or []
        if not isinstance(loose, list):
            warnings.append("JSON solto de story cards não é lista; ignorado")
        else:
            loose_titles = {str(c.get("title", "")).strip() for c in loose if isinstance(c, dict)}
            imported_titles = {str(c.get("title", "")).strip() for c in src_cards if isinstance(c, dict)}
            missing = loose_titles - imported_titles
            if missing:
                warnings.append(
                    f"{len(missing)} card(s) do JSON solto ausentes no metadata: "
                    + ", ".join(sorted(missing)[:5]))
            if len(loose) != len(src_cards):
                warnings.append(
                    f"JSON solto tem {len(loose)} cards; metadata tem {len(src_cards)}")

    return {
        "adventure_id": adv["id"],
        "title": title,
        "actions_imported": actions_imported,
        "cards_imported": cards_imported,
        "memories_imported": memories_imported,
        "warnings": warnings,
    }
