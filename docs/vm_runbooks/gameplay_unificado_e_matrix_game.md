# Plano: um turno, quatro saídas — e o Matrix-Game 4.0

Leitura em 2026-10-18 na branch `remote-code-ai-dungeon-fluido-3b22`.
Só plano. Nenhum código de produto. Os diffs alheios foram lidos e não foram staged.

A VM é uma NVIDIA L4 de 23 034 MiB. O narrador (llama-server, Qwen3.6-35B IQ2_M, `-ub 1024`) ocupa cerca de 13,2–13,5 GiB. Com o A1111 SDXL no mesmo cartão o pico medido foi 21 253 MiB (800×720) e 21 937 MiB (832×1216). A voz (`abz-voice`) ficou em 1 426 MiB no baseline de 16/10. Fonte: `docs/vm_runbooks/phase5_slots_record.md` e `phase0_baseline_record.md`.

## A. Ligar imagem, emoção e reação

**Sim.** Quando o travamento de likeness e o passe de emoção/pesos terminarem, as quatro saídas podem falar do mesmo acontecimento. A costura que falta é um único registro de beat por `seq` da resposta (`continue`). Não é outro modelo.

As quatro saídas do mesmo turno:

1. Narração que já leu emoção, vontade e postura de cada NPC presente.
2. Atualização de pesos da ficha a partir desse mesmo texto.
3. See e backdrop com o mesmo elenco, a mesma emoção e a mesma ação.
4. TTS que fala a frase com essa emoção.

### O que um turno faz hoje

1. `game_engine.handle_action` grava a ação do jogador (`do` / `say` / `story`) e chama `_stream_generation`.
2. `context_builder.build_context` monta o sanduíche. A system leva o narrador e o `[Plot]`. Cards, memórias, resumo e estado do jogador vão no `<lore>` da última mensagem user. Cada card entra por `npc_assets.card_canon_text`: Status, Appearance, Body, Clothing, Personality, Stats (`str` `dex` `con` `int` `wis` `cha`), Notes. Ações `see` ficam de fora.
3. O texto limpo por `output_sanitizer.clean_narration` vira uma action `continue`. O lock solta, sai `turn_done` e o `done`.
4. `_spawn_post_turn` dispara choices e, em seguida, `npc_chronicle.update_from_turn`. Esse passe lê o trecho recente, pede um JSON e grava ficha: fatos, status, personalidade e `stat_delta` de −2 a +2. Rosto já gravado não muda, salvo `look_changed`.
5. See (`POST /see`, `action_seq`) e backdrop (`scene.sync_scene`) são outros pedidos. Não esperam o chronicle.
6. O TTS é o cliente: `FeedItem` parte a prosa e chama `POST /api/tts` com `{text, voice}`. `voice_client.synthesize` manda `{model, voice, input, response_format: "mp3"}` ao stack Supertonic. Não há campo de emoção.

### O que já é verdade

- A narração **já vê** a ficha durável (rosto, roupa, personalidade, atributos, status, notas) quando o card entra no `[World info]`. Não vê emoção, vontade nem postura deste instante. Personalidade não é o humor do beat.
- O chronicle **já atualiza** atributos e fatos **depois** do `done`, num segundo passe de LLM, sobre o mesmo trecho. A prosa deste turno não leu o delta que ela mesma provoca. O turno seguinte só vê o delta se o card disparar de novo.
- See e backdrop **já compartilham** o elenco por menção (`scene._cast`, teto 4) e a cláusula `exact likeness`. O protagonista entra no See quando o texto diz "you". Cena de gesto (`kiss`, `beijo`, luta, dois nomes) pede plano aberto e negative de busto, para não virar um casal genérico. O retrato **não** vira img2img no See.
- O clima do fundo (`night`, `rain`, …) vem de palavra no texto. O `mood` do elenco (`scene._mood_near`, janela de 200 caracteres) volta no JSON da cena e **não entra** no prompt da pintura.
- O avatar tem um terceiro detector, `frontend/src/components/avatar/emotionDetect.js` (mesmas famílias: happy, sad, angry, fear, love), que muda o TalkingHead. O áudio não usa esse mood.

As janelas discordam. O See pinta os dois últimos trechos que não são `see` (`current_moment`). O backdrop usa as últimas 8 ações, cortadas em 4 000 caracteres (`_recent_text`). O mood do backend e o do frontend varrem palavras parecidas em raios parecidos, cada um por conta própria. Uma frase antiga com "sorri" pode vencer o que acabou de acontecer.

### O que os outros dois agentes estão acrescentando

Lidos sem stage. O diff de likeness estava sujo em `backend/app/jobs.py`, `backend/app/npc_assets.py`, `backend/app/routes/images.py`, `backend/app/scene.py`, `tests/test_npc_chronicle.py` e `tests/test_scene.py`, e entrou no commit `09fac9a` (`fix(see): trava o rosto do card antes da frase da ação`) enquanto este plano era escrito. `image_client.py` não mudou nesse commit: o See segue sem img2img. `voice_client.py`, `context_builder.py`, `llm_client.py` e `llm_scheduler.py` continuavam limpos.

**Likeness (commit `09fac9a`).** O primeiro bloco do CLIP passa a abrir com o rosto, não com a frase da ação: "kisses on the sofa" na frente pintava um casal genérico. See e backdrop chamam `npc_assets.likeness_clause`. `contradicting_looks` tira cabelo e roupa que o card não confirma e deixa o verbo. Ficha em português vira frase curta em inglês (`english_appearance`); o que continua em português não entra no prompt. A cor de cabelo do `portrait_prompt` vence a ficha. Negative novo: `different person, wrong hair color, wrong skin tone, extra unnamed person` (`IDENTITY_NEGATIVE`), também no job do backdrop. Cena nua guarda o rosto e solta a roupa. O See continua sem img2img.

**Emoção e pesos (ainda não estão no diff).** O pedido em curso é Drama-3 no cliente de voz, emoção na mensagem final do `context_builder`, e pesos de RPG na ficha. Hoje a ficha só tem os seis atributos de sempre. O `POST /tts` não aceita instrução de atuação. Drama 3 (Fish Audio, 13/10/2026) é um TTS de 35B MoE com cerca de 3B ativos por token, direção em linguagem natural e pesos abertos ([relato técnico](https://arxiv.org/html/2610.18010v2), [anúncio](https://www.prnewswire.com/news-releases/fish-audio-launches-drama-3-direct-ai-voices-in-plain-language-control-every-word-and-shift-emotion-mid-sentence-302906620.html)). Não cabe nesta L4 ao lado do Qwen. A costura usa o campo de direção que esse agente criar no cliente já existente (API ou o stack de voz da VM). Não sobe um segundo 35B na GPU.

### A costura

Um JSON `beat` por `seq` da action `continue`, escrito pelo passe que **já** lê esse trecho (`npc_chronicle`). Estender o JSON desse passe. Não criar outra chamada de LLM.

```text
beat = {
  seq,
  action: "frase curta do que acabou de acontecer",
  cast: [{card_id, name, emotion, want, stance}],
}
```

`emotion` é uma das cinco que o avatar já entende (`happy`, `sad`, `angry`, `fear`, `love`) ou vazio. `want` e `stance` são uma frase cada, tiradas do trecho, sem inventar fato novo. O delta de peso que o outro agente gravar na ficha sai **deste mesmo** objeto, junto com o `stat_delta` que já existe.

Quem lê o quê:

| Saída | Lê | Quando |
|---|---|---|
| Narração | emoção, vontade e postura **já gravadas** na ficha dos presentes | antes de gerar, na user final, depois do `</lore>` e antes da última ação |
| Pesos | o beat deste turno | no `apply_character`, no chronicle que já roda depois do `done` |
| See e backdrop | o beat deste `seq`: mesmos `card_id`, mesma ação, mesma emoção, likeness na frente | no pedido de imagem |
| TTS | a frase narrada mais a emoção do beat, no campo de direção do Drama-3 | no `POST /tts` |

O chronicle é assíncrono e o See pode ser pedido no meio do turno. Por isso o beat publica um evento `beat_ready` no mesmo barramento do `choices_ready`. See e backdrop pedidos antes do beat continuam com a regra de hoje (likeness + momento, sem casal genérico). Quando o beat chega, o backdrop já tem `supersede_key`; um fundo pendente do mesmo turno é reenfileirado com a ação e a emoção do beat. O See assíncrono (contrato C6) faz o mesmo se ainda estiver em `preparing` ou `pending`.

A user final não recebe um bloco novo de lore no prefixo cacheado: o texto do beat entra só na última mensagem, como o `[Canon:]` já entra. Quem não está em `scene._cast` do trecho recente não entra na lista. Morto ou ausente continua fora da pintura, como a likeness já faz com `status`.

O detector de palavra do frontend deixa de decidir o humor quando o beat existe. `emotionDetect.js` fica como reserva se `beat_ready` não chegou.

Não mexer em `llm_client.py` nem `llm_scheduler.py` para isso. O chronicle já tem role e slot.

### Arquivos da implementação futura

- `backend/app/npc_chronicle.py` — campos no JSON e gravação do beat no `seq`.
- `backend/app/npc_assets.py` — campos na ficha e a linha que `card_canon_text` mostra ao narrador (emoção, vontade, postura, pesos). Sem reescrever o rosto.
- `backend/app/context_builder.py` — só a user final.
- `backend/app/scene.py` e `backend/app/routes/images.py` — leem o beat; likeness continua na frente.
- `backend/app/jobs.py` — só se o reenfileirar do backdrop precisar do payload novo. O negative de elenco já ganhou `IDENTITY_NEGATIVE` no `09fac9a`.
- `backend/app/routes/voice.py`, `backend/app/voice_client.py`, `backend/app/schemas.py` — um campo opcional de direção no `TtsRequest`. `schemas.py` é append-only (contrato).
- `frontend/src/api.js`, `frontend/src/components/FeedItem.jsx` — mandam a emoção do beat junto com o texto. `emotionDetect.js` só na reserva.

`game_engine.py` não precisa mudar se o chronicle, que já roda no pós-turno, gravar o beat. Evitar import circular: o beat mora no chronicle ou num módulo pequeno, não um `import` de `routes/images` dentro de `scene.py` (o diff de likeness já faz isso por causa do ciclo).

### Testes que provam o mesmo beat

Um fixture, dois NPCs, um trecho: Alusair furiosa, quer o portão fechado, recusa; Caio está na cena; um terceiro já saiu.

- `tests/test_context_builder.py` — a user final contém a emoção, a vontade e a postura gravadas na ficha, e não contém o NPC que saiu.
- `tests/test_npc_chronicle.py` — o mesmo JSON atualiza o peso e grava o beat nesse `seq`; o rosto não muda.
- `tests/test_npc_chronicle.py` / `tests/test_scene.py` — o prompt do See e o do backdrop citam os dois nomes, a ação do portão e `angry`, com `exact likeness` antes da ação. Não há casal genérico. O terceiro nome não aparece. "sorri" numa frase anterior não troca o humor.
- `tests/test_v4_voice.py` — o payload de `synthesize` leva a frase da narração e a direção dessa emoção. Sem beat, o TTS de hoje (só texto e voz) continua válido.

Os testes de likeness que já existem continuam: `test_see_keeps_the_face_and_puts_the_beat_before_the_style`, `test_see_paints_the_kiss_not_a_portrait`, `test_scene_paints_the_card_face_not_the_prose`.

## B. Matrix-Game 4.0

**No-go** como substituto do See, do narrador ou de um viewport desta aventura. **Later**, e só como jogo à parte (Castelo, Egito ou corrida), numa GPU que não esteja com o llama-server e o SDXL. Não instalar agora.

O que saiu em 15/10/2026, com nota de 16/10, é render neural em fluxo para três jogos low-poly executáveis. O motor do jogo é dono da física, do input e da regra. O modelo re-renderiza o RGB limpo. HUD e som ficam fora do modelo. Não é um mundo jogável a partir de texto, nem um gerador de cena da história. ([README](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/README.md), [card](https://huggingface.co/Skywork/Matrix-Game-4.0))

| | |
|---|---|
| Código | [SkyworkAI/Matrix-Game](https://github.com/SkyworkAI/Matrix-Game), pasta `Matrix-Game-4/`. Apache-2.0. Terceiros mantêm a licença deles. |
| Relato | [PDF no repositório](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/docs/Matrix_Game_4_0.pdf), commit de 16/10/2026. O anúncio aponta esse PDF, não um id arXiv separado. Site: [matrix-game-v4-0.github.io](https://matrix-game-v4-0.github.io/page/). |
| Pesos | [Skywork/Matrix-Game-4.0](https://huggingface.co/Skywork/Matrix-Game-4.0). Um checkpoint por cena, 5 001 016 512 parâmetros BF16. Não é pipeline Diffusers. Pesos, imagens sink e assets dos três jogos: Apache-2.0 (Skywork). Wan2.2-Fun-5B-Control e umT5: Apache-2.0. TAEHV: MIT. Áudio da corrida: CC0 e CC BY 3.0. |
| Gera | Vídeo RGB 480×864 do Castelo, do Egito Antigo ou da City Racing. Um chunk latente são 4 frames depois da largada. Não gera ficha, prosa nem imagem solta de um NPC arbitrário. |
| Entrada | RGB low-poly limpo, sem HUD. Prompt de texto **fixo** no Castelo e no Egito. A corrida aceita legenda dinâmica por chunk, e isso baixa o FPS. A imagem sink é prefixo de identidade do modelo, não o retrato do card. Teclado e ação vão para o executável C++, não para o DiT. ([arquitetura](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/docs/architecture.md)) |
| VRAM e latência | Presets publicados: A100 80 GB, RTX 5090, 5080 e 5070 Ti. Não há preset L4. Na 5080 o modelo fica em ~9,6 GB (~42 FPS) ou ~12,8 GB com SageAttention (~67 FPS). Na 5070 Ti, ~9,2 GB (~41 FPS) ou ~10,9 GB (~58 FPS). Isso é 864×480, um passo, TAEHV, regime estável, sem I/O de vídeo. Nessas duas placas o prompt dinâmico não cabe: o T5 é descarregado antes do DiT. No A100 80 GB o preset `realtime` mede ~38–39 FPS (Castelo/Egito, processamento do modelo) e ~38,5 / ~28,9 FPS na corrida (fixo / dinâmico, pipeline online). `--fps 40` é a cadência de captura, não a latência ação-até-fóton. Encode, rede, buffer e decode somam por cima. Uma sessão e uma cena por processo. |

### Encaixa nesta VM?

Não ao lado da stack. 13 GiB do narrador mais ~10 GiB do DiT passam de 23 GiB antes mesmo do SDXL, que sozinho já leva o pico conjunto para 21–22 GiB. A voz ainda usa ~1,4 GiB. O preset de 16 GB só existe para Blackwell (5080 / 5070 Ti), com prompt fixo e kernels próprios (FP8 no 5090, INT8 no 5070 Ti, CUDA 13.2, PyTorch 2.12). A L4 é Ada. Não há medida de que o preset `realtime` do A100 rode aqui.

O que ele substituiria, se um dia rodar sozinho: um viewport ao vivo **daqueles três jogos**, com o executável low-poly por baixo e o modelo só pintando o RGB. Não substitui o See, o backdrop, o retrato, o narrador em português, o chronicle nem a ficha. O próprio README deixa jogar o executável sem carregar peso nenhum (`scripts/play.sh`); isso também é outro produto.

O que ele não faz, pelo contrato publicado: estado longo de história, memória de NPC, ficha, narração em português, elenco arbitrário, prompt livre de aventura. Nos atiradores não há prompt dinâmico, nem reset de cache, nem memória recuperada. A sessão tem orçamento finito de RoPE (8192 chunks latentes) e acaba. Os modelos são especializados nas cenas e nas câmeras que vieram no treino ([card](https://huggingface.co/Skywork/Matrix-Game-4.0), [arquitetura](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/docs/architecture.md)).

Matrix-Game 3.0 (27/03/2026) era um world model interativo em primeira pessoa. O 4.0 estreita isso para render do jogo executável. Trazer o 3.0 também não narra esta aventura, e não foi o objeto deste estudo.

### Recomendação

Não instalar, não baixar peso, não encostar no A1111 nem no llama-server por causa disto. O vínculo da seção A segue no SDXL. Reabrir o Matrix-Game só se aparecer uma segunda GPU, ou uma janela em que narrador e SDXL estejam desligados, e o produto quiser mesmo o Castelo, o Egito ou a corrida como modo separado. Aí o primeiro teste é o preset `realtime` vazio, medindo VRAM e FPS na L4, com o serviço em loopback. Sem esse número, continua no-go.

## Fontes

- [Matrix-Game 4.0 README](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/README.md) (commit de 16/10/2026)
- [Relato técnico em PDF](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/docs/Matrix_Game_4_0.pdf) (16/10/2026)
- [Arquitetura](https://github.com/SkyworkAI/Matrix-Game/blob/main/Matrix-Game-4/docs/architecture.md)
- [Skywork/Matrix-Game-4.0](https://huggingface.co/Skywork/Matrix-Game-4.0)
- [Site do projeto](https://matrix-game-v4-0.github.io/page/)
- [Fish Audio Drama 3, relato técnico](https://arxiv.org/html/2610.18010v2)
- [Fish Audio Drama 3, anúncio](https://www.prnewswire.com/news-releases/fish-audio-launches-drama-3-direct-ai-voices-in-plain-language-control-every-word-and-shift-emotion-mid-sentence-302906620.html) (13/10/2026)
