Corrija no aidungeon-local as divergências de imagem dos personagens: o retrato, o retrato-mestre de corpo inteiro e o turnaround 360° de um personagem com foto de referência têm de ser uma reconstrução fiel dessa foto (mesmo rosto, cabelo, cor e comprimento, sardas, acessórios e roupa que a foto mostra), e não outra pessoa com outra roupa. Corrija também o See com nudez: a nudez tem de ir para o personagem que a narração despiu, nunca para o personagem do jogador (Caio) quando ele está vestido na história. Entregue as correções testadas e publicadas no staging e na produção da VM, mais um `BUILD.md` no formato do gauntlet-build com a sequência de prompts e checkpoints usada.

PRIORIDADES (nesta ordem, quando entrarem em conflito)
1. Nudez e roupa atribuídas à pessoa certa: ninguém aparece nu no See sem a narração ter dito isso dele.
2. Fidelidade à foto de referência no retrato, no retrato-mestre e no 360° (rosto, cabelo, acessórios e roupa da foto).
3. Coerência entre retrato e 360° do mesmo personagem: os dois partem da mesma foto e mostram a mesma pessoa e a mesma roupa.
4. Não gastar créditos do Colab nem atrapalhar o jogo que já roda na VM.

RESTRIÇÕES
- Não usar o Colab: não ligar sessão nem rodar nada lá, para não gastar créditos.
- Não afrouxar a recusa de imagem com menor de idade nu ou em cena explícita.
- Não testar com as fotos reais dos cards do dono: usar os personagens sintéticos do staging ("Marina Teste" e "Bruno Teste").
- Não mudar as configurações de runtime da produção; sondas que mudam configuração rodam só no staging (porta 2123).
- Não rodar a suíte inteira na máquina local: ela trava. A suíte roda na VM.
- Não mexer no serviço de voz da ABZ nem em `~/sd-webui` e `~/sd-webui-forge-neo` na VM.

ALVO
- VM `vm.groupabz.com` (Linux, GPU L4 de 23 GB dividida entre narrador e ComfyUI), ComfyUI com FLUX.2 Klein 4B distilled (`engine=flux2_klein`), até 4 referências de identidade por imagem.
- Backend FastAPI em `backend/app`, frontend React/Vite em `frontend/src`, testes pytest em `tests/`.
- A suíte inteira continua passando na VM (`./run_tests.sh ../tests` em `~/aidungeon-wip`), sem teste removido.
- Cadeia do turnaround continua com no máximo duas inferências de imagem por personagem.

CENA
- Na ficha do personagem, o retrato e as vistas do 360° mostram a mesma pessoa da foto de referência: mesmo rosto, mesmo cabelo, mesmos acessórios, mesma roupa.
- No See de uma cena com duas pessoas em que a narração despe só uma, a imagem e o prompt copiado trazem a nudez nessa pessoa e a roupa na outra.
- O prompt copiado do See nunca diz que o personagem do jogador está nu quando a narração não disse.

CONTROLES
Botão See: pinta o momento atual com o elenco da cena. Botão de refazer o retrato e o 360° na ficha do personagem: regenera a partir da foto de referência. Copiar prompt: mostra o texto enviado ao pintor.

VERIFICAÇÃO
- Entrada: narração em que só a NPC se despe diante do jogador vestido. Resultado: o prompt do See atribui a nudez à NPC e roupa ao jogador (teste pytest novo).
- Entrada: narração em que ninguém se despe. Resultado: o prompt do See não contém nudez para nenhum personagem (teste pytest).
- Entrada: card com foto de referência. Resultado: os prompts e o grafo do retrato-mestre e da ficha de vistas levam a foto original como referência e não inventam roupa nem cabelo diferentes dos da foto (teste pytest novo).
- Entrada: "Marina Teste" e "Bruno Teste" no staging, com retrato e 360° refeitos. Resultado: as imagens salvas em `circuito/trabalho/` mostram a mesma pessoa e a mesma roupa da foto de referência, conferidas a olho.
- Entrada: `./run_tests.sh ../tests` na VM. Resultado: zero falhas.
- Entrada: `gates/lint-buildplan.mjs BUILD.md` do gauntlet-build. Resultado: saída 0.

PRONTO QUANDO: todas as verificações acima passam, o commit está no `main`, publicado no staging e na produção da VM, e ficam entregues o código corrigido, os testes novos, `BUILD.md` e as imagens de prova em `circuito/trabalho/`.

AMBIENTE
Ferramentas presentes: python3, node, npm, git, ssh, rsync, ffmpeg. Evidências em `circuito/trabalho/`. Rede: acesso por ssh à VM `caio@vm.groupabz.com`; Colab proibido. O dono volta amanhã: não há quem responda perguntas durante a execução.
