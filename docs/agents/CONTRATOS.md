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
  | 4 ou mais (a VM roda `-np 5`) | 0 | 1 | 2 | 3 |
  | 3 | 0 | 1 | 2 | 2 |
  | 2 | 0 | 1 | 1 | 1 |
  | 1 ou `/props` ilegível | sem `id_slot` | sem `id_slot` | sem `id_slot` | sem `id_slot` |

  Com 1 slot o `id_slot` fica de fora porque um slot inexistente trava o
  llama.cpp. O total fica em cache por 300 s (falha: 30 s) e é relido quando o
  servidor fica inacessível ou uma chamada estoura o prazo.
- Servidor (G2): `-np 5 --kv-unified -cram 0`. O app usa os slots 0 a 3; o 4
  fica livre para clientes sem `id_slot` (open-webui, agente de voz, hermes),
  que normalmente caem nele por ser o menos usado. Cliente externo que cair
  no slot 0 apaga o cache da narrativa: o turno seguinte reprocessa tudo.
- `cache_prompt`: `true` só na narrativa. Auxiliares (todo role ≠ `narrative`)
  vão com `false` (`scheduler.aux_cache_prompt`) até o livelock
  [llama.cpp #28280](https://github.com/ggml-org/llama.cpp/issues/28280) ser
  resolvido. Nunca usam o slot 0. Só isso não basta: com prompt cache no
  servidor (`-cram` > 0) o llama-server salva e restaura slots a cada tarefa
  nova, e o livelock voltou em 2026-10-18 15:42 com `cache_prompt=false`. Por
  isso o `-cram 0` (registro em `docs/vm_runbooks/phase5_slots_record.md`).
- Prompt da narrativa (`context_builder.build_context`): system estável, uma
  user por ação da janela e a user final com tudo o que muda a cada turno
  (cards, memórias, resumo, estado, author's note, âncoras, última ação). O
  Qwen3.6 é híbrido e o llama-server só reaproveita o prefixo a partir de um
  checkpoint; o útil é o do início da última user. O turno seguinte precisa
  repetir byte a byte todas as mensagens anteriores à user final, senão o
  slot 0 reprocessa a história inteira (7–14 s de TTFT). Teste:
  `test_next_turn_repeats_every_message_before_the_final_user`.
- A narrativa vem em SSE; as auxiliares em JSON (`stream=false`).
- Timeouts padrão (prazo total em segundos, sobrescrevíveis em
  `config/settings.json → scheduler.timeouts`): `choices` 15, `chronicle` 45,
  `summary` 60, `image_prompt` 20, `assistant` 90, `meta` 45. `timeout_s`
  explícito vence o padrão; saídas longas (rascunho de cenário) usam
  `scaled_timeout`. A narrativa não tem prazo total: `llm.timeout_seconds` é o
  timeout de leitura entre tokens.
- Erros (todos herdam de `LlmError`, com `code` e `message`):
  - `LlmTimeout` (`code="llm_timeout"`): estourou o prazo. Subclasses:
    `LlmQueueTimeout` (a vez não chegou dentro do prazo) e `LlmStalled`
    (narrativa parada sem fallback possível);
  - `LlmCancelled` (`code="llm_cancelled"`): `cancel` foi setado. Subclasse
    interna `LlmPreempted`: a chamada perdeu o slot para o fallback da narrativa
    e o `chat_completion` repete uma vez;
  - `LlmDegraded` (`code="llm_degraded"`): circuit breaker aberto (G3).
- Scheduler (G3, `backend/app/llm_scheduler.py`): toda chamada pede a vez antes
  de abrir a conexão.
  - Prioridade (menor primeiro): `narrative` 0, que nunca espera; `choices` 1;
    `image_prompt` e `meta` 2; `assistant` 3; `chronicle` 4; `summary` 5.
  - Uma auxiliar por slot fixado e no máximo `scheduler.aux_cap` (3) em voo,
    `aux_cap_during_narrative` (2) com narrativa em curso. Entra a primeira da
    fila que couber.
  - A espera na fila vai até o prazo do role e respeita `cancel`. Estourou:
    `LlmQueueTimeout`, que não conta para o breaker. O prazo do role conta da
    admissão.
  - Breaker: `scheduler.breaker.threshold` (3) falhas de servidor seguidas
    (timeout ou narrativa parada) abrem por `breaker.open_s` (60 s). Aberto, as
    auxiliares levantam `LlmDegraded` na hora; a narrativa nunca é barrada.
    Passado o prazo, a próxima chamada decide: falha reabre, sucesso fecha.
    `llm_degraded` e `llm_recovered` (C2) vão para todas as aventuras com canal
    aberto.
  - Narrativa parada (`scheduler.stall`): a narrativa pede `return_progress`.
    Sem token em 30 s e sem `prompt_progress` em 15 s, o socket cai, o app grava
    `scheduler.stall_hint_path` (`data/llm_stall_hint.json`) para o
    `slot_watchdog` e repete uma vez no slot do `image_prompt`, sem cache,
    tomando o slot da auxiliar que estiver nele.
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
    `EventSource` do navegador não manda header). História de outra pessoa
    responde 404. O `?token=` sai do access log do uvicorn; no nginx, a
    location de `/events` não pode logar a query (G8).
  - Ao conectar: `retry: 3000` e o evento `ready` (`data: {}`) com `id` igual
    ao ponto de partida (o `Last-Event-ID` pedido ou o último id publicado), para
    o navegador já ter um `Last-Event-ID` se cair antes do primeiro evento. Com
    o breaker aberto, `llm_degraded` (sem `id`) vem logo depois.
  - Aventura apagada encerra o stream.
- Formato de cada evento: `id: <int>`, `event: <type>`, `data: <json>`.
- Ids: inteiros crescentes no processo, começando no relógio em ms do boot;
  um `Last-Event-ID` de antes de um reinício não esconde os eventos novos. O
  buffer guarda os últimos 100 eventos por aventura; inscrito lento perde os
  mais antigos (fila de 500).
- `turn_done` sai antes do `done` do stream da geração.
- Tipos:

  | tipo | `data` |
  |---|---|
  | `turn_done` | `{action_id, seq}` |
  | `choices_ready` | `{for_seq, choices: [{type, text}]}` |
  | `choices_failed` | `{for_seq, code, message}` |
  | `card_created` | `{card}` |
  | `card_updated` | `{card}` |
  | `job_queued` | `{job_id, type, ref_id}` |
  | `job_progress` | `{job_id, type, status}` |
  | `portrait_ready` | `{job_id, card_id, image: {id, url, prompt}}` |
  | `backdrop_ready` | `{job_id, ref_id, image: {id, url, prompt}}` |
  | `see_ready` | `{job_id, image}` (`image` no formato `ImageResult`) |
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
- Por isso um `see` pode cair entre a ação do jogador e a resposta. Quem lê o
  turno pula `see` (contexto, choices, chronicle, scene); o undo remove a
  resposta e a ação do jogador e deixa o `see`.

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
PRIORITY_SEE, PRIORITY_PRESENT_PORTRAIT, PRIORITY_BACKDROP, PRIORITY_BULK_PORTRAIT = 0, 1, 2, 3

def enqueue(conn, type_: str, adventure_id: str, ref_id: str | None = None,
            payload: dict | None = None, priority: int = PRIORITY_BACKDROP,
            supersede_key: str | None = None) -> dict: ...
def enqueue_portrait(conn, adventure_id: str, card_id: str, *, replace: bool = False,
                     priority: int = PRIORITY_PRESENT_PORTRAIT) -> dict | None: ...
def cancel(conn, job_id: str) -> dict | None: ...
```

- Tipos: `character_portrait`, `scene_backdrop`, `see_image`. Status:
  `preparing` (só o See, enquanto o LLM escreve o prompt), `pending`,
  `running`, `done`, `error`, `canceled`. Colunas novas (migration 021):
  `priority` e `supersede_key`.
- Prioridades (menor roda antes; no empate, o mais antigo): 0 `see_image`;
  1 retrato de NPC presente (cena, chronicle, botão de regenerar);
  2 `scene_backdrop`; 3 retratos em lote (aventura nova, "gerar todos").
  Com narrativa em curso o worker só tira o See da fila.
- Retrato já na fila (dedupe por card) passa à frente quando o pedido novo
  é mais urgente; nunca vai para trás.
- `supersede_key`: um job novo com a mesma chave cancela os que ainda não
  rodam (`canceled`, `error="superseded"`). O fundo usa `backdrop:<adventure_id>`.
  Job em `running` termina (cancelamento cooperativo).
- Eventos (C2): `job_queued` ao entrar; `job_progress` a cada mudança
  (`pending` depois do `preparing`, `running`, `done`, `error`, `canceled`);
  no `done`, `portrait_ready` / `backdrop_ready` / `see_ready`; no erro do
  See, `see_failed`. `enqueue` publica antes do commit de quem chamou: o
  evento é aviso, a verdade é a linha do job.
- `result` no `done`: retrato `{image, card_id, prompt}`, fundo
  `{image, ref_id}`, See `{image}` (`ImageResult`). No `error`: `{code}`, com
  a mensagem em `error`.
- O render roda fora de transação: o lock de escrita do SQLite não fica preso
  enquanto a GPU pinta.
- Aventura apagada no meio do render: o PNG é apagado e o job termina
  `canceled` (a linha some junto com a aventura), sem evento.
- Reinício do app: retrato e fundo que estavam em `running` voltam para
  `pending`; See em `preparing`, `pending` ou `running` vira `error` com
  `code="interrupted"`.

## C6 See assíncrono

- `POST /api/adventures/{id}/see` com `Prefer: respond-async` (ou `?async=1`)
  responde `202 {"job_id": ...}` em < 300 ms, com
  `Preference-Applied: respond-async` e sem lock de geração. O jogador pode
  agir enquanto a imagem é pintada.
- Sem o header a rota segue síncrona (200 `ImageResult`, também sem lock),
  porque o frontend v0.9.0 espera a imagem na resposta. O frontend passa a
  mandar o header quando usar o SSE (K8).
- 404 da aventura e 503 `image_disabled` saem antes do 202.
- O job nasce em `preparing`: o prompt (chamada `image_prompt`) roda numa
  thread, fora da fila do SD, e só então o job vai para `pending` com
  prioridade 0. Hoje o prompt vem de `routes/images._plan_see`; na K5 ele
  passa a chamar `images.plan_see` (C4).
- O resultado chega por `see_ready` (`{job_id, image}`, com a imagem e a
  action `see`) ou `see_failed` (`{job_id, code, message}`). Fallback por
  polling: `GET /api/jobs/{id}`; job de história de outra pessoa responde 404.
- Cancelar: `DELETE /api/adventures/{id}/jobs/{job_id}` vale em `preparing` e
  `pending`.
- A action `see` entra no fim da aventura (o jogador pode ter agido), presa à
  imagem pelo mesmo seq.
- Recusa do guardrail (cena explícita com pessoa real) chega como `see_failed`
  com `code="real_person_explicit"`.

## C7 Choices

- Pré-geradas no backend logo depois do `done` (role `choices`, slot 1) e
  publicadas em `choices_ready` (`choices` no formato da rota, `[{type, text}]`)
  ou `choices_failed`.
- `POST /api/adventures/{id}/choices` devolve o cache da última ação ou espera
  no máximo `choices.wait_s` (8 s) pela geração em curso (sem geração, começa
  uma); nunca responde 409. Prazo estourado: 503 `llm_timeout`, e o resultado
  ainda sai em `choices_ready`. Falha: 503 com o código da geração.
- O cache é preso a (id, seq) da última ação que não é `see`: `retry`, `undo` e
  edição não reaproveitam opções velhas.
- Ação nova cancela as choices pendentes (e o summary), nunca o chronicle.
  Depois de 2 summaries adiados seguidos, o seguinte termina mesmo com o
  jogador agindo.
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
