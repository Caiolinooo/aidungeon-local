# Brief: simulação de aventura/vida (D20, d100, Sims-like)

Data da leitura: 2026-10-21. Fontes e o que cada uma confirmou: `outputs/rpg-life-sim-research.provenance.md`. Afirmação sem página ou trecho lido nesta sessão: `[INFERENCE]`.

## D&D 5e SRD (d20 vs CD/CA, vantagem)

O SRD 5.2 trata perícia, salvaguarda e ataque como teste d20: rola-se 1d20 (alto é melhor); somam-se o modificador de habilidade, o bônus de proficiência quando cabe, e bônus ou penalidades de circunstância. Sucesso se o total é maior ou igual ao alvo. O alvo de perícia ou salvaguarda é a Classe de Dificuldade (CD); o de ataque é a Classe de Armadura (CA). A CA base é 10 + modificador de Destreza. Vantagem usa dois d20 e fica o maior; desvantagem fica o menor. Várias vantagens não empilham; vantagem e desvantagem no mesmo teste cancelam. No ataque, 20 natural acerta (acerto crítico) e 1 natural erra, quaisquer que sejam os modificadores. Fonte: “Playing the Game” / “D20 Tests” do SRD 5.2 (opengamingnetwork e 5e24srd; o PDF em dndbeyond repete o mesmo procedimento).

A tabela de point-buy 8=0 … 13=5, 14=7, 15=9 e orçamento 27 é a do PHB 2014 e a exigida por este plano. A página do SRD 5.2 lida aqui descreve o teste e a CA, não essa tabela. `[INFERENCE]` quanto ao texto exato da tabela no PDF 5.2.

No projeto, `backend/app/stats.py` só monta a ficha d20 (modificador `(valor-10)//2`, arquétipos). O custo point-buy é linear (`valor-8`), então 14 custa 6 e 15 custa 7. Não há resolução de ataque no servidor. O dado do painel (`GamePanel.jsx`) usa `Math.random`.

Ação deste plano: pack `d20` (D&D 5e e Tormenta20), dados e PV/CA/XP/ouro no servidor, antes da narração. Corrigir a tabela de point-buy.

## Call of Cthulhu 7e (d100, graus, Sanidade)

Chaosium (wiki “The Game System”, página lida): sucesso regular é 1d100 menor ou igual à perícia; difícil (Hard) é metade; extremo é um quinto. Rolagens opostas comparam o grau; empate de grau vai para a perícia maior, e perícias iguais desempatam no d100 menor. Dado de bônus/penalidade troca o dado das dezenas (menor ou maior). Empurrar uma falha rola de novo com consequência pior se falhar outra vez. Sorte é d100 contra a Sorte atual, não contra perícia.

Sanidade (TRPGLine e wiki “Sanity” / atributos secundários): SAN inicial igual a POW. O teste é d100 menor ou igual à SAN atual, sem dado de bônus/penalidade. A perda publicada vem em par (ex.: 0/1d6): o primeiro valor se passar, o segundo se falhar. Perda de 5 ou mais num único teste pede um teste de INT: sucesso é insanidade temporária (1d10 horas); falha suprime a memória. Fumble no teste de SAN perde o máximo daquela fonte (TRPGLine). Crítico de perícia é 01; desastre é 100, ou 96–100 se a perícia é menor que 50 (vídeo ABCs of RPGs, não o wiki; o wiki lido não fixa o 96). `[INFERENCE]` no limiar 96 se só o vídeo for a fonte — o plano adota esse limiar de qualquer forma.

No projeto não há pack d100, SAN nem graus de sucesso. O ruleset `coc-7e`, se existir, é prosa.

Ação: pack `d100` com graus (extremo / difícil / regular), crítico 1, desastre 100 ou ≥96 se a base < 50, e teste de SAN com o par de perda.

## FATE Core

Fate SRD e Evil Hat: rolam-se 4dF. Cada dado vale −1, 0 ou +1 (dois lados de cada no d6). O total vai de −4 a +4, quase sempre entre −2 e +2. Soma-se o valor da perícia e compara-se à dificuldade na escada de adjetivos (+8 Legendary … −2 Terrible, e abaixo/acima). Zero ou mais de margem (shifts) é sucesso. Evil Hat trata sucesso com estilo como margem alta; Gaming Ballistic explicita sucesso com estilo em 3+ shifts e empate no zero. 4dF é a curva padrão; d6−d6 é alternativa do mesmo artigo da Evil Hat, não a do SRD lido.

No projeto, FATE fica em prosa (`rulesets`). Não há rolagem 4dF no servidor. O cliente só tem dN uniforme.

Ação: nenhuma resolução FATE no motor. `POST /sim/roll` com `4dF` cobre a rolagem. Narrativa continua no ruleset.

## GURPS (3d6 roll-under)

GURPS Lite 4e (HTML lido) e o resumo de B347: teste de sucesso é 3d6 menor ou igual à perícia ou atributo efetivo. Maior o alvo, mais fácil. Margem é a diferença entre alvo e rolagem. 3 ou 4 é sempre sucesso crítico; 5 é crítico se a perícia efetiva é 15+; 6 se é 16+. 18 é sempre falha crítica; 17 é crítica se a perícia é 15 ou menos; qualquer rolagem 10 ou mais acima do alvo também é falha crítica. O FAQ da Steve Jackson confirma que 3–4 ainda é crítico em testes de resistência mesmo com alvo baixo, e que perícia abaixo de 3 torna o feito impossível fora desses casos. Gaming Ballistic resume o mesmo roll-under e o par ataque/defesa.

No projeto, GURPS é prosa. Não há 3d6 roll-under no motor.

Ação: igual a FATE — só `3d6` em `POST /sim/roll`. Sem aplicar margem, crítico ou PV.

## Savage Worlds

Test Drive SWADE 2020 (PDF da Pinnacle, trechos lidos): atributo ou perícia é um dado de traço de d4 a d12. O teste soma modificadores e compara ao número-alvo, em geral 4. Cada 4 pontos acima do alvo é um raise. Wild Card rola também um d6 (Wild Die) e fica o maior, não a soma. 1 no traço e no Wild Die é falha crítica. Sem a perícia, rola-se d4−2. Aces: o máximo do dado explode, rola de novo e soma, sem teto. Extras têm Parry 2 + metade do dado de Fighting e Toughness 2 + metade de Vigor, mais armadura.

No projeto, Savage Worlds é prosa. Não há dado aberto nem Wild Die.

Ação: sem motor. A UI oferece o campo de dado livre; explosão e Wild Die não são simulados. `[INFERENCE]` se o jogador quiser o ace, terá de somar duas rolagens à mão.

## FATAL (tabelas densas — não copiar)

Wikipedia (en, “F.A.T.A.L.”, lida): RPG de fantasia sombria da Fatal Games (Byron Hall; 1ª ed. 2002). O sistema é simulacionista, com cerca de vinte atributos rolados (4d100/2−1 na 1ª ed.; 10d100/5−1 na 2ª), traços que o jogador normalmente escolheria decididos na rolagem, e tabelas extensas inclusive para o corpo. A recepção é amplamente negativa. A resenha de Darren MacLennan e Jason Sartin na RPGnet (2003) chama o jogo de “Necronomicon of role-playing games”, “fundamentally broken in its attitude towards sexuality” e fala em misoginia. Há crítica também de racismo e homofobia. Regras de violência sexual entram no combate; sexo consensual não é tratado. A resposta dos autores (Hall e “Burnout”) não mudou o consenso.

Uso aqui: exemplo do que não fazer. Simulação por tabela densa, sem escolha do jogador e com conteúdo sexual gráfico, foi rejeitada. Este plano não copia tabela, texto nem tema. O Modo Vida fica em seis necessidades, relógio e relações, com o jogador no comando da própria ação.

## The Sims 4 e inZOI

Manual de The Sims 4 (PS4, EA, PDF lido): seis necessidades — Bladder, Fun, Hunger, Social, Energy, Hygiene. Só a fome mata direto. Verde é seguro, amarelo pede atenção, vermelho altera humor e comportamento. Fome baixa deixa desconfortável até a inanição; bexiga baixa pode humilhar; energia baixa leva a desmaiar; diversão baixa deixa tenso; social baixa deixa triste; higiene baixa atrapalha o social e o trabalho. Sims cuidam de si quando podem. A wiki de Motive detalha moodlets (Hungry, Has To Pee, Tired, Lonely, Grungy, Needs Amusement) com limiares em torno de 5–20% em alguns casos.

inZOI (inzoitips e wiki Live Mode, lidos): oito necessidades que decaem — Hunger, Sleep, Hygiene, Bathroom, Fun, Social, Recognition, Energy/Vigor. Fome, sono ou reconhecimento zerados podem matar; social muito baixo pode fazer o Zoi ir embora. O humor tem muitas emoções ao mesmo tempo; a mais intensa fica ativa e muda o aprendizado. Relações sobem ou descem por conversa (amizade, romance, trabalho, família nas páginas de Interactions). O dia de Live Mode tem relógio configurável (48 min reais por dia na wiki) e estações a cada 7 dias.

No projeto, o life-sim atual (migration 024) guarda memória e humor de NPC e um `life_day`. Não há relógio hora/estação, barras do jogador nem relação NPC–NPC.

Ação: Modo Vida opcional, seis necessidades no estilo Sims (fome, energia, higiene, social, diversão, bexiga), humor por média, rotina diária e relação por co-localização. Sem morte por fome e sem Recognition da inZOI.

## RimWorld e Dwarf Fortress

RimWorld Wiki (Needs, Mood, Social, Recreation, lidas): todo humanoide tem Food, Rest e Mood. Colono também tem Recreation e, em geral, Beauty, Comfort e Outdoors. Fome vazia vira desnutrição e morte; descanso vazio faz dormir no lugar. Recreation baixa dá pensamentos de −5/−10/−20; variedade importa porque a mesma atividade gera tolerância. Mood é a soma de pensamentos, traços e ambiente; abaixo do limiar, mental break. Social: opinião de −100 a −20 é rival, cerca de −20 a +20 conhecido, +20 a +100 amigo. Opinião baixa gera insulto e briga; romance exige opinião mínima. Família altera opinião e o luto.

Dwarf Fortress Wiki (Personality need e Relationship, lidas): cada anão tem necessidades ligadas a facetas (socializar, criar, rezar, lutar, estar com amigos, romance, etc.). Não cumprir vira estresse. Relações nascem do tempo juntos; amigos e família dão pensamento feliz, morte deles dá infelicidade. A história emerge desses estados, não de um roteiro.

No projeto, humor de NPC existe como texto regenerado, não como barra nem grafo de relação.

Ação: copiar o princípio (necessidade → humor → conduta; relação emerge de estar no mesmo lugar), não as dezenas de needs do DF nem o mental break do RimWorld. Teto de +6 por dia no par evita amizade instantânea.

## Lorebooks e jogos com regra (AI Dungeon, NovelAI, SillyTavern, AI Roguelite, Friends & Fables)

NovelAI Docs e SillyTavern Docs (lidos): Lorebook / World Info injeta texto quando chaves aparecem no trecho recente. Há entrada sempre ligada, ordem, orçamento de tokens e, na SillyTavern, recursão. Não rolam dado nem gravam PV. O guia LoreWeaver (secundário, 2026) descreve o mesmo para Story Cards do AI Dungeon: gatilho por palavra, sem estado mecânico. `[INFERENCE]` no detalhe atual da UI do AI Dungeon, porque a doc oficial da Latitude não foi aberta nesta sessão.

Friends & Fables (fables.gg, artigo lido) faz o oposto: estado estruturado (ficha, inventário, espaços de magia, mapa) é a verdade; o LLM narra em cima. O exemplo deles é Fireball recusado sem espaço de magia. Combate tem ordem de turno. AI Roguelite (página Steam lida) gera lugar, item e texto com LLM, mas a ação passa por plausibilidade e por rolagem de atributo/perícia; o Steam avisa que falha de teste é regra, não censura. Um guia da comunidade cita necessidades que caem e itens que se gastam. NOPOTIONS (página “how it works”, lida à parte) separa narrador e motor d20 — mesmo desenho deste plano, não é um dos cinco produtos pedidos.

No projeto, cards e memória entram no prompt como texto. `action_check.py` pede a um LLM se a ação é possível; não rola dado nem altera PV. O narrador pode inventar o desfecho.

Ação: motor autoritativo. O servidor decide o resultado, grava PV/itens/ouro e entrega ao narrador um bloco que é o único desfecho. Ação impossível (sem item, sem ouro, alvo morto, personagem caído) recusa antes dos tokens. Lorebooks continuam como estão.

## Mecânica → quem faz → estado → ação

| Mecânica | Quem faz | Estado no projeto | Ação (este plano) |
|---|---|---|---|
| d20 vs CD/CA, vantagem, 1/20 natural | D&D 5e SRD | Ficha em `stats.py`; sem rolagem de ataque | Pack `d20`; servidor rola e aplica |
| Point-buy 8–15 (14=7, 15=9) | PHB 5e | Custo linear em `stats.py` e `CharacterModal.jsx` | Tabela padrão |
| d100, graus, SAN em par | CoC 7e / BRP | Ausente (prosa se houver ruleset) | Pack `d100` + teste de SAN |
| 4dF + escada | FATE Core | Prosa | Só `POST /sim/roll` `4dF` |
| 3d6 roll-under, crítico 3–4 / 17–18 | GURPS 4e | Prosa | Só `POST /sim/roll` `3d6` |
| Traço + Wild Die, TN 4, aces | Savage Worlds | Prosa | Sem motor; dado livre sem explosão |
| Tabelas densas de corpo/criação | FATAL | Ausente | Não copiar |
| 6 needs, humor, rotina | The Sims 4 | `life_day` e memória de NPC (024) | Modo Vida opcional |
| 8 needs, emoção, relações | inZOI | Ausente | Só o que couber nas 6 needs e no relógio |
| Needs + opinião + mental break | RimWorld | Humor textual de NPC | Humor por média; relação −100..100 |
| Needs de personalidade, relação por convivência | Dwarf Fortress | Ausente | Rotina + co-localização, teto +6/dia |
| Lorebook por palavra | AI Dungeon, NovelAI, SillyTavern | Cards + memória no prompt | Manter; não substitui regra |
| Estado estruturado + regra antes da prosa | Friends & Fables; AI Roguelite (rolagem) | `action_check` é veredito LLM; dado no cliente | Motor autoritativo no turno |
| Recência da memória vs similaridade | — | `_recency` soma `seq_end` cru ao score | Normalizar, peso 50 |
| Apagar aventura | — | `delete_adventure` não limpa `npc_memories` | Incluir a tabela |
