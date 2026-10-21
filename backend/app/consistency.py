"""Cruzamento ponta a ponta da aventura.

A cadeia que faz o jogo funcionar: ficha do NPC -> retrato -> imagem; memória
-> embedding -> recuperação; narração -> cânone -> mundo. Cada elo depende do
anterior, e um elo quebrado (ficha sem roupa, retrato de uma ficha antiga,
memória sem vetor, cena pintando outra momento) aparece só como resultado
estranho. Este módulo audita a cadeia e repara o que é reparável, para o
sistema se comportar como o planejado em vez de depender de sorte do LLM.

Checks (id):
  card_sheets     ficha dos personagens com campos, stats e classe preenchidos
  portraits       prompt de retrato coerente com a ficha e retrato gerado
  memories        memória do banco com embedding utilizável (modelo/dimensão)
  npc_minds       NPCs com mente viva (humor, sentimentos, memórias)
  canon_world     o cânone narrado não contradiz o mundo
  scene_alignment o último fundo e o último "See" pintaram a mesma cena

Reparáveis: card_sheets, portraits, memories, npc_minds. Cânone e cena
são diagnóstico: o botão de reparo não aparece para eles.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from typing import Any

from app import db, embeddings_client, npc_assets, npc_life, settings_store, world_facts
from app.errors import GameError

log = logging.getLogger("aidungeon.consistency")

OK = "ok"
WARN = "warn"
ERROR = "error"

CHECKS = ("card_sheets", "portraits", "memories", "npc_minds",
          "canon_world", "scene_alignment")
REPAIRABLE = ("card_sheets", "portraits", "memories", "npc_minds")

# Marcadores de nudez (pt/en). A ficha fala pt-BR, o prompt de retrato fala en.
_NUDITY = re.compile(
    r"(?i)\b(nude|naked|nudity|unclothed|undressed|bare[- ]?chest|topless)\b"
    r"|\bnua?s?\b|\bdesnudas?\b|\bdesnudo\b|\bsem roupa\b|\bnu\b"
)
# Palavras que a peça do english_appearance traduz (roupa/aparência em pt -> en).
_CLOTHING = re.compile(
    r"(?i)\b(wearing|dressed in|clad in|clothed in|garments?|garb|attire|"
    r"robe|robes|gown|dress|shirt|armor|armour|cape|cloak|vest|silk|"
    r"vestes?|vestido|vestida|roupa|armadura|armor)\b"
)

_WORD = re.compile(r"[A-Za-zÀ-ÿ]{3,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "out",
    "are", "was", "were", "has", "have", "had", "not", "but", "you", "her",
    "his", "she", "him", "its", "her", "uma", "dos", "das", "com", "que",
    "por", "para", "como", "ela", "ele", "seu", "sua", "the", "uma", "não",
    "the", "them", "their", "over", "under", "near", "onto", "from",
}


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP]


def _longest_common_run(left: list[str], right: list[str]) -> int:
    """Maior sequência contígua de palavras iguais entre duas listas."""
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    best = 0
    for word in left:
        cur = [0] * (len(right) + 1)
        for j, other in enumerate(right, start=1):
            if word == other:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _check(cid: str, name: str, status: str, detail: str,
           items: list[dict] | None = None) -> dict:
    return {"id": cid, "name": name, "status": status, "detail": detail,
            "items": items or [], "repairable": cid in REPAIRABLE}


def _characters(cards: list[dict]) -> list[dict]:
    return [c for c in cards if (c.get("type") or "") == "character"]


def _is_player_card(card: dict) -> bool:
    """Ficha do jogador (não é NPC): tem outro schema."""
    return bool((card.get("player_username") or "").strip())


def _npc_characters(cards: list[dict]) -> list[dict]:
    return [c for c in _characters(cards) if not _is_player_card(c)]


# --------------------------------------------------------------- checks

def _check_card_sheets(conn: sqlite3.Connection, cards: list[dict]) -> dict:
    chars = _characters(cards)
    if not chars:
        return _check("card_sheets", "Fichas dos personagens", OK,
                      "Nenhum personagem na aventura.")
    items: list[dict] = []
    queued: list[dict] = []
    for card in chars:
        sheet = npc_assets.parse_sheet(card.get("sheet"))
        # A ficha do jogador segue o schema do personagem (status, classe,
        # atributos); a do NPC tem os campos de vida (humor, laços, ...).
        wanted = ("status", "class") if _is_player_card(card) else npc_assets.SHEET_TEXT_FIELDS
        missing = [f for f in wanted
                   if not (isinstance(sheet.get(f), str) and sheet[f].strip())]
        stats = sheet.get("stats") if isinstance(sheet.get("stats"), dict) else {}
        missing_stats = [k for k in npc_assets.STATS_KEYS
                         if not isinstance(stats.get(k), int)
                         or not 1 <= int(stats[k]) <= 20]
        if not (missing or missing_stats):
            continue
        item = {"card_id": card.get("id"), "title": card.get("title"),
                "missing_fields": missing, "missing_stats": missing_stats}
        # Ficha na fila é pendência, não falha: o job card_assets preenche
        # os campos em branco quando roda.
        pending = (not _is_player_card(card)
                   and db.find_active_job(conn, "card_assets", card.get("id")) is not None)
        if pending:
            item["fix"] = "wait_for_assets"
            queued.append(item)
        else:
            items.append(item)
    if items:
        return _check("card_sheets", "Fichas dos personagens", ERROR,
                      f"{len(items)} personagem(ns) com ficha incompleta.", items)
    if queued:
        return _check("card_sheets", "Fichas dos personagens", WARN,
                      f"{len(queued)} ficha(s) em preenchimento na fila.", queued)
    return _check("card_sheets", "Fichas dos personagens", OK,
                  f"{len(chars)} personagem(ns) com ficha completa.")


def _check_portraits(conn: sqlite3.Connection, adv: dict, cards: list[dict]) -> dict:
    chars = _characters(cards)
    if not chars:
        return _check("portraits", "Retratos", OK, "Nenhum personagem na aventura.")
    items: list[dict] = []
    for card in chars:
        sheet = npc_assets.parse_sheet(card.get("sheet"))
        prompt = (card.get("portrait_prompt") or "").strip()
        clothing = (sheet.get("clothing") or "").strip()
        if not prompt:
            # O job de retrato escreve o prompt junto com a imagem; enquanto ele
            # está na fila, não é falha — é pendência.
            if db.find_active_job(conn, "character_portrait", card["id"]):
                items.append({"card_id": card.get("id"), "title": card.get("title"),
                              "issue": "retrato na fila: o prompt sai com a imagem",
                              "fix": "generate_portrait"})
            else:
                items.append({"card_id": card.get("id"), "title": card.get("title"),
                              "issue": "sem prompt de retrato", "fix": "rebuild_prompt"})
            continue
        if clothing and _NUDITY.search(prompt):
            items.append({"card_id": card.get("id"), "title": card.get("title"),
                          "issue": "a ficha descreve roupa, o prompt de retrato diz nudez",
                          "fix": "rebuild_prompt"})
        image_id = card.get("portrait_image_id")
        if not image_id:
            if not db.find_active_job(conn, "character_portrait", card["id"]):
                items.append({"card_id": card.get("id"), "title": card.get("title"),
                              "issue": "retrato ainda nao gerado", "fix": "generate_portrait"})
            continue
        image = db.get_image(conn, image_id)
        image_prompt = (image or {}).get("prompt") or ""
        if clothing and image_prompt and not _CLOTHING.search(image_prompt):
            items.append({"card_id": card.get("id"), "title": card.get("title"),
                          "issue": "retrato gerado de outra ficha (sem roupa no prompt)",
                          "fix": "regenerate_portrait"})
    errors = [i for i in items if i["fix"] in ("rebuild_prompt", "regenerate_portrait")]
    warns = [i for i in items if i["fix"] == "generate_portrait"]
    if errors:
        return _check("portraits", "Retratos", ERROR,
                      f"{len(errors)} retrato(s) fora de sintonia com a ficha.", items)
    if warns:
        return _check("portraits", "Retratos", WARN,
                      f"{len(warns)} personagem(ns) sem retrato gerado.", items)
    return _check("portraits", "Retratos", OK,
                  f"{len(chars)} retrato(s) em sintonia com a ficha.")


def _check_memories(conn: sqlite3.Connection, adventure_id: str) -> dict:
    cfg = settings_store.effective_embeddings_settings(conn)
    model = cfg.get("model") or embeddings_client.DEFAULT_MODEL
    mems = db.list_memories(conn, adventure_id, include_vector=True)
    if not mems:
        return _check("memories", "Memórias e embeddings", OK,
                      "Nenhuma memória longa gravada ainda.")
    items: list[dict] = []
    if not cfg.get("enabled", True):
        for m in mems:
            items.append({"memory_id": m.get("id"), "issue": "embeddings desligado"})
        return _check("memories", "Memórias e embeddings", WARN,
                      "Embeddings desligado: a memória existe mas não é recuperável por vetor.",
                      items)
    missing = [m for m in mems if not m.get("embedding")]
    stale = [m for m in mems
             if m.get("embedding") and (m.get("embedding_model") or "") != model]
    dims = [m.get("embedding_dim") for m in mems
            if m.get("embedding") and m.get("embedding_dim")]
    majority = Counter(dims).most_common(1)[0][0] if dims else None
    wrong_dim = [m for m in mems if m.get("embedding") and majority
                 and m.get("embedding_dim") and m["embedding_dim"] != majority]
    for m in missing:
        items.append({"memory_id": m.get("id"), "issue": "sem embedding"})
    for m in stale:
        items.append({"memory_id": m.get("id"), "issue": f"embedding de outro modelo ({m.get('embedding_model')})"})
    for m in wrong_dim:
        items.append({"memory_id": m.get("id"), "issue": f"embedding com dimensão divergente ({m.get('embedding_dim')} != {majority})"})
    if items:
        return _check("memories", "Memórias e embeddings", ERROR,
                      f"{len(items)} memória(s) sem vetor utilizável.", items)
    return _check("memories", "Memórias e embeddings", OK,
                  f"{len(mems)} memória(s) com embedding utilizável.")


def _mind_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has_simulated_life(sheet: dict, memories: list) -> bool:
    """Humor e sentimentos ficam na ficha. O bloco `life` guarda humor e rotina.

    `life.sentiments` não é gravado em lugar nenhum: olhar só para ele
    acusava uma ficha completa.
    """
    life = sheet.get("life") if isinstance(sheet.get("life"), dict) else {}
    if any(_mind_text(sheet.get(key)) for key in ("humor", "sentiments")):
        return True
    if any(_mind_text(life.get(key)) for key in ("mood", "activity", "sentiments")):
        return True
    return bool(memories)


def _npc_phrase(count: int, tail: str) -> str:
    noun = "NPC" if count == 1 else "NPCs"
    return f"{count} {noun} {tail}"


def _check_npc_minds(conn: sqlite3.Connection, adventure_id: str,
                     cards: list[dict]) -> dict:
    chars = _npc_characters(cards)
    if not chars:
        return _check("npc_minds", "Mentes dos NPCs", OK, "Nenhum NPC na aventura.")
    items: list[dict] = []
    for card in chars:
        sheet = npc_assets.parse_sheet(card.get("sheet"))
        try:
            mems = npc_life.list_memories(conn, adventure_id, card["id"], limit=50)
        except Exception:  # memória de NPC é opcional para o relatório
            mems = []
        if not _has_simulated_life(sheet, mems):
            items.append({"card_id": card.get("id"), "title": card.get("title"),
                          "issue": "sem vida simulada (humor, sentimentos, memórias)"})
    if items:
        return _check("npc_minds", "Mentes dos NPCs", WARN,
                      _npc_phrase(len(items), "sem vida simulada."), items)
    return _check("npc_minds", "Mentes dos NPCs", OK,
                  _npc_phrase(len(chars), "com vida simulada."))


def _check_canon_world(conn: sqlite3.Connection, adv: dict) -> dict:
    actions = db.list_actions(conn, adv["id"])
    cards = db.list_cards(conn, adv["id"])
    mems = db.list_memories(conn, adv["id"])
    assessment = world_facts.assess(adv, actions, cards, mems)
    blob = (assessment.canon_blob or "").lower()
    items: list[dict] = []
    for name, kind in ([(p, "place") for p in assessment.blocked_places]
                       + [(i, "item") for i in assessment.blocked_items]
                       + [(p, "place") for p in assessment.invented_places]
                       + [(p, "person") for p in assessment.invented_people]):
        if name and name.lower() in blob:
            items.append({"name": name, "kind": kind,
                          "issue": "usado na narrativa apesar de bloqueado"})
    if items:
        return _check("canon_world", "Cânone x mundo", ERROR,
                      f"{len(items)} contradição(ões) entre o cânone e o mundo.", items)
    return _check("canon_world", "Cânone x mundo", OK,
                  "O cânone narrado não contradiz o mundo.")


def _check_scene_alignment(conn: sqlite3.Connection, adventure_id: str) -> dict:
    rows = db.list_jobs(conn, adventure_id)

    def last_done(kind: str) -> dict | None:
        for row in reversed(rows):
            if row.get("type") == kind and row.get("status") == "done":
                return row
        return None

    see = last_done("see_image")
    backdrop = last_done("scene_backdrop")
    if not see or not backdrop:
        return _check("scene_alignment", "Cena x narração", OK,
                      "Sem imagens concluídas para comparar.")
    p_see = (see.get("payload") or {}).get("prompt") or ""
    p_back = (backdrop.get("payload") or {}).get("prompt") or ""
    if not p_see or not p_back:
        return _check("scene_alignment", "Cena x narração", OK,
                      "Sem prompts comparáveis.")
    run = _longest_common_run(_words(p_see), _words(p_back))
    if run < 6:
        return _check("scene_alignment", "Cena x narração", WARN,
                      "O fundo e o See pintaram cenas diferentes.",
                      [{"see_job": see.get("id"), "backdrop_job": backdrop.get("id"),
                        "common_words": run}])
    return _check("scene_alignment", "Cena x narração", OK,
                  "O fundo e o See pintaram a mesma cena.",
                  [{"see_job": see.get("id"), "backdrop_job": backdrop.get("id"),
                    "common_words": run}])


# ----------------------------------------------------------------- audit

def audit(conn: sqlite3.Connection, adventure_id: str) -> dict:
    """Audita a cadeia inteira da aventura. Devolve o relatório."""
    adv = db.get_adventure(conn, adventure_id)
    if not adv:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    cards = db.list_cards(conn, adventure_id)
    checks = [
        _check_card_sheets(conn, cards),
        _check_portraits(conn, adv, cards),
        _check_memories(conn, adventure_id),
        _check_npc_minds(conn, adventure_id, cards),
        _check_canon_world(conn, adv),
        _check_scene_alignment(conn, adventure_id),
    ]
    ok = not any(c["status"] == ERROR for c in checks)
    return {"ok": ok, "adventure_id": adventure_id, "checks": checks,
            "counts": {"ok": sum(1 for c in checks if c["status"] == OK),
                       "warn": sum(1 for c in checks if c["status"] == WARN),
                       "error": sum(1 for c in checks if c["status"] == ERROR)}}


# ---------------------------------------------------------------- repair

def repair(conn: sqlite3.Connection, adventure_id: str, check_id: str,
           *, llm_cfg: dict | None = None,
           transport: Any = None) -> dict:
    """Repara um check reparável. Devolve o que foi refeito."""
    if check_id not in REPAIRABLE:
        raise GameError(422, "invalid_request", f"Check nao reparavel: {check_id}")
    adv = db.get_adventure(conn, adventure_id)
    if not adv:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    if check_id == "card_sheets":
        return _repair_cards(conn, adv, llm_cfg, transport)
    if check_id == "portraits":
        return _repair_portraits(conn, adv, llm_cfg, transport)
    if check_id == "npc_minds":
        return _repair_npc_minds(conn, adv, llm_cfg, transport)
    return _repair_memories(conn, adventure_id, transport)


def _repair_cards(conn: sqlite3.Connection, adv: dict, llm_cfg: dict | None,
                  transport: Any) -> dict:
    report = _check_card_sheets(conn, db.list_cards(conn, adv["id"]))
    repaired: list[dict] = []
    errors: list[str] = []
    for item in report["items"]:
        card = db.get_card(conn, adv["id"], item["card_id"])
        if not card:
            continue
        if _is_player_card(card):
            # Ficha do jogador: quem completa é o PUT /character.
            errors.append(f"{card.get('title')}: ficha do jogador — "
                          "complete pelo PUT /adventures/{id}/character")
            continue
        try:
            updated = npc_assets.enrich_card(conn, adv, card, llm_cfg or {},
                                             transport)
            repaired.append({"card_id": card["id"], "title": card.get("title"),
                             "sheet": npc_assets.parse_sheet(updated.get("sheet"))})
        except Exception as exc:  # LLM fora: completa o que é determinístico
            log.warning("consistency: falha ao refazer a ficha de %s: %s",
                        card.get("title"), exc)
            before = npc_assets.parse_sheet(card.get("sheet") or "")
            partial = npc_assets.ensure_sheet_complete(
                before, entry=card.get("entry") or "", card=card)
            if partial != before:
                db.update_card(conn, adv["id"], card["id"],
                               {"sheet": json.dumps(partial, ensure_ascii=False)})
                repaired.append({"card_id": card["id"], "title": card.get("title"),
                                 "sheet": partial, "partial": True})
            errors.append(
                f"{card.get('title')}: {exc} — completado só com o que é "
                "determinístico (status, classe, atributos); os textos vivos "
                "precisam do LLM")
    return {"check": "card_sheets", "repaired": repaired, "errors": errors}


def _repair_portraits(conn: sqlite3.Connection, adv: dict, llm_cfg: dict | None,
                      transport: Any) -> dict:
    from app import jobs

    report = _check_portraits(conn, adv, db.list_cards(conn, adv["id"]))
    repaired: list[dict] = []
    errors: list[str] = []
    for item in report["items"]:
        card = db.get_card(conn, adv["id"], item["card_id"])
        if not card:
            continue
        fix = item.get("fix")
        try:
            if fix == "rebuild_prompt":
                updated = npc_assets.enrich_card(conn, adv, card, llm_cfg or {},
                                                 transport)
                repaired.append({"card_id": card["id"], "fix": fix,
                                 "portrait_prompt": updated.get("portrait_prompt") or ""})
                jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)
            elif fix == "generate_portrait":
                jobs.enqueue_portrait(conn, adv["id"], card["id"])
                repaired.append({"card_id": card["id"], "fix": fix})
            elif fix == "regenerate_portrait":
                jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)
                repaired.append({"card_id": card["id"], "fix": fix})
        except Exception as exc:
            log.warning("consistency: falha no retrato de %s: %s",
                        card.get("title"), exc)
            errors.append(f"{card.get('title')}: {exc}")
    return {"check": "portraits", "repaired": repaired, "errors": errors}


def _repair_npc_minds(conn: sqlite3.Connection, adv: dict, llm_cfg: dict | None,
                      transport: Any) -> dict:
    """Preenche humor e sentimentos da ficha. A mente do turno continua no pós-turno."""
    report = _check_npc_minds(conn, adv["id"], db.list_cards(conn, adv["id"]))
    repaired: list[dict] = []
    errors: list[str] = []
    for item in report["items"]:
        card = db.get_card(conn, adv["id"], item["card_id"])
        if not card or _is_player_card(card):
            continue
        try:
            updated = npc_assets.enrich_card(conn, adv, card, llm_cfg or {},
                                             transport)
        except Exception as exc:
            log.warning("consistency: falha ao refazer a mente de %s: %s",
                        card.get("title"), exc)
            errors.append(f"{card.get('title')}: {exc}")
            continue
        sheet = npc_assets.parse_sheet(updated.get("sheet"))
        repaired.append({"card_id": card["id"], "title": card.get("title"),
                         "sentiments": _mind_text(sheet.get("sentiments")),
                         "humor": _mind_text(sheet.get("humor"))})
    return {"check": "npc_minds", "repaired": repaired, "errors": errors}


def _repair_memories(conn: sqlite3.Connection, adventure_id: str,
                     transport: Any) -> dict:
    cfg = settings_store.effective_embeddings_settings(conn)
    model = cfg.get("model") or embeddings_client.DEFAULT_MODEL
    mems = db.list_memories(conn, adventure_id, include_vector=True)
    repaired: list[dict] = []
    errors: list[str] = []
    for m in mems:
        if m.get("embedding") and (m.get("embedding_model") or "") == model:
            continue
        try:
            vec = embeddings_client.get_embedding(m.get("text") or "", cfg=cfg,
                                                  transport=transport)
        except Exception as exc:
            log.warning("consistency: embedding falhou para a memoria %s: %s",
                        m.get("id"), exc)
            errors.append(f"memoria {m.get('id')}: {exc}")
            continue
        if not vec:
            errors.append(f"memoria {m.get('id')}: servico de embeddings sem resposta")
            continue
        updated = db.update_memory(conn, int(m["id"]),
                                   embedding=embeddings_client.pack_vector(vec),
                                   embedding_model=model, embedding_dim=len(vec))
        if updated:
            repaired.append({"memory_id": m["id"], "dim": len(vec)})
    return {"check": "memories", "repaired": repaired, "errors": errors}
