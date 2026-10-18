# Contratos entre Grok (plataforma) e Kimi K3 (narrativa, imagens, frontend)

Congelados na Fase 0 (18/10/2026). Mudar um contrato exige PR próprio que
atualize este arquivo, os dois briefings (`GROK.md`, `KIMI_K3.md`) e os testes
que cobrem o contrato. Nenhum dos lados muda a assinatura do outro por conta
própria.

## C1 Roles e slots de LLM (dono: Grok)

```python
# backend/app/llm_client.py
ROLES = ("narrative", "choices", "chronicle", "summary",
         "image_prompt", "assistant", "meta")

def chat_completion(cfg: dict, messages: list[dict], *, role: str = "assistant",
                    timeout_s: float | None = None,
                    cancel: threading.Event | None = None,
                    temperature: float = 0.3, max_tokens: int = 200,
                    stop: list[str] | None = None, transport=None,
                    state: StreamState | None = None) -> str: ...

def stream_chat_completion(cfg: dict, messages: list[dict], *, sampler: dict,
                           stop: list[str], transport=None,
                           state: StreamState | None = None,
                           role: str = "narrative", cancel=None,
                           timeout_s: float | None = None) -> Iterator[str]: ...

def scaled_timeout(cfg: dict, role: str, max_tokens: int) -> float: ...
def recent_calls(limit: int = 50) -> list[dict]: ...   # role, slot, ttft, prompt_n, cache_n, tok/s
def invalidate_slot_cache() -> None: ...
```

- Mapa de slots pelo `total_slots` de `GET /props`:

  | slots no servidor | narrative | choices | chronicle, summary | image_prompt, assistant, meta |
  |---|---|---|---|---|
  | 4 ou mais (`-np 4`) | 0 | 1 | 2 | 3 |
  | 3 | 0 | 1 | 2 | 2 |
  | 2 | 0 | 1 | 1 | 1 |
  | 1 ou `/props` ilegível | sem `id_slot` | sem `id_slot` | sem `id_slot` | sem `id_slot` |

  Com 1 slot o `id_slot` fica de fora porque um slot inexistente trava o
  llama.cpp. O total fica em cache por 300 s (falha: 30 s) e é relido quando o
  servidor fica inacessível.
- `cache_prompt`: `true` só na narrativa. Auxiliares (todo role ≠ `narrative`)
  vão com `false` (`scheduler.aux_cache_prompt`) até o livelock
  [llama.cpp #28280](https://github.com/ggml-org/llama.cpp/issues/28280) ser
  resolvido. Nunca usam o slot 0.
- A narrativa vem em SSE; as auxiliares em JSON (`stream=false`).
- Timeouts padrão (prazo total em segundos, sobrescrevíveis em
  `config/settings.json → scheduler.timeouts`): `choices` 15, `chronicle` 45,
  `summary` 60, `image_prompt` 20, `assistant` 90, `meta` 45. `timeout_s`
  explícito vence o padrão; saídas longas (rascunho de cenário) usam
  `scaled_timeout`. A narrativa não tem prazo total: `llm.timeout_seconds` é o
  timeout de leitura entre tokens.
- Erros (todos herdam de `LlmError`, com `code` e `message`):
  - `LlmTimeout` (`code="llm_timeout"`): estourou o prazo;
  - `LlmCancelled` (`code="llm_cancelled"`): `cancel` foi setado;
  - `LlmDegraded` (`code="llm_degraded"`): circuit breaker aberto (G3).
- `cancel` é um `threading.Event`. Ao ser setado (ou no fim do prazo), a chamada
  derruba o socket com `shutdown` e levanta `LlmCancelled` (ou `LlmTimeout`).
  O socket é capturado no connect TCP, porque sem stream o llama-server só
  manda os headers no fim. O llama-server libera o slot ao perceber o cliente
  desconectado. Um stream encerrado pelo cancelamento nunca conta como
  resposta completa.

## C2 Eventos (dono: Grok)

```python
# backend/app/events.py
def publish(adventure_id: str, type: str, data: dict | None = None) -> int: ...
def subscribe(adventure_id: str, last_event_id: int | None = None) -> Subscription: ...
```

- Endpoint SSE: `GET /api/adventures/{id}/events`.
  - Keep-alive (comentário SSE `: ping`) a cada 15 s.
  - Reconexão com `Last-Event-ID` (header) ou `?last_event_id=` reentrega o que
    ainda estiver no buffer curto da aventura.
  - Autenticação: o mesmo Bearer das outras rotas, ou `?token=` (o
    `EventSource` do navegador não manda header).
- Formato de cada evento: `id: <int>`, `event: <type>`, `data: <json>`.
- Tipos:

  | tipo | `data` |
  |---|---|
  | `turn_done` | `{action_id, seq}` |
  | `choices_ready` | `{for_seq, choices: [str]}` |
  | `choices_failed` | `{for_seq, code, message}` |
  | `card_created` | `{card}` |
  | `card_updated` | `{card}` |
  | `job_queued` | `{job_id, type, ref_id}` |
  | `job_progress` | `{job_id, type, status}` |
  | `portrait_ready` | `{card_id, image_id}` |
  | `backdrop_ready` | `{image_id}` |
  | `see_ready` | `{job_id, image}` |
  | `see_failed` | `{job_id, code, message}` |
  | `llm_degraded` | `{reason, until}` |
  | `llm_recovered` | `{}` |
  | `meta_result` | `{command, ok, message, card_id?, job_id?}` |

## C3 Ganchos no game_engine (Grok cria, Kimi implementa)

```python
# backend/app/meta_commands.py (Kimi)
@dataclass
class MetaCommand:
    kind: str            # "card" | "portrait" | "scene"
    name: str | None     # alvo ("Clara"); None em /cena
    raw: str             # texto original

def match(text: str, type_: str) -> MetaCommand | None: ...
def execute(adventure_id: str, cmd: MetaCommand,
            transport=None) -> Iterator[dict]: ...   # eventos {"event", "data"}

# backend/app/output_sanitizer.py (Kimi)
def clean_narration(text: str, truncated: bool) -> str: ...
```

- Antes do narrador (e antes do lock de geração e do `input_modifier`),
  `game_engine.handle_action` chama `meta_commands.match` com o texto cru.
  Se casar, repassa os eventos de `execute` (que termina com `meta_done`) e o
  narrador não roda. Nada é gravado como prosa. Exceção em `execute` vira um
  evento `error`.
- Depois do stream e antes de gravar (e antes do `output_modifier`),
  `game_engine` chama `output_sanitizer.clean_narration(text, truncated)`. O
  evento `done` leva o texto já limpo.
- Entregues na G1: `match` nunca casa e `clean_narration` só corta a frase
  incompleta quando `truncated` (`output_sanitizer.cut_incomplete_sentence`).
  A K1/K2 trocam a implementação sem mudar as assinaturas.
- Quem grava ação nova usa `game_engine.appending(adventure_id)` +
  `game_engine.append_action(conn, adventure_id, type_, text)`: o seq é
  calculado e commitado sob um lock, porque See, jogador e narrador gravam em
  paralelo.

## C4 ImagePlan (dono: Kimi; renderização: Grok)

```python
# backend/app/image_plan.py
@dataclass
class FaceRef:
    image_path: str
    weight: float = 0.8
    method: str = "auto"          # "auto" | "ip_adapter_faceid" | "instantid"

@dataclass
class ImagePlan:
    kind: str                     # "portrait" | "see" | "backdrop"
    prompt: str                   # sempre em inglês
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
```

- Planejadores (Kimi): `npc_assets.plan_portrait(conn, adventure, card, img_cfg)`,
  `images.plan_see(...)`, `scene.plan_backdrop(...)`. Devolvem `ImagePlan`.
- Renderização (Grok): `image_client.render(img_cfg, plan, adventure_id,
  image_id) -> dict` com `{path, width, height, seed, ms}`. Monta o payload do
  provider (Forge/A1111), aplica preset de tamanho e as `face_refs` via ControlNet.
- `allow_explicit=False` sempre que houver pessoa real no quadro (C8).

## C5 Jobs (dono: Grok)

```python
# backend/app/jobs.py
def enqueue(conn, type_: str, adventure_id: str, ref_id: str | None = None,
            payload: dict | None = None, priority: int = 2,
            supersede_key: str | None = None) -> dict: ...
```

- Prioridades (menor roda antes): 0 `see_image`; 1 retrato de NPC presente;
  2 `scene_backdrop`; 3 retratos em lote.
- `supersede_key`: um job novo com a mesma chave cancela os pendentes antigos
  (ex.: `backdrop:<adventure_id>`).
- Cada mudança de estado publica `job_queued` / `job_progress` (C2).

## C6 See assíncrono

- `POST /api/adventures/{id}/see` responde `202 {"job_id": ...}` em < 300 ms,
  sem lock de geração. O jogador pode agir enquanto a imagem é pintada.
- O prompt (chamada `image_prompt`) roda dentro do job, antes da fila do SD.
- O resultado chega por `see_ready` (com a imagem e a action `see`) ou
  `see_failed`. Fallback por polling: `GET /api/jobs/{id}`.
- Recusa do guardrail (cena explícita com pessoa real) chega como `see_failed`
  com `code="real_person_explicit"`.

## C7 Choices

- Pré-geradas no backend logo depois do `done` (role `choices`, slot 1) e
  publicadas em `choices_ready`.
- `POST /api/adventures/{id}/choices` devolve o cache da última ação ou espera
  no máximo 8 s pela geração em curso; nunca responde 409.
- Ação nova cancela as choices pendentes (e o summary), nunca o chronicle.
- Idioma: `assistant.adventure_language(adventure) -> "pt-BR" | "en" | ...`
  (helper do Kimi).

## C8 Ficha de pessoa real (dono: Kimi)

- Campos novos em `sheet` do card: `real_person: bool`, `real_name: str`,
  `wiki_title: str`, `likeness: {descriptor, reference_image_path, source_url,
  license, verified_by_user}`.
- Endpoints:
  - `POST /api/adventures/{id}/cards/{card_id}/likeness/resolve`: busca na
    Wikipedia (REST `page/summary`), baixa a imagem principal para
    `data/references/`, descreve a foto com a visão do llama-server e grava o
    `likeness`.
  - `PUT /api/adventures/{id}/cards/{card_id}/reference`: upload manual
    (prioridade sobre a Wikipedia).
  - `DELETE /api/adventures/{id}/cards/{card_id}/reference`.
- Guardrail obrigatório (testado): pessoa real no quadro força
  `allow_explicit=False`; cena explícita com pessoa real tira a pessoa do quadro
  ou recusa o See; menores nunca.

## Arquivos compartilhados

`backend/app/db.py`, `backend/app/schemas.py`, `backend/app/main.py` (só registro
de routers) e `backend/schema.sql` são append-only: cada lado escreve numa seção
própria com comentário de cabeçalho. Migrations: Kimi 010–019, Grok 020–029.
