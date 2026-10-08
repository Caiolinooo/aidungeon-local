"""Replay E2E do backup real "Faerûn: Caio" (PLANO.md §6 — critério da Fase 6).

Fluxo (tudo via API, com Bearer de teste do conftest e LLM 100% MOCKADO):

(a) importa `D:\\Projeto\\AI_Dungeon_CAIO\\faern-caio-backup.zip` (SOMENTE
    LEITURA — lido em memória; o importador extrai em temp dir próprio) via
    POST /api/import/aidungeon;
(b) verifica contagens exatas: 201 ações (ordem e tipos iguais ao backup; seq
    contíguo 0..200), 236 cards, 25 memórias, e plot_essentials/authors_note/
    ai_instructions iguais ao metadata.json;
(c) REPLAY: importa uma 2ª cópia e reenvia TODAS as 83 ações do jogador
    (do/say/story) do backup via POST /api/adventures/{id}/action, consumindo
    o SSE até `done`. Verifica: nenhuma falha 4xx/5xx, todo stream termina em
    `done` com a action `continue` persistida, seq sempre contíguo e nenhum
    payload ao LLM estourando o orçamento de contexto (payloads capturados);
(d) ROUND-TRIP: exporta a 1ª cópia (intacta) via GET /api/adventures/{id}/export
    e compara semanticamente com o backup (ações na ordem, cards, memórias,
    campos de metadata).

Mock do LLM: httpx.MockTransport determinístico — eco numerado curto em 2ª
pessoa para geração (SSE) e resumo fake para o memory bank (não-stream).
O summarizer (chamado pelo game_engine sem transport) é desviado para o mesmo
mock via monkeypatch — sem isso ele tentaria a rede real em cada geração.
O LLM real NUNCA é chamado e o teste é rápido e determinístico.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import httpx
import pytest

from app import context_builder, db, settings_store

from conftest import sse_body

BACKUP_PATH = Path(r"D:\Projeto\AI_Dungeon_CAIO\faern-caio-backup.zip")

pytestmark = pytest.mark.skipif(
    not BACKUP_PATH.is_file(),
    reason="backup real ausente (esperado em D:\\Projeto\\AI_Dungeon_CAIO)",
)

PLAYER_TYPES = ("do", "say", "story")


# ------------------------------------------------------------- dados do backup
@pytest.fixture(scope="module")
def backup_zip_bytes() -> bytes:
    """Bytes do zip (somente leitura — o arquivo nunca é modificado)."""
    return BACKUP_PATH.read_bytes()


@pytest.fixture(scope="module")
def backup_ref(backup_zip_bytes) -> dict:
    """metadata.json + ações concatenadas (actions-*.json por partNumber)."""
    with zipfile.ZipFile(io.BytesIO(backup_zip_bytes)) as zf:
        meta = json.loads(zf.read("metadata.json").decode("utf-8"))
        parts = []
        for name in zf.namelist():
            if name.startswith("actions-") and name.endswith(".json"):
                parts.append(json.loads(zf.read(name).decode("utf-8")))
        parts.sort(key=lambda d: d.get("partNumber", 0))
        actions: list[dict] = []
        for part in parts:
            actions.extend(part["actions"])
    return {"metadata": meta, "actions": actions}


# ------------------------------------------------------------------ mock LLM
class DeterministicMockLLM:
    """Servidor llama.cpp fake: /models + /chat/completions (SSE e JSON).

    Geração (stream=true): eco numerado curto em 2ª pessoa — "You ..." com o
    número da chamada, determinístico pela ordem das chamadas.
    Resumo (stream=false, memory bank / story summary): JSON com texto fake.
    Payloads de geração são capturados para auditoria de orçamento.
    """

    def __init__(self) -> None:
        self.generation_calls = 0
        self.summary_calls = 0
        self.payloads: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/models"):
                return httpx.Response(200, json={
                    "object": "list",
                    "data": [{"id": "mock-echo-model", "object": "model"}],
                })
            if path.endswith("/chat/completions"):
                payload = json.loads(request.content.decode("utf-8"))
                if payload.get("stream"):
                    self.generation_calls += 1
                    self.payloads.append(payload)
                    text = (f"You press onward, weighing your next move. "
                            f"[mock {self.generation_calls:03d}]")
                    return httpx.Response(
                        200, headers={"content-type": "text/event-stream"},
                        text=sse_body((text,)),
                    )
                self.summary_calls += 1
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": (
                        f"Mock summary {self.summary_calls}: you continued "
                        "your journey through Faerûn.")}}],
                })
            return httpx.Response(404, json={"error": f"no mock route: {path}"})

        return httpx.MockTransport(handler)


@pytest.fixture()
def mock_llm(monkeypatch):
    """Injeta o mock em TODOS os caminhos de LLM da API:

    - geração (rotas SSE): dependency override de `get_http_transport`;
    - summarizer (game_engine chama maybe_update SEM transport): monkeypatch
      que injeta o mock — senão ele tentaria a rede real a cada geração.
    """
    mock = DeterministicMockLLM()
    transport = mock.transport()

    from app.deps import get_http_transport
    from app.main import app
    app.dependency_overrides[get_http_transport] = lambda: transport

    import memory.summarizer as summarizer
    original = summarizer.maybe_update

    def patched(adventure_id, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(adventure_id, **kwargs)

    monkeypatch.setattr(summarizer, "maybe_update", patched)

    yield mock

    app.dependency_overrides.pop(get_http_transport, None)


# ------------------------------------------------------------------ helpers
def import_backup(client, zip_bytes: bytes) -> dict:
    """POST /api/import/aidungeon → ImportReport (já autenticado pelo fixture)."""
    res = client.post(
        "/api/import/aidungeon",
        files={"file": ("faern-caio-backup.zip", zip_bytes, "application/zip")},
    )
    assert res.status_code == 201, f"import falhou: {res.status_code} {res.text[:300]}"
    return res.json()


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse do fluxo SSE da API → [(event, data-json)]."""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        assert event is not None and data is not None, f"bloco SSE inválido: {block!r}"
        events.append((event, json.loads(data)))
    return events


_DO_RE = re.compile(r"^>\s*You\s+(.*)$", re.DOTALL)
# O backup grava says como '> You say, "..."' (com vírgula) E '> You say "..."'.
_SAY_RE = re.compile(r"^>\s*You\s+say[,\s]*\"(.*)\"\s*$", re.DOTALL)


def inner_player_text(type_: str, text: str) -> str:
    """Desfaz a formatação do backup p/ reenviar — o engine reformata na entrada.

    (game_engine._format_player_text sempre embrulha: mandar o texto já
    formatado geraria '> You > You ...'.)
    """
    stripped = text.strip()
    if type_ == "do":
        m = _DO_RE.match(stripped)
        return (m.group(1) if m else stripped).strip()
    if type_ == "say":
        m = _SAY_RE.match(stripped)
        return (m.group(1) if m else stripped).strip()
    return text  # story: prosa pura


def _split_keys(keys) -> list[str]:
    if not isinstance(keys, str):
        return []
    return [part.strip() for part in keys.split(",") if part.strip()]


def _card_key(card: dict) -> tuple:
    """Identidade semântica de um card (ordem e ids não importam)."""
    return (
        card.get("title") or "",
        card.get("type") or "other",
        card.get("value") or card.get("description") or "",
        tuple(sorted(_split_keys(card.get("keys")))),
        bool(card.get("isPinned")),
        bool(card.get("useForCharacterCreation")),
    )


# --------------------------------------------------- (a)+(b) importação e contagens
def test_import_backup_exact_counts(client, backup_zip_bytes, backup_ref):
    meta = backup_ref["metadata"]
    ref_actions = backup_ref["actions"]
    ref_state = meta["state"]

    report = import_backup(client, backup_zip_bytes)
    assert report["title"] == meta["adventure"]["title"]
    assert report["actions_imported"] == 201
    assert report["cards_imported"] == 236
    assert report["memories_imported"] == 25
    # ids do backup têm buracos (0..211) — o importador avisa e usa a ordem do array
    assert any("não são contíguos" in w for w in report["warnings"])

    res = client.get(f"/api/adventures/{report['adventure_id']}")
    assert res.status_code == 200
    state = res.json()

    # ações: ordem, tipos e textos idênticos ao backup; seq contíguo 0..200
    actions = state["actions"]
    assert len(actions) == len(ref_actions) == 201
    assert [a["seq"] for a in actions] == list(range(201))
    assert [a["type"] for a in actions] == [a["type"] for a in ref_actions]
    assert [a["text"] for a in actions] == [a.get("text") or "" for a in ref_actions]

    # cards e memórias: contagens exatas
    assert len(state["story_cards"]) == 236
    assert len(state["memories"]) == 25
    assert [m["text"] for m in state["memories"]] == [
        m["text"] for m in ref_state["memories"] if m.get("text")
    ]

    # campos de metadata mapeados byte a byte
    assert state["plot_essentials"] == meta["adventure"]["memory"]
    assert state["authors_note"] == meta["adventure"]["authorsNote"]
    assert state["ai_instructions"] == ref_state["instructions"]["scenario"]
    assert state["story_summary"] == (ref_state.get("storySummary") or "")
    assert state["tags"] == meta["adventure"]["tags"]
    assert state["third_person"] == bool(meta["adventure"]["thirdPerson"])
    assert state["source"] == "import"


# ------------------------------------------------------------ (c) replay E2E
def test_replay_all_player_actions(client, mock_llm, backup_zip_bytes, backup_ref):
    first = import_backup(client, backup_zip_bytes)
    copy = import_backup(client, backup_zip_bytes)
    adv_id = copy["adventure_id"]
    assert first["adventure_id"] != adv_id

    player_actions = [a for a in backup_ref["actions"] if a["type"] in PLAYER_TYPES]
    assert len(player_actions) == 83  # 25 do + 58 say (0 story neste backup)

    cfg = settings_store.load_file_settings()
    context_size = int(cfg["llm"]["context_size"])
    max_tokens = int(cfg["sampler"]["max_tokens"])

    expected_seq = 201  # a cópia já tem as 201 ações importadas (seq 0..200)
    for i, raw in enumerate(player_actions):
        text = inner_player_text(raw["type"], raw["text"])
        assert text, f"texto interno vazio (backup id={raw['id']})"

        res = client.post(f"/api/adventures/{adv_id}/action",
                          json={"type": raw["type"], "text": text})
        assert res.status_code == 200, (
            f"replay {i + 1}/{len(player_actions)} (backup id={raw['id']}, "
            f"type={raw['type']}): HTTP {res.status_code}: {res.text[:200]}")

        events = parse_sse(res.text)
        kinds = [e for e, _ in events]
        assert "error" not in kinds, f"replay {i}: evento error: {events[-1]}"
        assert kinds[-1] == "done", f"replay {i}: stream não terminou em done: {kinds}"
        done = events[-1][1]

        player = done["player_action"]
        cont = done["action"]
        assert player["type"] == raw["type"]
        assert player["seq"] == expected_seq
        assert cont["type"] == "continue"
        assert cont["seq"] == expected_seq + 1
        assert cont["text"].strip(), f"replay {i}: continue vazio"
        # formatação persistida = formato do engine (byte a byte, PLANO §4.2)
        if raw["type"] == "do":
            assert player["text"] == f"\n> You {text}\n"
        elif raw["type"] == "say":
            assert player["text"] == f'\n> You say "{text}"\n'
        else:
            assert player["text"] == text
        expected_seq += 2

    # uma chamada de geração por ação rejogada — nenhuma falha, nenhuma a mais
    assert mock_llm.generation_calls == len(player_actions)

    # estado final da cópia: seq contíguo 0..(201 + 2*83 - 1), sem buracos
    state = client.get(f"/api/adventures/{adv_id}").json()
    total = 201 + 2 * len(player_actions)
    assert len(state["actions"]) == total
    assert [a["seq"] for a in state["actions"]] == list(range(total))

    # a 1ª cópia permanece intacta (o replay foi só na 2ª)
    intact = client.get(f"/api/adventures/{first['adventure_id']}").json()
    assert len(intact["actions"]) == 201

    # orçamento de contexto: NENHUM payload estoura a janela do modelo
    # (estimador do próprio context_builder; teto = context_size - resposta)
    estimates = []
    for payload in mock_llm.payloads:
        est = sum(context_builder.estimate_tokens(m["content"])
                  for m in payload["messages"])
        estimates.append(est)
        assert est + max_tokens <= context_size, (
            f"contexto estourou o orçamento: ~{est} tokens + {max_tokens} de "
            f"resposta > {context_size}")
    print(f"\n[replay] {len(player_actions)} ações rejogadas · "
          f"{mock_llm.generation_calls} gerações · "
          f"{mock_llm.summary_calls} resumos (memory bank/rolling) · "
          f"contexto estimado min/med/max: "
          f"{min(estimates)}/{sum(estimates) // len(estimates)}/{max(estimates)} "
          f"tokens (teto {context_size - max_tokens})")


# ------------------------------------------------------- (d) round-trip export
def test_export_roundtrip_matches_backup(client, backup_zip_bytes, backup_ref):
    report = import_backup(client, backup_zip_bytes)
    adv_id = report["adventure_id"]

    res = client.get(f"/api/adventures/{adv_id}/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        exp_meta = json.loads(zf.read("metadata.json").decode("utf-8"))
        exp_parts = [json.loads(zf.read(n).decode("utf-8"))
                     for n in zf.namelist()
                     if n.startswith("actions-") and n.endswith(".json")]
        exp_parts.sort(key=lambda d: d.get("partNumber", 0))
        exp_actions = []
        for part in exp_parts:
            exp_actions.extend(part["actions"])

    ref_meta = backup_ref["metadata"]
    ref_actions = backup_ref["actions"]
    ref_state = ref_meta["state"]
    exp_state = exp_meta["state"]

    # ações: mesma ordem, tipos, textos e timestamps (ids viram seq — 0..200)
    assert len(exp_actions) == len(ref_actions) == 201
    assert [(a["type"], a["text"], a["createdAt"]) for a in exp_actions] == [
        (a["type"], a.get("text") or "", a.get("createdAt")) for a in ref_actions
    ]
    assert [a["id"] for a in exp_actions] == [str(i) for i in range(201)]

    # cards: mesmo conjunto (título, tipo, entry, triggers, flags) — 236
    assert len(exp_state["storyCards"]) == len(ref_state["storyCards"]) == 236
    assert sorted(_card_key(c) for c in exp_state["storyCards"]) == \
        sorted(_card_key(c) for c in ref_state["storyCards"])

    # memórias: mesmos textos, na mesma ordem — 25
    ref_mem_texts = [m["text"] for m in ref_state["memories"] if m.get("text")]
    assert [m["text"] for m in exp_state["memories"]] == ref_mem_texts
    assert len(exp_state["memories"]) == 25

    # campos de metadata: espelho do backup
    assert exp_meta["adventure"]["title"] == ref_meta["adventure"]["title"]
    assert exp_meta["adventure"]["description"] == ref_meta["adventure"]["description"]
    assert exp_meta["adventure"]["memory"] == ref_meta["adventure"]["memory"]
    assert exp_meta["adventure"]["authorsNote"] == ref_meta["adventure"]["authorsNote"]
    assert exp_meta["adventure"]["tags"] == ref_meta["adventure"]["tags"]
    assert exp_meta["adventure"]["thirdPerson"] == ref_meta["adventure"]["thirdPerson"]
    assert exp_state["instructions"]["scenario"] == ref_state["instructions"]["scenario"]
    assert exp_state["storySummary"] == (ref_state.get("storySummary") or "")
    assert exp_meta["totalActionCount"] == 201
