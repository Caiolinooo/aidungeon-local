# Aidungeon Local — UI unificada, criação de aventura confiável e peso de jogo

## Context

O usuário pediu três frentes (PT-BR): (1) a interface não agrada visualmente; (2) as instruções e funções da IA são imprecisas e "inúteis" — o jogo não tem peso nem dá vontade de jogar; (3) quando a aventura é gerada por IA, muitas vezes ela simplesmente não é criada.

A auditoria do código (backend + frontend, leitura direta e 3 scouts) fechou a causa de cada uma:

- **Visual**: existe um design system (variáveis de tema em `frontend/src/index.css`), mas **340 classes `zinc-*` hardcoded em 23 arquivos** ignoram esse sistema — o tema Parchment (claro) fica com miolo preto. Sete classes de animação do plugin `tailwindcss-animate` são usadas, mas esse plugin não está instalado (`tailwind.config.js` tem `plugins: []`), então essas animações não existem. As fontes declaradas no config nunca carregam. A preferência de escala/fonte do leitor não afeta o parágrafo salvo. A UI mistura português e inglês e `frontend/index.html` declara `lang="en"`.
- **IA "inútil"**: as regras do sistema (a tabela do ruleset, copiada para `adventures.character_rules`) **nunca chegam ao prompt do narrador** — `context_builder.build_context` só injeta o bloco de resolução. As consequências do Modo Vida são calculadas (`sim/life.py` devolve `reactions`) e **descartadas**: o frontend nunca lê `rules.time.reactions`. As necessidades decaem ~0,3 por turno (o intent usa 5 minutos por ação), então nada muda na prática. As escolhas sugeridas pedem **"Texts in English"** (`choices_service.py:33`). E o narrador pode escrever pelas decisões do jogador, vazar a rolagem ou trocar de idioma sem que nada seja detectado (`output_sanitizer.clean_narration` só limpa marcações e corta frase incompleta).
- **Aventura não criada**: três caminhos de exceção em `backend/app/assistant.py` transformam falha *parcial* (uma ficha, um campo, um card) em falha *total* (503) e o rascunho inteiro é perdido. Depois, `POST /api/adventures` grava tudo numa única transação: se qualquer passo de jobs/auditoria levantar, `db.session()` dá rollback e a aventura não existe. O frontend grava o cenário antes da aventura, deixando cenário órfão duplicado a cada tentativa, e ainda pode criar a aventura sem ficha (o `characterTitle` devolve vazio e o jogo abre bloqueado em "Crie seu personagem").

Este plano fecha as três frentes sem trocar o modelo de LLM e sem adicionar dependências.

## Approach

Ordem obrigatória: Fase 0 → Fase 1 → Fase 2 → Fase 3 → Fase 4 → Fase 5. As fases 0 e 1 não se tocam; 3 e 4 também não. Cada fase termina com o que está em **Verification** verde.

---

### Fase 0 — Sistema de tokens (frontend, sem backend)

O objetivo da fase é que **nenhuma cor da interfacedependa de uma paleta fixa**: as 340 classes `zinc-*` passam a ler as variáveis de tema, e o tema Parchment deixa de ter miolo preto.

**0.1 — `frontend/src/index.css:6-58`: variáveis de tema como trincas RGB**

Hoje cada tema define 13 variáveis hexadecimais. O Tailwind 3 **descarta o modificador de opacidade** (`/50`, `/80`, `/95`) quando a cor é um `var()` cru — e o código usa opacidade em `bg-red-950/95`, `border-accent-pri/40`, `bg-elevated/80` etc. Por isso as variáveis passam a ser trincas RGB (`R G B`), e o Tailwind consome `rgb(var(--x) / <alpha-value>)`.

Manter **exatamente** os seletores atuais: `:root, [data-theme="obsidian"], .theme-obsidian` / `[data-theme="parchment"], .theme-parchment` / `[data-theme="oled"], .theme-oled`. Cada bloco passa a declarar as rampas + as variáveis semânticas:

Rampas (valores `R G B` exatos):

```
obsidian (e oled, que só muda a rampa neutra):
  --n-950: 9 9 11      --n-900: 24 24 27     --n-800: 39 39 42     --n-700: 63 63 70
  --n-600: 82 82 91    --n-500: 113 113 122  --n-400: 161 161 170  --n-300: 212 212 216
  --n-200: 228 228 231 --n-100: 244 244 245
  --accent-950: 2 44 34    --accent-900: 6 78 59    --accent-800: 6 95 70    --accent-700: 4 120 87
  --accent-600: 5 150 105  --accent-500: 16 185 129 --accent-400: 52 211 153 --accent-300: 110 231 183
  --accent-200: 167 243 208 --accent-100: 209 250 229 --accent-50: 236 253 245
  --danger-950: 69 10 10   --danger-900: 127 29 29  --danger-800: 153 27 27  --danger-700: 185 28 28
  --danger-600: 220 38 38  --danger-500: 239 68 68  --danger-400: 248 113 113 --danger-300: 252 165 165
  --danger-200: 254 202 202 --danger-100: 254 226 226 --danger-50: 254 242 242
  --warn-950: 69 26 3      --warn-900: 120 53 15    --warn-800: 146 64 14    --warn-700: 180 83 9
  --warn-600: 217 119 6    --warn-500: 245 158 11   --warn-400: 251 191 36   --warn-300: 252 211 77
  --warn-200: 253 230 138  --warn-100: 254 243 199  --warn-50: 255 251 235

parchment (rampa neutra invertida — claro embaixo, escuro em cima):
  --n-950: 244 233 213 --n-900: 250 243 228 --n-800: 255 253 248 --n-700: 216 197 162
  --n-600: 160 138 104 --n-500: 138 118 89  --n-400: 107 90 67   --n-300: 84 69 47
  --n-200: 59 47 30    --n-100: 42 33 20
  --accent-50: 236 253 245 --accent-100: 2 44 34   --accent-200: 6 78 59   --accent-300: 6 95 70
  --accent-400: 4 120 87   --accent-500: 5 150 105 --accent-600: 6 95 70   --accent-700: 4 120 87
  --accent-800: 6 78 59    --accent-900: 209 250 229 --accent-950: 236 253 245
  --danger-50: 255 247 247 --danger-100: 127 29 29 --danger-200: 153 27 27 --danger-300: 185 28 28
  --danger-400: 192 37 37  --danger-500: 220 38 38 --danger-600: 185 28 28 --danger-700: 185 28 28
  --danger-800: 159 18 57  --danger-900: 136 19 55  --danger-950: 255 241 242
  --warn-50: 255 251 235   --warn-100: 120 53 15   --warn-200: 146 64 14   --warn-300: 180 83 9
  --warn-400: 194 65 12    --warn-500: 180 83 9   --warn-600: 146 64 14   --warn-700: 180 83 9
  --warn-800: 146 64 14    --warn-900: 253 246 227 --warn-950: 254 243 199

oled (rampa neutra própria):
  --n-950: 0 0 0      --n-900: 10 10 10   --n-800: 18 18 18   --n-700: 38 38 38
  --n-600: 107 107 116 --n-500: 139 139 148 --n-400: 163 163 163 --n-300: 196 196 196
  --n-200: 224 224 224 --n-100: 245 245 245
  (accent/danger/warn idênticas à obsidian)
```

Variáveis semânticas (nomes novos com sufixo `-rgb`; os nomes antigos saem):

```
obsidian / oled:
  --bg-canvas-rgb: var(--n-950);      --bg-surface-rgb: var(--n-900);
  --bg-elevated-rgb: var(--n-800);    --bg-input-rgb: var(--n-900);
  --border-subtle-rgb: var(--n-700);  --border-focus-rgb: var(--accent-500);
  --text-primary-rgb: var(--n-100);   --text-secondary-rgb: var(--n-400);
  --text-muted-rgb: var(--n-500);     --accent-primary-rgb: var(--accent-700);
  --accent-hover-rgb: var(--accent-600); --accent-gold-rgb: var(--warn-500);
  --info-text-rgb: 125 211 252;
parchment:
  --bg-canvas-rgb: var(--n-950);      --bg-surface-rgb: var(--n-900);
  --bg-elevated-rgb: var(--n-800);    --bg-input-rgb: var(--n-900);
  --border-subtle-rgb: var(--n-700);  --border-focus-rgb: var(--accent-700);
  --text-primary-rgb: var(--n-100);   --text-secondary-rgb: var(--n-400);
  --text-muted-rgb: var(--n-500);     --accent-primary-rgb: var(--accent-700);
  --accent-hover-rgb: var(--accent-600); --accent-gold-rgb: var(--warn-500);
  --info-text-rgb: 3 105 161;
```

`color-scheme: dark` (obsidian, oled) e `light` (parchment) permanecem.

Consequência intencional: o acento passa a ser **verde nos três temas** (parchment deixa de ser âmbar, oled deixa de ser azul). Uma cor de acento por tema, sem exceções.

**0.2 — `frontend/tailwind.config.js:1-30`: a paleta passa a ser a rampa**

Substituir o arquivo inteiro por:

```js
import colors from 'tailwindcss/colors'

const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]
const ramp = (prefix) =>
  Object.fromEntries(STEPS.map((s) => [s, `rgb(var(--${prefix}-${s}-rgb) / <alpha-value>)`]))

const base = { ...colors, transparent: 'transparent', current: 'currentColor', inherit: 'inherit' }

export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    colors: {
      ...base,
      zinc: ramp('n'),
      emerald: ramp('accent'),
      red: ramp('danger'),
      amber: ramp('warn'),
    },
    extend: {
      colors: {
        canvas: 'rgb(var(--bg-canvas-rgb) / <alpha-value>)',
        surface: 'rgb(var(--bg-surface-rgb) / <alpha-value>)',
        elevated: 'rgb(var(--bg-elevated-rgb) / <alpha-value>)',
        'surface-input': 'rgb(var(--bg-input-rgb) / <alpha-value>)',
        subtle: 'rgb(var(--border-subtle-rgb) / <alpha-value>)',
        'accent-pri': 'rgb(var(--accent-primary-rgb) / <alpha-value>)',
        'accent-hov': 'rgb(var(--accent-hover-rgb) / <alpha-value>)',
        'accent-gld': 'rgb(var(--accent-gold-rgb) / <alpha-value>)',
        info: 'rgb(var(--info-text-rgb) / <alpha-value>)',
      },
      textColor: {
        primary: 'rgb(var(--text-primary-rgb) / <alpha-value>)',
        secondary: 'rgb(var(--text-secondary-rgb) / <alpha-value>)',
        muted: 'rgb(var(--text-muted-rgb) / <alpha-value>)',
      },
      fontFamily: {
        prose: ['Georgia', 'Cambria', 'Times New Roman', 'serif'],
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Menlo', 'monospace'],
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-in-top': { from: { opacity: '0', transform: 'translateY(-8px)' }, to: { opacity: '1', transform: 'none' } },
      },
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
        'slide-in-top': 'slide-in-top 220ms ease-out',
      },
    },
  },
  plugins: [],
}
```

`theme.colors` (não `theme.extend.colors`) é o que substitui a paleta padrão e faz `zinc-500` resolver para a rampa; `theme.extend.colors` continua se somando por cima (canvas/surface/…). As 340 ocorrências de `zinc-*` **não precisam ser editadas** — é esse o objetivo.

**0.3 — `frontend/src/index.css`: consumidores das variáveis**

Trocar as leituras das variáveis antigas (o `var(--bg-canvas)` cru → `rgb(var(--bg-canvas-rgb))`):

- `html, body, #root { background-color: rgb(var(--bg-canvas-rgb)); color: rgb(var(--text-primary-rgb)); }` (hoje `index.css:59-66`)
- `body { ... font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }` — hoje `index.css:71` ignora a `fontFamily` do config (passa a usar a mesma pilha declared em `theme.fontFamily.sans`).
- `.streaming-cursor::after { color: rgb(var(--accent-primary-rgb)); }` (`index.css:100`)
- `::-webkit-scrollbar-thumb { background: rgb(var(--border-subtle-rgb)); }` (`index.css:132`)
- Adicionar os `@keyframes fade-in` e `@keyframes slide-in-top` (espelhando o config) junto dos keyframes existentes (`blink`, `scene-flash`, `npc-step`).
- Remover os nomes antigos (`--bg-canvas`, `--bg-surface`, `--bg-elevated`, `--bg-input`, `--border-subtle`, `--border-focus`, `--text-primary`, `--text-secondary`, `--text-muted`, `--accent-primary`, `--accent-hover`, `--accent-gold`). Depois, `grep -rn "var(--bg-\|var(--text-\|var(--accent-\|var(--border-" frontend/src` deve retornar **apenas** `var(--app-height)` (usado em `Modal.jsx:43`, que permanece).

**0.4 — `frontend/src/index.css:59-66`**: `--app-height` continua igual.

**0.5 — Classes de animação inexistentes**

| Onde | Antes | Depois |
|---|---|---|
| `components/drawer/Drawer.jsx:141` | `backdrop-blur-xs animate-in fade-in` | `backdrop-blur-sm animate-fade-in` |
| `components/ReaderPreferencesModal.jsx:12` | `animate-in fade-in` | `animate-fade-in` |
| `components/drawer/CardsTab.jsx:420` | `animate-in slide-in-from-top-2` | `animate-slide-in-top` |
| `pages/Play.jsx:1134` | `animate-in slide-in-from-top-2` | `animate-slide-in-top` |
| `pages/Play.jsx:1263,1281,1300` | `animate-in fade-in` | `animate-fade-in` |

`backdrop-blur-xs` não existe no Tailwind 3.4.19 (a escala é `sm` 4px, `DEFAULT` 8px, `md`, `lg`, `xl`, `2xl`, `3xl`) — usar `backdrop-blur-sm`.

**0.6 — Famílias de cor fora da rampa (11 ocorrências)**

| Onde | Antes | Depois |
|---|---|---|
| `pages/Play.jsx:120` (texto do passo de rolagem) | `text-sky-300/90` | `text-info` |
| `components/drawer/SimTab.jsx:436` (mesmo bloco) | `text-sky-300` | `text-info` |
| `components/SceneBackdrop.jsx:17` (humor `sad`) | `border-sky-400` | `border-info` |
| `components/SceneBackdrop.jsx:19` (humor `fear`) | `border-violet-400` | `border-accent-pri` |
| `components/SceneBackdrop.jsx:20` (humor `love`) | `border-rose-400` | `border-accent-gld` |
| `components/ReaderPreferencesModal.jsx:82-83` (swatch do tema) | `bg-sky-400`, `text-sky-400` | `bg-accent-pri`, `text-accent-pri` |
| `components/drawer/SimTab.jsx:165` | `bg-rose-600` | `bg-danger-600` |
| `components/drawer/SimTab.jsx:181,201` | `text-rose-300` | `text-danger-300` |
| `components/drawer/SimTab.jsx:288` | `bg-rose-600`, `bg-amber-500` | `bg-danger-600`, `bg-warn-500` |
| `components/drawer/SimTab.jsx:366` | `bg-rose-700/80` | `bg-danger-700/80` |
| `components/drawer/CardsTab.jsx:42` (card lesionado) | `bg-orange-950/80 text-orange-200 border-orange-800` | `bg-warn-950/80 text-warn-200 border-warn-800` |

**0.7 — Toast e banner passam a seguir o tema (sem renomear classes)**

`components/Toast.jsx:34-36` já usa `border-red-800 bg-red-950/95 text-red-200` / `border-emerald-800 bg-emerald-950/95 text-emerald-200`: com a rampa, obsidian continua vermelho-escuro e parchment vira rosa-claro com texto vermelho-escuro — legível nos dois. **Não renomear as classes.** Adicionar `role="status"` e `aria-live="polite"` no container dos toasts (`Toast.jsx:29`) para que a falha da IA seja anunciada a leitores de tela.

`pages/Play.jsx:1401` (banner "Crie seu personagem", `bg-amber-950 text-amber-200`) segue a mesma lógica — sem renomear.

**0.8 — Alvos de toque e o dock**

- `components/drawer/Drawer.jsx:65-72`: `min-h-9` (36px) → `min-h-11` (44px).
- `pages/Play.jsx:229-235`: o handler de `resize` força `setIsInspectorDocked(true)` em ≥1024px e anula o botão de esconder (`Play.jsx:1183-1195`). Remover a atribuição: o `resize` passa a atualizar **apenas** a largura (`isWide`), e o estado do dock passa a ser decidido só pelo usuário (botão de esconder/mostrar). Em `<1024` o inspetor abre como Drawer; em `≥1024` ele é dockado apenas se `isInspectorDocked` for true.

**0.9 — A preferência do leitor passa a valer no texto salvo**

- `components/FeedItem.jsx:112-114`: o parágrafo de prosa usa `text-[1.05rem] leading-8`, que **sobrescreve** o `fontSize`/`lineHeight` inline do wrapper (`pages/Play.jsx:1229-1232`). Remover `text-[1.05rem] leading-8` e deixar `whitespace-pre-wrap font-prose` (+ `italic text-secondary` para `type === 'story'`). O wrapper controla o tamanho.
- `pages/Play.jsx:1268-1269` (parágrafo de streaming): usar **as mesmas** classes do item salvo (`whitespace-pre-wrap font-prose` + `italic text-secondary` quando aplicável), sem tamanho arbitrário.
- `pages/Play.jsx:981-985`: o mapa da preferência de fonte passa a usar as famílias do config — `serif → font-prose`, `sans → font-sans`, `mono → font-mono` (hoje `serif → font-serif`, que é outra pilha).
- Nenhuma fonte web é baixada: o app é self-hosted e precisa funcionar offline. `frontend/index.html` continua sem `<link>` de CDN.

**0.10 — `frontend/index.html`**

- `lang="en"` → `lang="pt-BR"`.
- Remover `class="dark"` do `<html>` (não existe nenhuma variante `dark:` no código — zero ocorrências — e o `darkMode` do Tailwind é o padrão `media`).
- `meta name="description"`: texto em PT-BR ("AI Dungeon Local — contos interativos com LLMs locais").

---

### Fase 1 — A aventura é sempre criada (backend)

**1.1 — `backend/app/assistant.py:773-796` (`deepen_characters`): uma ficha que falha não derruba o rascunho**

O `except` captura só `llm_client.LlmError`, mas `app/npc_assets.py` levanta `GameError(503, "generation_failed", "LLM returned an unparseable response; try again")` (`npc_assets.py:82-85` e `:210-216`) — que escapa, mata a geração inteira e descarta título, abertura e cards. Capturar também `GameError` (e `ValueError`): o card segue sem `sheet`, e `jobs.enqueue_incomplete_sheets` preenche depois. O docstring do função já promete esse comportamento — o código é que não cumpre.

**1.2 — `backend/app/assistant.py:711-717` + `:873-898` (`regenerate_field`): campo vazio não derruba o rascunho**

Quando `opening_text` ou `description` vêm vazios, `regenerate_field` é chamado e pode levantar `GameError` 503 / `ValueError("resposta com JSON incompleto")`, o que descarta um rascunho bom. Envolver a chamada em `try/except (GameError, ValueError)` e manter o campo vazio (a tela mostra o campo vazio e o usuário pode regerar só aquele campo).

**1.3 — `backend/app/assistant.py:719-729` (`_draft_cards`): cenário sem cards ainda é cenário**

Se as duas tentativas de `_draft_cards` falharem, hoje a rota devolve 503 "resposta sem 'story_cards'". Mudar: o cenário é mantido **sem** story cards (a tela mostra "0 fichas" e o usuário gera fichas individualmente). Só a ausência de `title` continua sendo falha fatal (`assistant.py:685-702`).

**1.4 — `backend/app/assistant.py`: a mensagem de erro precisa ser acionável**

`_generation_failed(detail)` mantém o `code` `"generation_failed"` e a `message` `"LLM returned an unparseable response; try again"` (contrato em `docs/openapi.yaml`), mas o `detail` real ("resposta sem 'title'", "resposta sem 'story_cards'") vai para o log **e** para a resposta, no campo `error.hint` — additive, não quebra o cliente existente. `routes/assistant.py` (as seis rotas) já converte `LlmError` → `GameError(503, exc.code, exc.message)`; acrescentar `exc.hint` quando existir.

**1.5 — `backend/app/routes/adventures.py:92-160`: a criação não pode dar rollback por causa de um job**

Hoje tudo (aventura + action + cards + `jobs.enqueue_all_portraits` + `_complete_npc_sheets` + `jobs.enqueue_incomplete_sheets` + `consistency.audit`) está dentro de **um** `with db.session()`, e `db.session()` (`backend/app/db.py:54-65`) dá rollback em qualquer exceção → a aventura inteira desaparece.

Reestruturar `create_adventure`:

```python
with db.session() as conn:
    adv = db.insert_adventure(...)
    db.insert_action(conn, adv["id"], 0, "start", sc["opening_text"])
    for card in sc["story_cards"]:
        db.insert_card(conn, adv["id"], ...)
# fora da transação: enfileirar os jobs (falha aqui não desfaz a aventura)
try:
    with db.session() as conn:
        jobs.enqueue_all_portraits(conn, adv["id"])
        _complete_npc_sheets(conn, adv["id"])
        jobs.enqueue_incomplete_sheets(conn, adv["id"])
except Exception as exc:
    log.warning("adventures: jobs de criação falharam (aventura mantida): %s", exc)
```

`consistency.audit` só lê → pode ficar na primeira transação (o relatório entra em `state["consistency"]`).

**1.6 — `backend/app/routes/scenarios.py:14-29`: `POST /api/scenarios` exige autenticação**

A rota não tem `Depends(auth.require_auth)` — qualquer um pode criar cenário. Adicionar `info: auth.AuthInfo = Depends(auth.require_auth)` (mesmo padrão de `routes/adventures.py`), usando `info.user` como dono do cenário. Ajustar `tests/test_v9_scenarios.py` (que hoje chama a rota sem token).

**1.7 — Novo endpoint atômico `POST /api/adventures/from-draft`**

O frontend hoje faz 4 chamadas encadeadas (`createScenario` → `createAdventure` → `saveCharacter` → `applyRuleset`); qualquer uma que falhe deixa cenário órfão ou aventura sem ficha. Criar **um** endpoint que faz tudo numa transação.

- Schema novo em `backend/app/schemas.py` (junto de `ScenarioCreate:65` e `PlayerCharacterUpsert:136`):

```python
class AdventureFromDraft(BaseModel):
    draft: ScenarioCreate
    character: PlayerCharacterUpsert | None = None
    ruleset_id: str | None = None
```

- Rota nova em `backend/app/routes/adventures.py`, `status_code=201`, `Depends(auth.require_auth)`:
  1. `db.insert_scenario(conn, **draft)` — o cenário nasce junto da aventura, então nunca fica órfão.
  2. `db.insert_adventure(conn, scenario_id=..., owner=info.user, ...)` herdando os campos do draft.
  3. `db.insert_action(conn, adv["id"], 0, "start", draft["opening_text"])`.
  4. `db.insert_card` para cada `story_cards` do draft.
  5. Se `character` vier: `db.upsert_player_character(conn, adv["id"], info.user, title=..., sheet=...)`.
  6. Se `ruleset_id` vier: aplicar o ruleset como `routes/rulesets.py:106` faz (`db.update_adventure(..., character_rules=merged[:_RULES_CAP])` + `packs.pack_for_ruleset` + `sim_state` + `sim_rules.ensure_actor`).
  7. Commitar. **Depois** enfileirar os jobs (mesmo `try/except` de 1.5) e rodar `consistency.audit`.
  8. Devolver `db.adventure_state(conn, adv["id"])` com `state["consistency"]`.

- `POST /api/adventures` (existente) continua valendo, com a correção de 1.5 — é usado por testes e pelo importador.

**1.8 — Testes (backend)**

Novo arquivo `tests/test_adventure_from_draft.py`:
- `POST /api/adventures/from-draft` com draft + personagem + ruleset → 201; `GET /api/adventures/{id}` → 200 com a ficha do personagem e `character_rules` preenchido; o cenário não fica órfão (a aventura referencia `scenario_id`).
- Sem token → 401.

Novo arquivo `tests/test_assistant_resilience.py`:
- LLM mock devolve JSON inválido ao pedir a ficha de personagem (`npc_assets.build_sheet`) → `POST /api/assistant/generate` responde **200** com o rascunho íntegro (title + story_cards presentes).
- LLM mock devolve `opening_text` vazio e falha no `regenerate_field` → 200 com o rascunho, `opening_text` vazio.
- LLM mock falha em todas as tentativas de card → 200 com o cenário sem `story_cards` (não 503).

Ajustar `tests/test_v9_scenarios.py` para a autenticação nova.

Em `tests/test_adventure_mgmt.py`: `jobs.enqueue_all_portraits` monkeypatchado para levantar `RuntimeError` → `POST /api/adventures` ainda devolve 201 e `GET /api/adventures/{id}` devolve 200.

---

### Fase 2 — Modal de criação (frontend)

**2.1 — `components/GenerateAdventureModal.jsx:19-27` (`characterTitle`): o herói sempre vira ficha**

Hoje a função devolve `''` quando não acha um nome curto → `api.saveCharacter` nunca roda (`GenerateAdventureModal.jsx:294-307`) → a aventura abre sem ficha e `pages/Play.jsx:1400-1410` bloqueia o jogo com "Crie seu personagem". Nova regra:

```js
function characterTitle(name, description) {
  const n = (name || '').trim()
  if (n) return n.slice(0, 80)
  const first = (description || '').split('\n')[0].trim()
  if (first && first.length <= 60 && !/[,.;:!?]/.test(first)) return first.slice(0, 80)
  return 'Protagonista'
}
```

**2.2 — `components/GenerateAdventureModal.jsx:271-317` (`create`): uma chamada só, sem órfãos**

Substituir a sequência `createScenario` → `createAdventure` → `saveCharacter` → `applyRuleset` por `api.createAdventureFromDraft({ draft, character, ruleset_id })` (novo helper em `api.js`, junto de `createAdventure:158`). Em sucesso: `navigate('/play/' + adv.id)`. Em falha: o rascunho **permanece na tela** (nada é gravado, nada é duplicado) e o erro aparece no painel inline de 2.4. Remover do fluxo a chamada a `createScenario` (só o endpoint novo grava).

**2.3 — `frontend/src/api.js`: preservar o código do erro**

`request()` (`api.js:52-81`) hoje lança `new Error(message)` e perde o `code` do corpo `{"error":{"code","message"}}`. Anexar `err.code = data?.error?.code` e `err.status = res.status` ao `Error` — sem breaking change (as mensagens continuam iguais). Adicionar:

```js
export const createAdventureFromDraft = (payload) =>
  request('/api/adventures/from-draft', { method: 'POST', body: JSON.stringify(payload) })
```

**2.4 — Falha visível, persistente e acionável**

`generate()` (`GenerateAdventureModal.jsx:89-147`) e `create()` (`:271-317`) hoje só fazem `toast(e.message)` — o toast some em 6 s (`components/Toast.jsx:22`) e o rascunho parece sucesso. Substituir por um **painel inline** dentro do modal (`border-danger-800 bg-danger-950/60 p-3 rounded-lg text-sm`), com a mensagem e o botão "Tentar de novo". Mensagens por `err.code`:

| code | mensagem |
|---|---|
| `generation_failed` | "A IA devolveu uma resposta que não deu para interpretar. O rascunho foi mantido — tente gerar de novo." |
| `validation_error` | mensagem do servidor |
| `timeout` / erro de rede | "O servidor de IA não respondeu a tempo. O rascunho foi mantido — tente de novo." |
| ausente | `err.message` |

O toast continua para confirmações curtas ("Cópia", "Fechado").

**2.5 — `window.prompt` sai (não funciona no celular)**

`regenerate(field)` (`GenerateAdventureModal.jsx:152`) e o enriquecimento de card (`enrichCard`) usam `window.prompt`. Trocar por um campo inline: estado `refineField` (o campo/cartão em refinamento) + `refineText` (o texto digitado) + um botão "Refinar" que abre o campo ao lado do item; "Confirmar" dispara a chamada e "Cancelar" fecha. Nenhum `window.prompt`/`window.confirm` novo.

**2.6 — Estimativa honesta**

O modal promete "cerca de um minuto" (`GenerateAdventureModal.jsx:604-607`), mas com 5 fichas + 5 fichas profundas e o modelo IQ2_M local o Leva 2–6 minutos. Trocar o texto por "2 a 6 minutos com o modelo local" e manter o indicador de etapa (`stageFor`, `GenerateAdventureModal.jsx:33-40`).

**2.7 — Botão "Criar" exige título de verdade**

`create()` (`:272-274`) já valida `draft.title`; o problema é que a falha é só um toast. Passar a bloquear o botão ("Criar aventura") quando o título estiver vazio, com a mensagem de ajuda **no painel inline**.

---

### Fase 3 — Peso de jogo: o motor (backend)

**3.1 — As regras do sistema chegam ao narrador**

`adventures.character_rules` (a tabela do ruleset, copiada por `routes/rulesets.py:106`) **nunca** entra no prompt: `context_builder.build_context` (`backend/app/context_builder.py:535`) só injeta o bloco de resolução. Mudar em `build_context`:

- Ler `adventure.get("character_rules")`; se não-vazio, acrescentar ao **system message**, logo após o bloco `[Plot]`, um bloco:

```
[Rules]
<character_rules>
```

- O bloco passa pelo mesmo `_fit_budget` dos outros (chave nova `character_rules`, **default 900** tokens, em `config/settings.json` no bloco `budgets`; `FIXED_BUDGET_KEYS` em `context_builder.py:222` ganha a chave).
- Se `character_rules` estiver vazio, nenhum bloco é emitido (sem custo de contexto).
- O texto das regras é do próprio usuário/sistema de regras — entra como cânone, no system, ao lado do Plot.

**3.2 — `third_person` passa a valer**

`context_builder.build_context` nunca lê `adventure["third_person"]` → a opção de POV do modal (`GenerateAdventureModal.jsx`) não faz nada. Quando `third_person` for true, acrescentar ao system message (logo após `BASE_SYSTEM_PROMPT`):

```
Narrate in the third person. Refer to the protagonist by name; never as "you".
```

**3.3 — O Modo Vida cobra de verdade**

Três mudanças, todas em código, sem tocar nas taxas existentes (as taxas estão equilibradas por `tests/test_sim_life.py`):

1. `backend/app/sim/intent.py:70`: `minutes = _as_int(data.get("minutes"), 5)` → default **30**. Um turno passa a ser ~30 min de jogo (o modelo continua mandando o valor quando sabe). Com as taxas atuais (`fome -4/h`, `energia -3/h`, `bexiga -5/h`), um turno custa ~2 pontos de fome, 1,5 de energia e 2,5 de bexiga — de 80 a 25 (limiar de alerta) em ~22 turnos, e as atividades (comer/dormir/banheiro) pesam de verdade (`ACTIVITY_RATES`). `backend/app/sim/engine.py:108` usa `int(classified.get("minutes") or 5)` → trocar o `or 5` por `or 30` para manter o piso coerente.
2. `backend/app/sim/life.py:368-379` (`_need_bits`): hoje o rótulo qualitativo só aparece quando a necessidade é `< 25`; o narrador vê "fome 78" sem saber se isso é bom ou ruim. Adicionar `NEED_BANDS` em `life.py`:

```python
NEED_BANDS = {
    "fome": ("saciado", "com fome", "faminto"),
    "energia": ("descansado", "cansado", "exausto"),
    "higiene": ("limpo", "sujo", "imundo"),
    "social": ("em companhia", "isolado", "sozinho"),
    "diversao": ("entretido", "entediado", "aburrido"),
    "bexiga": ("confortável", "apertado", "urgente"),
}
```

   com as faixas **ok ≥ 60**, **mid ≥ 25**, **low < 25**; `_need_bits` passa a emitir `fome 78 (saciado)` / `fome 44 (com fome)` / `fome 22 (faminto)`. `NEED_LABELS_LOW` continua existindo e passa a ser o rótulo da faixa `low` (mesmos textos). O `only_low=True` (linha 420, NPCs da cena) continua mostrando só as necessidades baixas.
3. As **consequências** já são calculadas (`life._react` → `reactions` com as frases de `_WARN`/`_CRIT`) e já viajam no payload salvo (`sim/engine.py:118` grava `"time": time_info`, e `life.advance` devolve `reactions`) — o frontend é que nunca as mostra. Ver 4.1.

**3.4 — Escolhas que importam, no idioma da história**

`backend/app/choices_service.py:33`: o prompt termina com "Texts in English, concise (under 120 chars), varied." — as sugestões saem em inglês mesmo com a história em português. Reescrever o `SYSTEM`:

- remover a linha "Texts in English";
- acrescentar: "Write every option in the same language as the scene excerpt.";
- acrescentar: "The three options must differ in risk: one safe, one bold, one that carries a real cost or consequence. Never offer an action the Facts block says is impossible.";
- manter o resto do contrato (JSON estrito com `{type, text}`, `count` opções, `MAX_TEXT=120`, `CHOICE_TYPES=("do","say","story")`).

**3.5 — Trava de qualidade do narrador**

`backend/app/output_sanitizer.py`: `clean_narration` (`:85`) limpa marcação e corta frase incompleta, mas **não** detecta o narrador escrevendo pelo jogador, nem prosa em inglês dentro de uma história em português, nem vazamento da rolagem.

Adicionar em `output_sanitizer.py`:

```python
IMPERSONATION_RE = re.compile(
    r"\b(you|voc[êe])\s+(decide|say|reach|step|draw|choose|open|close|move|walk|run|grab|point|think|feel)\b",
    re.IGNORECASE,
)
MECHANICS_RE = re.compile(
    r"\b(d20|d100|ACERTO|ERRO|CR[IÍ]TICO|you rolled|rolled a|vs CA \d|damage roll)\b",
    re.IGNORECASE,
)
PT_MARKERS = ("que", "não", "você", "com", "para", "uma", "dos", "está", "olha")
EN_MARKERS = (" the ", " you ", " with ", " door ", " and ", " looks ")

def quality_flags(text: str, *, expected_language: str = "") -> list[str]:
    """Retorna as violações do turno: 'impersonation', 'mechanics_leak', 'language'."""
```

Regras:
- `impersonation`: casa `IMPERSONATION_RE` em qualquer posição do parágrafo.
- `mechanics_leak`: casa `MECHANICS_RE`.
- `language`: só quando `expected_language == "pt"`: o texto tem ≥ 40 caracteres alfanuméricos e **nenhum** marcador de português e **≥ 2** marcadores de inglês.

**3.6 — Um retry corretivo, no máximo**

Em `backend/app/game_engine.py:472` (`text = output_sanitizer.clean_narration(...)`):

1. Calcular `flags = output_sanitizer.quality_flags(text, expected_language=<idioma da história>)`. O idioma esperado é derivado do último texto de ação em português (se o texto do jogador tem marcador PT → `"pt"`; senão `""` = não checar).
2. Se houver flags **e** o turno não for um retry manual do jogador: fazer **um** retry corretivo — a mesma chamada de geração, com uma linha extra na última user message:
   ```
   Your previous attempt broke these rules: <flags>. Rewrite the same beat, obeying them.
   ```
   e temperatura `min(1.0, temperature + 0.1)`.
3. Escolher o melhor entre as duas tentativas (menos flags; empate → a última). Se as duas tiverem flags, **manter** o texto (não descartar o turno) e registrar as flags.
4. Persistir as flags: `db.set_action_rules` **sobrescreve** a coluna `actions.rules_json` (`backend/app/db.py:235-239`) e o bloco do sim já gravou `{"before","cards","resolution"}` ali → não pode ser usado direto. Adicionar em `backend/app/db.py`:

```python
def merge_action_rules(conn, action_id: int, extra: dict) -> None:
    payload = get_action_rules(conn, action_id)
    if not isinstance(payload, dict):
        payload = {}
    payload.update(extra or {})
    set_action_rules(conn, action_id, payload)
```

   e gravar `{"quality_flags": flags}` por `merge_action_rules` (preserva `before`/`cards`/`resolution`, de que o undo depende).
5. Expor no API: `db.action_to_dict` (`db.py:269-282`) ganha a chave `"quality_flags": _action_quality_flags(row)` (leitura de `payload["quality_flags"]` do `rules_json`, default `[]`).

**3.7 — Nada de stop por nome de protagonista** (verificado: não é bug)

Os stops dinâmicos (`context_builder.py:748-763`) são `"\n> Nome"`, `"\nNome:"`, `"\n>Nome"` — só cortam o formato de fala do protagonista (`"Ana: ..."`), que é o objetivo. **Manter como está.**

---

### Fase 4 — Peso de jogo: a tela (frontend)

**4.1 — O feed mostra a mecânica e as consequências**

`pages/Play.jsx:113-127` (`RulesNote`) renderiza apenas `rules.steps` e `rules.changes`. O bloco salvo (`db._action_resolution` → `payload["resolution"]`) contém `{pack, intent, steps, changes, time, block}` — e `time.reactions` (as consequências de vida) **nunca são exibidas**.

Reescrever `RulesNote` para renderizar, dentro de um contêiner com título "Resolução do sistema":

- `rules.steps`: cada passo com o rótulo em negrito (`text-info`) e o texto em `text-info`; usar `text-info` no lugar de `text-sky-300/90`.
- `rules.changes`: `text-secondary`, como hoje.
- `rules.time.reactions` (lista de frases, quando não-vazia): linha "Consequências:" com cada frase em `text-warn-200`.
- `rules.intent`: quando `rules.intent.kind` existir e não for `"none"`, uma tag discreta (`text-muted`, `text-[11px]`) com o tipo de ação ("Ataque", "Perícia", "Uso de item", "Compra", "Descanso", "Nenhuma") — o jogador passa a ver que o sistema interpretou o que ele fez.
- `rules.quality_flags` (ver 3.6): quando não-vazio, uma linha `text-warn-200` "O narrador desviou da regra: <flags>" (em PT: "impersonation" → "escreveu pelo jogador", "mechanics_leak" → "vazou a rolagem", "language" → "respondeu em inglês").

**4.2 — O texto salvo herda a preferência do leitor**

Ver 0.9 (mesma mudança, mesma fase de código): `FeedItem.jsx:112-114` e o streaming em `Play.jsx:1268-1269`.

**4.3 — Composer no celular**

`components/InputBar.jsx:225-310`: a linha é `flex w-full items-end gap-2` com quatro botões `shrink-0` + Send → espremido em 375px. Novo layout:

- `< sm` (`max-sm:flex-col`): a textarea ocupa a primeira linha (`w-full`), e os botões ficam numa segunda linha (`flex items-center gap-2`) com o Send ocupando o resto (`flex-1`).
- `≥ sm`: manter a linha única atual.
- Todos os botões mantêm `min-h-11`.

**4.4 — Interface toda em português**

Sem framework de i18n: tradução literal no lugar (o app é PT-BR). Arquivos com texto visível ao usuário (lista verificada):

`src/App.jsx`;
`pages/`: `Home.jsx`, `Play.jsx`, `Login.jsx`, `Rulesets.jsx`, `ScenarioEditor.jsx`, `Settings.jsx`, `Users.jsx`;
`components/`: `AuthGate.jsx`, `CharacterModal.jsx`, `Drawer.jsx`, `FeedItem.jsx`, `GamePanel.jsx`, `GenerateAdventureModal.jsx`, `InputBar.jsx`, `Modal.jsx`, `OfflineBanner.jsx`, `ReaderPreferencesModal.jsx`, `SceneBackdrop.jsx`, `ShareModal.jsx`, `StoryCardsEditor.jsx`, `TagInput.jsx`, `Toast.jsx`, `avatar/ChatAvatarDock.jsx`;
`components/drawer/`: `AvatarsTab.jsx`, `CardsTab.jsx`, `ConfigTab.jsx`, `ConsistencyTab.jsx`, `DetailsTab.jsx`, `MemoryTab.jsx`, `NpcsTab.jsx`, `SimTab.jsx`.

Regras:
- Traduzir **apenas** o texto visível. Valores de rede permanecem: `do`/`say`/`story`, tipos de card, campos da API, nomes de arquivo, enums.
- Glossário fixo: Aventura (Adventure), Cenário (Scenario), Personagem (Character), Ficha (Card), História (Story), Regras (Rules), Memória (Memory), Configurações (Settings), Usuários (Users), Fechar (Close), Salvar (Save), Cancelar (Cancel), Carregando (Loading), Erro (Error), Desfazer (Undo), Tentar de novo (Retry), Dispensar (Dismiss), Continuar (Continue), Sem título (Untitled), Enviar (Send).
- Rótulos da ação do jogador (`FeedItem.jsx`): "You do" → "Você age", "You say" → "Você diz", "Story" → "Narração".
- `InputBar.jsx`: os botões Do/Say/Story passam a exibir "Ação" / "Fala" / "História" (o valor enviado continua `do`/`say`/`story`).
- Placeholder `InputBar.jsx:236` ("AI is writing…") → "A IA está escrevendo…".
- `Play.jsx:1236` ("A fresh adventure begins." / "Send a Story action below…") e `Play.jsx:1266` → PT-BR.
- `Toast.jsx`: `aria-label="Dismiss"` → `aria-label="Dispensar"`.
- As strings dos prompts internos do modelo **continuam em inglês** (decisão do usuário): não alterar `backend/app/context_builder.py`, `assistant.py`, `choices_service.py` etc. com exceção do que está explicitamente listado na Fase 3.

---

### Fase 5 — Contrato e pendências

**5.1 — `docs/openapi.yaml`**: documentar `POST /api/adventures/from-draft` (schema `AdventureFromDraft`, resposta 201 = estado da aventura) e o `error.hint` opcional das rotas do assistente.

**5.2 — `docs/vm_runbooks/phase0_baseline.md`: registrar a pendência do modelo** (decisão do usuário: trocar o modelo fica fora do plano, mas registrado). Adicionar uma seção "Pendência: modelo do LLM" com:

- Estado verificado em 2026-10-21: `GET http://vm.groupabz.com:1234/v1/models` responde 200 sem autenticação e devolve `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf`, `ftype "IQ2_M - 2.7 bpw"`, `n_ctx 262144`, `n_params 34660610888`.
- Consequência: quantização de 2 bits + fine-tune de RP degradam a aderência a JSON e a qualidade narrativa — é a causa primária de "a IA é imprecisa".
- O backend **não lê** `config/models.json`; o modelo efetivo vem de `llm.model_alias` (`"auto"`) → `GET {base_url}/models` (`backend/app/llm_client.py:261-275`).
- O `POST /v1/chat/completions` do servidor exige `LLM_API_KEY` (401 "Invalid API Key" sem ela); a chave vive no `.env` da VM (`deploy/vm/aidungeon-backend.service`, `.env.example`).
- Recomendação para quando a troca for feita: modelo de instrução com ≥ 14B em Q4_K_M ou melhor, com `response_format`/`grammar` JSON habilitado no llama-server (o `llm_client.chat_completion` ainda não envia `response_format` — `backend/app/llm_client.py:855-899`).

---

## Critical files & anchors

| Arquivo | Âncora | Por quê |
|---|---|---|
| `frontend/src/index.css` | `:6-58` (3 blocos de tema) e `:59-142` (consumidores) | A base de toda a Fase 0: é aqui que a paleta deixa de ser fixa. |
| `frontend/tailwind.config.js` | `:1-30` (arquivo inteiro) | `theme.colors` é o que faz as 340 classes `zinc-*` virarem rampas de tema. |
| `backend/app/assistant.py` | `:685-702`, `:711-729`, `:773-796`, `:873-898` | As quatro falhas parciais que hoje matam o rascunho inteiro. |
| `backend/app/routes/adventures.py` | `:92-160` (`create_adventure`) | A transação única que faz rollback da aventura — e onde nasce o endpoint `from-draft`. |
| `backend/app/context_builder.py` | `:535-778` (`build_context` + stops) | Onde entram `[Rules]` e a diretiva de terceira pessoa, e onde `third_person` é ignorado. |

## Verification

Pré-requisitos: `python` com o venv do backend (`backend/.venv`, o `pytest` roda de `tests/`), `node`/`npm` para o frontend. Não há dependência nova a instalar.

**Backend**
1. `cd /mnt/DEV/Projeto/aidungeon-local && backend/.venv/bin/python -m pytest tests -q` — a suíte (~487 testes) precisa seguir verde. Ajustes esperados: `tests/test_sim_life.py` (nenhuma taxa muda — só o default de minutos e os rótulos das faixas; se algum teste fixar o texto de `_need_bits`, atualizar a expectativa para o novo formato, sem remover o teste), `tests/test_v9_scenarios.py` (auth nova) e `tests/test_adventure_mgmt.py` (jobs).
2. Comportamento novo, com LLM mockado (sem rede):
   - `POST /api/assistant/generate` com a ficha de personagem retornando JSON inválido → **200**, `title` e `story_cards` preservados.
   - `POST /api/assistant/generate` com todas as tentativas de card falhando → **200**, `story_cards` vazio (não 503).
   - `POST /api/adventures/from-draft` → **201**; `GET /api/adventures/{id}` → **200** com `character` salvo e `character_rules` do ruleset aplicado; `GET /api/scenarios/{id}` → o cenário existe **e** está referenciado pela aventura (sem órfão).
   - `POST /api/adventures` com `jobs.enqueue_all_portraits` levantando → **201** e a aventura existe.
   - `POST /api/scenarios` sem token → **401**.
   - `build_context` de uma aventura com `character_rules` → o system message contém `[Rules]` e a tabela do ruleset; com `third_person=True` → contém a diretiva de terceira pessoa.
   - `output_sanitizer.quality_flags("You decide to open the door.")` → `["impersonation"]`; `quality_flags("The dice roll came up 17.")` → `["mechanics_leak"]`; um parágrafo PT limpo → `[]`.

**Frontend**
3. `cd frontend && npm run build` — precisa passar (o Tailwind resolve todas as classes; um token errado quebra o build).
4. `cd frontend && node scripts/smoke_and_audit.js` — o script roda contra `dist`; deve terminar sem erro de console.
5. `cd frontend && node scripts/capture_screenshots.js` — o script hoje captura **só o tema padrão**. Estendê-lo para os três temas: em `captureScreen` (`frontend/scripts/capture_screenshots.js:293-318`), o `page.evaluateOnNewDocument` já grava o token; acrescentar `localStorage.setItem('aidl_theme', theme)` (a chave é `aidl_theme`, valores `obsidian`/`parchment`/`oled`, usada por `frontend/src/themeContext.jsx`) e capturar `home` e `play` em cada tema. Conferir nas imagens: no **Parchment** nenhum bloco escuro (modal, card, drawer, toast, banner), o inspetor respeita o botão de esconder, o parágrafo salvo muda de tamanho quando a preferência de leitor muda, e o toast de erro é legível no tema claro. (Conferir também `docs/screenshots/` — as capturas antigas servem de referência do "antes".)
6. Greps de obediência (todos devem retornar **zero** depois das fases):
   - `grep -rn "sky-\|rose-\|violet-\|orange-" frontend/src` (item 0.6 — nenhuma família fora da rampa).
   - `grep -rn "animate-in\|backdrop-blur-xs" frontend/src` (item 0.5).
   - `grep -rn "window.prompt\|window.confirm" frontend/src` (item 2.5).
   - `grep -rn "var(--bg-\|var(--text-\|var(--accent-\|var(--border-" frontend/src` — o único resultado aceito é `var(--app-height)` em `components/Modal.jsx:43` (item 0.3).
   As classes `zinc-*`/`emerald-*`/`red-*`/`amber-*` **continuam no código** de propósito: é o nome da família que o Tailwind mapeia para a rampa do tema. A prova de que elas seguem o tema é a captura de tela do item 5 nos três temas.

**Manual (precisa da chave do LLM)**
8. Com `LLM_API_KEY` da VM no ambiente, `POST /api/assistant/generate` → criar a aventura pela UI → a aventura **aparece** em `/play/<id>` com ficha de personagem, sem cenário órfão; enviar uma ação → o feed mostra a "Resolução do sistema" (passos, mudanças, consequências) e as sugestões de escolha **em português**; abrir a aba Vida/Regras → as necessidades têm rótulo qualitativo ("faminto", "saciado") e o relógio anda ~30 min por turno.

## Assumptions & contingencies

- **Chave do LLM**: `LLM_API_KEY` não está no repositório (só `.env.example`); ela vive no `.env` da VM (`deploy/vm/aidungeon-backend.service`). Sem ela, toda a verificação de backend roda com LLM mockado (itens 2) e a verificação manual (item 8) fica pendente — registrar isso no relatório final em vez de fingir que rodou.
- **Modelo IQ2_M**: pode não seguir o JSON. Mitigação já implementada na Fase 1 (falha parcial não derruba o rascunho). Se, mesmo assim, o rascunho falhar com "resposta sem 'title'", o frontend deve reduzir `card_count` de 5 para 3 e repetir uma vez (`AssistantGenerateRequest.card_count` aceita 3..8, `backend/app/schemas.py:283-299`).
- **Budget do assistente**: `generate_scenario_draft` usa `max(max_tokens, 1400 + 350 * card_count)` (`backend/app/assistant.py:679-688`) = 3150 tokens com 5 cards; com um modelo IQ2_M o JSON pode truncar e cair no salvage (`_salvage_truncated`, `assistant.py:201-245`). Se o salvage falhar, a Fase 1 mantém o cenário sem cards — não é motivo para reverter a Fase 1.
- **Troca do modelo**: fora do escopo (decisão do usuário). Registrada em 5.2. Se o usuário trocar o modelo depois, `llm.model_alias` (`"auto"`) faz a descoberta sozinha — o app não depende do nome do modelo.
- **Temas**: se alguma tela ficar com contraste ruim depois da rampa, o ajuste é **nos valores da rampa** (`index.css`), nunca em componente — a rampa é a fonte da verdade.
- **`third_person`**: se a aventura for criada sem `third_person` explícito, o valor vem do cenário (`routes/adventures.py` herda `sc["third_person"]`) → a diretiva nova entra só quando o campo for verdadeiro.
- **`character_rules` no prompt**: se `character_rules` for gigante, `_fit_budget` corta no limite de 900 tokens; a tabela truncada ainda é útil e não quebra o contexto.
