# BUILD — fidelidade à foto de referência e nudez no See

Sequência de prompts usada para corrigir duas falhas do aidungeon-local e os
checkpoints que provam cada uma. Formato do gauntlet-build: cada prompt é uma
instrução para um agente, e o checkpoint vem depois do bloco.

## O que se constrói

- **Projeto:** aidungeon-local. Backend FastAPI em `backend/app`, frontend
  React/Vite em `frontend/src`, testes pytest em `tests/`.
- **Onde roda:** VM `vm.groupabz.com`, GPU L4 de 23 GB dividida entre o narrador
  (llama-server com visão, porta 1234) e o ComfyUI (FLUX.2 Klein 4B, porta 8188).
  Staging na porta 2123, produção na 2122.
- **Quem usa:** o dono do jogo, que cria personagens a partir de fotos geradas
  por IA e joga histórias com eles.
- **Falha 1:** o See pintou nu o personagem do jogador, que a narração não despiu.
- **Falha 2:** retrato e 360° de um card com foto de referência saíam como outra
  pessoa, com outra roupa, e diferentes entre si.

## Regras que valem em todos os prompts

- Não usar o Colab (créditos do dono).
- Não afrouxar a recusa de imagem com menor de idade nu ou em cena explícita.
- Testar com "Marina Teste" e "Bruno Teste" no staging, aventura
  `41c17c7bf1ed43a08754d2a31bd75e61`.
- A suíte inteira roda na VM: `./run_tests.sh ../tests` em `~/aidungeon-wip`.
  Na máquina local, só um arquivo de teste por vez.
- Nenhum teste é removido; teste que descrevia o comportamento antigo é
  reescrito para o novo, com a razão em comentário.
- Cadeia do 360°: mestre, frente, perfil, costas e close-up, até duas tentativas
  por pintura.

## Prompt 1 — Nudez do See só em quem a narração despiu

```
Corrija a decisão de quem está nu no See. Sintoma: num turno em que o jogador
escreveu "tiro a roupa de todas as mulheres e as deixo nuas" (sem ponto final) e
a narração começou na linha seguinte com "Caio observou...", o See pintou o Caio
nu. Na narração ninguém se despiu. Reproduza antes de mexer e deixe um teste de
regressão.

1. Reprodução. Em `backend/app/npc_assets.py`, a função `_nude_states(cards,
   text)` decide quem está nu a partir do texto do momento. A rota do See
   (`backend/app/routes/images.py`, `_build_final_prompt`) monta esse texto com
   `current_moment(actions, seq)`, que junta a linha do jogador e a narração
   separadas por quebra de linha. Chame `_nude_states` com o texto real nas três
   formas: só a fala, só a narração, as duas juntas. O erro só aparece nas duas
   juntas: é a prova de que a causa é a junção, e não um padrão errado de nudez.

2. Causa. Os padrões de estado usam `\s+` entre a palavra de nudez e o nome
   seguinte. `\s+` atravessa a quebra de linha, então "nuas\n\nCaio" é lido como
   "nuas Caio" e o nome que abre a linha seguinte herda o estado.

3. Correção, em `_nude_states`, antes de calcular os nomes coordenados: troque
   cada sequência de quebras de linha por um marcador que nenhum padrão
   atravessa (um caractere que não é espaço nem letra, seguido de uma quebra).
   Uma linha, com um comentário dizendo o caso que quebrou. Não reescreva os
   padrões nem a função `scene_clothing`.

4. Teste novo em `tests/test_scene.py`:
   `test_nudity_does_not_jump_from_one_line_to_the_name_that_opens_the_next`.
   Entradas: (a) fala terminada em "nuas" e narração começando com o nome do
   jogador: ninguém nu; (b) uma frase de nudez verdadeira sobre a NPC em linha
   própria: só a NPC nua; (c) duas pessoas na mesma linha, uma nua: continua
   como antes.

5. Não mude a prioridade entre o pedido do jogador e o momento, nem a trava de
   menor em `likeness.decide_frame`.

6. Confira o turno real na produção, só leitura: com o mesmo texto, a decisão
   passa a ser "ninguém nu".
```

### Checkpoint 1

- [ ] `pytest tests/test_scene.py -k nudity_does_not_jump` exits 0
- [ ] Com a correção revertida, o mesmo teste fails com o nome do jogador na lista de nus
- [ ] `_nude_states` com a fala e a narração do turno real returns conjunto vazio
- [ ] `_nude_states` com "Marina está nua." em linha própria returns `{"Marina"}`
- [ ] `tests/test_scene.py` e `tests/test_world_facts.py` inteiros exits 0
- [ ] `likeness.decide_frame` com menor em cena explícita ainda returns o código de recusa

## Prompt 2 — A foto é lida mesmo quando o narrador não vê imagem

```
A aparência do personagem (`look_lock`) é lida da foto de referência pelo modelo
de visão do narrador, em `backend/app/look_lock.py`. No dia da falha o narrador
ativo era o llama-server do Colab, sem mmproj: ele respondia HTTP 500 com a
mensagem "image input is not supported - hint: if this is unexpected, you may
need to provide the mmproj". A leitura desistia, o card ficava sem `look_lock` e
o resto da cadeia inventava a aparência pelo texto. Faça a leitura procurar quem
vê.

1. `look_lock._look_reply(llm_cfg, messages, transport)` hoje só tenta outro
   leitor quando o erro contém "404". Crie `_cannot_see(exc)`: verdadeiro para
   "404", para "image input is not supported" e para "mmproj", sem diferenciar
   maiúsculas.

2. Crie `_seeing_cfgs(llm_cfg)`, que devolve, nesta ordem, as configurações de
   quem pode ler:
   a. o narrador desta máquina: `settings_store.effective_runtime_settings(conn)
      ["endpoints"]["local_llm"]`, com `model_alias` "auto", só quando essa URL é
      diferente da `base_url` ativa (comparar sem a barra final);
   b. o perfil com visão que já existe: `provider_profiles.vision_llm_cfg(conn,
      current_model=...)`.
   As outras chaves de `llm_cfg` são mantidas (chave de API, limites, agenda).

3. `_look_reply` tenta o narrador ativo; se `_cannot_see`, tenta cada
   configuração de `_seeing_cfgs` e devolve a resposta junto com a configuração
   que respondeu, porque a leitura de roupa (`_read_clothing`) e a de corpo
   (`_read_body_text`) usam a mesma. Se ninguém lê, propaga o último erro; quem
   chama (`_read`) já registra "visão indisponível" e segue sem derrubar a
   pintura. Erro que não é falta de visão continua subindo na hora, sem tentar
   os outros.

4. Papel e limites da chamada ficam iguais: `role="assets"`, temperatura 0,
   `max_tokens=4000`, `timeout_s=240`.

5. Teste novo em `tests/test_look_lock.py`:
   `test_a_narrator_that_cannot_see_hands_the_photo_to_this_machine`. Um
   transporte falso responde 500 com a mensagem do mmproj para o host
   `colab.test` e a leitura válida para `local.test`; grave
   `runtime.endpoints.local_llm` numa sessão própria antes de ler (a função abre
   outra conexão). Espera-se: o look gravado, a primeira chamada no host do
   Colab, as seguintes só no host local, e duas leituras (traços e roupa).

6. Não mexa no notebook do Colab: carregar mmproj lá é outro trabalho e não pode
   ser provado sem ligar uma sessão.
```

### Checkpoint 2

- [ ] `pytest tests/test_look_lock.py -k cannot_see` exits 0
- [ ] Com `_cannot_see` reduzido a "404", o teste fails sem `look_lock` gravado
- [ ] O teste antigo do recuo após falha (`test_see_reads_missing_looks_once_and_backs_off_after_a_failure`) exits 0
- [ ] Um erro 503 do narrador ativo não chama o host local: a lista de hosts contains só o primeiro
- [ ] No staging, `look_lock` de "Marina Teste" shows `source: photo` e `version: 9` depois de um 360° novo
- [ ] O log da produção não shows "visão indisponível" durante um 360° feito com o narrador da VM

## Prompt 3 — O mestre do 360° sai da foto, com a roupa da foto

```
O retrato-mestre do 360° recebia só recortes de rosto como referência; a roupa
vinha de um texto (`outfit_lock`). Quando a foto não era lida, esse texto era
escrito pelo narrador sem ver nada: "light blue short-sleeved blouse with a
ruffled collar, dark indigo jeans, white sneakers" para uma personagem cuja foto
mostrava outra roupa. Prova feita no staging com "Marina Teste": só com o rosto
e sem texto, o mestre saiu de short preto; com a foto principal inteira entre as
referências, saiu com a camiseta "OCEAN CLUB", a calça cargo verde e a bota
caramelo da foto, mesmo sem leitura nenhuma. Faça a foto vestir o mestre.

1. `backend/app/jobs.py`, `_turnaround_references`: com foto, o mestre recebe os
   recortes de rosto e depois a foto principal inteira (`[*faces, *primary]`),
   no teto de 4. O close-up continua rostos + mestre; frente, perfil e costas
   continuam primeiro rosto + mestre. Sem foto, o caminho da âncora não muda.

2. Crie as constantes `_PHOTO_OUTFIT` ("Outfit: exactly the garments, colours,
   footwear and accessories this person wears in the reference photo, nothing
   added and nothing changed. Same outfit in every view.") e
   `_PHOTO_OUTFIT_BUST` ("Clothing and accessories exactly as worn in the
   reference photo").

3. `_turnaround_prompts` ganha `photo_outfit: bool = False`. Com ele ligado: não
   usa trava nenhuma, a frase de roupa do mestre, das vistas e do giro é
   `_PHOTO_OUTFIT`, a do close-up é `_PHOTO_OUTFIT_BUST`, e as regras de estampa
   (letras, costas lisas) não rodam, porque não há peça escrita.

4. `_execute_turnaround`: depois de `look_lock.ensure`, se o card tem foto
   principal (`look_lock.primary_photo`) e continua sem `look_lock`, ligue
   `photo_outfit`, registre um aviso no log e NÃO chame `outfit_lock.ensure`:
   nada inventado é gravado na ficha. Na próxima vez em que a leitura funcionar,
   a trava nasce das peças que a foto mostra.

5. Crie `_photo_references(card, with_primary=True)`: recortes de rosto de
   `look_lock.face_references` e a foto principal, todos por
   `turnaround.reference_photo_png` (PNG, lado menor de pelo menos 512 px), sem
   repetidos, no máximo 4; vazio para menor de idade. O Prompt 4 usa a mesma
   função.

6. Testes. Em `tests/test_look_lock.py`, o teste das referências passa a
   esperar `[b"f1", b"f2", b"p"]` no mestre. Em `tests/test_turnaround.py`,
   `test_uploaded_photo_leads_the_master_and_every_view` ganha três afirmações
   para a foto sem leitura: o prompt do mestre contém "wears in the reference
   photo", nenhum prompt contém "Default outfit", e a ficha não tem
   `outfit_lock`.

7. Não aumente `megapixels` das referências: medido na L4, 1.0 dobra o tempo
   (6 s para 13 s por pintura) sem ganho visível.
```

### Checkpoint 3

- [ ] `pytest tests/test_turnaround.py tests/test_look_lock.py` exits 0
- [ ] `_turnaround_references("master", faces=[f1, f2], primary=[p], ...)` returns `[f1, f2, p]`
- [ ] Com a foto sem leitura, o prompt do mestre contains "wears in the reference photo" e nenhum prompt contains "Default outfit"
- [ ] Depois do 360° sem leitura, a ficha do card não contains a chave `outfit_lock`
- [ ] No staging, o mestre de "Marina Teste" shows camiseta branca "OCEAN CLUB", calça cargo verde e bota caramelo, como na foto
- [ ] No staging, o mestre de "Bruno Teste" shows camisa xadrez vermelha e preta, jeans escuro e bota marrom, como na foto
- [ ] O resultado do job de 360° dos dois returns `view_problems` vazio

## Prompt 4 — O retrato de busto sai da mesma foto que o 360°

```
O retrato de busto de um card com foto enviada era pintado pelo texto da ficha,
no estilo da aventura, com a âncora (um retrato já pintado) como referência: no
staging a Marina saiu em desenho, de capuz; na produção um personagem com foto
saiu como "pale skin, dark hair" do texto. O caminho que segue a foto já existia
em `_execute_portrait` (`backend/app/jobs.py`), mas só para pessoa real
(`real_person`). Abra-o para todo card adulto com foto enviada e dê a ele as
mesmas referências do mestre.

1. `reference_identity` passa a ser: não é menor e
   `likeness.has_reference_image(card)`. Com isso o retrato usa o prompt fixo
   "Upper-body portrait of the exact same adult person shown in the supplied
   reference photo...", fundo neutro, sem o estilo da aventura e sem o texto da
   ficha.

2. Referências, só para o provedor `comfyui`: `_photo_references(card,
   with_primary=...)` do Prompt 3. Se vier vazio (pessoa real só com a foto da
   Wikipedia), fica o caminho antigo, `likeness.portrait_face_references`. A
   âncora não entra quando há foto enviada.

3. Roupa. Sem roupa da história (`current_outfit` vazio): a foto principal entra
   inteira e o texto diz a roupa travada visível (`outfit_lock.visible_text`)
   ou, sem trava, `_PHOTO_OUTFIT_BUST`. Com roupa da história que seja roupa de
   verdade e não nudez: entram só os rostos (`with_primary=False`) e o texto
   "current outfit: ..." veste; a frase da foto não aparece.

4. `backend/app/npc_chronicle.py`, `_look_locked`: card com foto enviada
   (`sheet["reference"]` com `image_path` ou `images`) conta como visual
   travado. Antes, a crônica gravava uma roupa tirada da prosa ("um terno,
   provavelmente preto") num card com foto que ainda não tinha retrato, e o
   retrato saía com ela.

5. Testes. `tests/test_look_lock.py`:
   `test_portrait_of_a_photo_card_is_painted_from_the_photo`, com um card de
   duas fotos e `face_images: [2]`: o prompt contém a frase da foto e não contém
   o `portrait_prompt` do card; as referências são iguais a
   `_photo_references(card)` e às do mestre; com `current_outfit` "a red
   raincoat" entra só o rosto e o prompt contém a capa. `tests/test_npc_chronicle.py`:
   `test_a_card_with_a_reference_photo_keeps_the_photo_look`. Em
   `tests/test_reference_lock.py`, os dois testes que esperavam os bytes crus
   passam a esperar `turnaround.reference_photo_png(PNG_BYTES)`.

6. Não toque em `sdwebui` nem em `openai_compatible`: eles seguem com
   `portrait_pixels` e o prefixo de semelhança.
```

### Checkpoint 4

- [ ] `pytest tests/test_look_lock.py tests/test_reference_lock.py tests/test_npc_chronicle.py tests/test_likeness.py` exits 0
- [ ] O prompt do retrato de um card com foto contains "exact same adult person shown in the supplied reference photo"
- [ ] As referências do retrato equals as do mestre do 360° do mesmo card
- [ ] Com `current_outfit` "a red raincoat", a lista de referências contains só o recorte de rosto
- [ ] Um card de menor com foto returns lista vazia em `_photo_references`
- [ ] No staging, o retrato de "Marina Teste" shows a mesma pessoa, óculos e camiseta do close-up do 360°
- [ ] No staging, o retrato de "Bruno Teste" shows a mesma barba, cabelo e camisa xadrez do close-up do 360°

## Prompt 5 — Frase encenada, suíte na VM e publicação

```
Feche o trabalho: um defeito de texto no See com nudez, a suíte inteira, a
publicação e a prova visual guardada.

1. `backend/app/routes/images.py`, `_staged_explicit_action`: quando a frase é a
   neutra de reserva e os dois são o sujeito ("Caio and A Donzela da Noite are
   together in the room"), a encenação produzia "his arms raised as he A Donzela
   da Noite are together in the room". Depois de calcular a ação da pessoa nua
   (`_action_core`), se ela começa pelo nome da pessoa vestida
   (`_name_pattern(clothed_name).match(core)`), devolva vazio: o chamador usa
   `_stateful_action`. Teste novo em `tests/test_scene.py`:
   `test_staged_explicit_action_needs_an_action_of_the_nude_person_alone`, com o
   nome escrito "A Donzela" e "the Donzela". O teste antigo da encenação fica
   igual.

2. Suíte. Sincronize a árvore com `rsync -a --delete` para `~/aidungeon-wip` na
   VM, excluindo `.git`, `/data`, `node_modules`, `.venv`, `venv`,
   `__pycache__`, `run_tests.sh` e `test.log`, e rode `./run_tests.sh ../tests`.
   Leia a saída do comando; `test.log` naquela pasta é antigo.

3. Publicação. Commit no `main`, push para `origin` e para
   `ssh://caio@vm.groupabz.com/home/caio/aidungeon.git`. Na VM, em
   `~/aidungeon-git`: `git fetch -q origin` e `tools/deploy.sh --target staging
   --ref <SHA>` com `DEPLOY_RESTART="/home/caio/aidungeon-git/tools/restart_service.sh
   aidungeon-staging 2123"`. Antes da produção (`--target main`, serviço
   `aidungeon`, porta 2122), leia `/api/colab/status` e só siga se não estiver
   em `creating`; não emende a leitura e o deploy num comando só.

4. Prova no staging. Para "Marina Teste" (`e07cf8fde00748059f9b9d6628b4b243`) e
   "Bruno Teste" (`bfe2faf0ca9a41c3ae05bb84698aa069`): `POST
   /api/adventures/{id}/cards/{card}/turnaround` com `{"force": true}`, depois
   `POST .../portrait` com `{"source": "reference"}`. Espere os jobs terminarem
   lendo a tabela `jobs`.

5. Evidência. Monte em `circuito/trabalho/` uma tira por personagem, em JPEG,
   na ordem: foto de corpo inteiro, foto de rosto, ficha do 360° nova, retrato
   novo. E uma tira "antes" com o retrato e a ficha anteriores. Confira a olho
   rosto, cabelo, óculos ou barba e cada peça de roupa.

6. Produção. Refaça retrato e 360° só dos cards com foto enviada da aventura em
   que a falha foi relatada, anotando antes os ids do retrato e do 360°
   anteriores para poder voltar. Um card por vez na placa.
```

### Checkpoint 5

- [ ] `pytest tests/test_scene.py -k staged_explicit_action` exits 0 com os três testes
- [ ] `_staged_explicit_action("Caio and A Donzela da Noite are together in the neon-lit room", ...)` returns string vazia
- [ ] `./run_tests.sh ../tests` na VM exits 0 com zero falhas e nenhum teste a menos que antes
- [ ] O smoke do deploy no staging e na produção shows `GET /api/adventures: HTTP 200` e `integrity=ok`
- [ ] `circuito/trabalho/` contains uma tira "depois" por personagem e uma tira "antes"
- [ ] Na tira da Marina, ficha e retrato novos shows a mesma camiseta "OCEAN CLUB" da foto
- [ ] `node ~/.claude/skills/gauntlet-build/gates/lint-buildplan.mjs BUILD.md` exits 0
- [ ] `/api/colab/status` da produção returns `{"state":"off"}` no fim do trabalho

## Chaves e serviços, pelo prompt que precisa deles primeiro

| Serviço | Primeiro prompt | O que é preciso |
|---|---|---|
| Acesso ssh a `caio@vm.groupabz.com` | 1 (leitura do turno real) | chave ssh já instalada; sem senha de sudo |
| Narrador da VM com visão (porta 1234) | 2 | serviço `llama-server` ligado; um modelo sem mmproj não serve para a prova |
| ComfyUI com FLUX.2 Klein 4B (porta 8188) | 3 | serviço `comfyui` ligado; `flux-2-klein-4b-fp8.safetensors` e `flux2-vae.safetensors` presentes |
| Token de admin do jogo | 3 (jobs no staging) | gerado na VM por `~/kfprobe/stg_api.sh`; não sai da máquina |
| Wan 2.2 TI2V 5B | 5 (giro do 360°) | opcional: sem ele fica o giro montado das vistas |
| Colab | nenhum | não usado; a leitura com o narrador do Colab é coberta só por teste |

## Depois do build

Fica para o gauntlet-harden, se o dono quiser: revisão de segurança e de carga.
Este documento termina no jogo corrigido, testado e publicado.
