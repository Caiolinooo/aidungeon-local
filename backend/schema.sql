-- AI Dungeon Local — schema do banco (SQLite, aidungeon.db)
-- Fonte: PLANO.md §4.1 (NÃO alterar campos sem atualizar o plano)

CREATE TABLE scenarios(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', opening_text TEXT DEFAULT '',
  image_style TEXT DEFAULT '',
  tags TEXT DEFAULT '[]', third_person INTEGER DEFAULT 0,
  story_cards TEXT DEFAULT '[]',       -- v2: cards do rascunho do assistente (JSON)
  created_at TEXT, updated_at TEXT);

CREATE TABLE adventures(
  id TEXT PRIMARY KEY, scenario_id TEXT REFERENCES scenarios(id),
  title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', story_summary TEXT DEFAULT '',
  tags TEXT DEFAULT '[]', third_person INTEGER DEFAULT 0,
  source TEXT DEFAULT 'new',            -- 'new' | 'import'
  ui_profile TEXT DEFAULT '',           -- v0.7.0: manifesto JSON de controles (IA)
  ui_state TEXT DEFAULT '',             -- v0.7.0: estado JSON dos widgets
  created_at TEXT, updated_at TEXT);

CREATE TABLE actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  seq INTEGER NOT NULL,                  -- ordem absoluta (não o id do backup!)
  type TEXT NOT NULL CHECK(type IN ('start','continue','do','say','story','see')),
  text TEXT NOT NULL, created_at TEXT, edited_at TEXT,
  UNIQUE(adventure_id, seq));

CREATE TABLE story_cards(
  id TEXT PRIMARY KEY, adventure_id TEXT NOT NULL REFERENCES adventures(id),
  title TEXT DEFAULT '', type TEXT DEFAULT 'other',
  triggers TEXT DEFAULT '[]',            -- JSON array de palavras-chave
  entry TEXT DEFAULT '', pinned INTEGER DEFAULT 0,
  use_for_character_creation INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);

CREATE TABLE memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  text TEXT NOT NULL, seq_start INTEGER, seq_end INTEGER,
  embedding BLOB,                        -- fase 4 (pode ser NULL)
  embedding_model TEXT,                  -- fase 4: nome do modelo (ex.: Qwen3-Embedding-0.6B)
  embedding_dim INTEGER,                 -- fase 4: dimensão do vetor (ex.: 1024)
  created_at TEXT);

CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);

-- v2 (2026-10-08): imagens geradas pela ação "See"
CREATE TABLE images(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  action_seq INTEGER NOT NULL,
  prompt TEXT DEFAULT '',
  negative_prompt TEXT DEFAULT '',
  file_path TEXT NOT NULL,
  provider TEXT DEFAULT '',
  width INTEGER,
  height INTEGER,
  created_at TEXT);

-- v2: estilo visual da aventura (vem de state.imageStyle no backup)
ALTER TABLE adventures ADD COLUMN image_style TEXT DEFAULT '';

-- v0.3.0 (2026-10-08): assets por NPC nos story cards
ALTER TABLE story_cards ADD COLUMN portrait_image_id TEXT;
ALTER TABLE story_cards ADD COLUMN portrait_prompt TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN voice TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN vrm_id TEXT DEFAULT '';

-- v0.5.0 (2026-10-09): ficha estruturada do NPC (JSON: appearance, body,
-- clothing, personality, stats, notes) — alimenta retratos e a aba NPCs
ALTER TABLE story_cards ADD COLUMN sheet TEXT DEFAULT '';

-- Retrato de identidade: a primeira foto do NPC. Gerações seguintes (img2img)
-- partem deste arquivo; geração automática não substitui portrait_image_id.
ALTER TABLE story_cards ADD COLUMN portrait_anchor_id TEXT;

-- v0.3.0: kind='portrait' + ref_id=card_id para retratos (action_seq=-1: retrato
-- não pertence ao feed de ações)
ALTER TABLE images ADD COLUMN kind TEXT DEFAULT 'scene';
ALTER TABLE images ADD COLUMN ref_id TEXT DEFAULT '';

-- v0.3.0: fila do pipeline assíncrono de assets (worker em app/jobs.py)
CREATE TABLE jobs(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  type TEXT NOT NULL,                -- 'character_portrait'
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|canceled
  ref_id TEXT DEFAULT '',            -- story_card alvo
  payload TEXT DEFAULT '{}',         -- JSON de entrada
  result TEXT,                       -- JSON de saída (ex.: {"image": {...}})
  error TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT);

-- Donos: Caio é o admin. Cada aventura pertence a um usuário.
CREATE TABLE users(
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT);
ALTER TABLE adventures ADD COLUMN owner TEXT DEFAULT '';

-- --- Grok (G5): jobs com prioridade e chave de substituição (C5, migration 021)
ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 2;
ALTER TABLE jobs ADD COLUMN supersede_key TEXT DEFAULT '';
CREATE INDEX jobs_queue ON jobs(status, priority, created_at);

-- Compartilhar aventura e turno (migration 022). Banco novo não roda o
-- arquivo de migration: o init marca os números depois deste script.
CREATE TABLE adventure_shares(
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  created_at TEXT,
  PRIMARY KEY (adventure_id, username));
ALTER TABLE adventures ADD COLUMN turn_mode INTEGER DEFAULT 0;
ALTER TABLE adventures ADD COLUMN turn_user TEXT DEFAULT '';

-- Multiplayer ao vivo (migration 023): ator da ação, personagem do jogador
-- e regras de criação herdadas pela aventura.
ALTER TABLE actions ADD COLUMN actor TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN player_username TEXT DEFAULT '';
ALTER TABLE adventures ADD COLUMN character_rules TEXT DEFAULT '';

-- Life-sim (migration 024): memórias do NPC e o dia da aventura.
CREATE TABLE IF NOT EXISTS npc_memories (
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL,
  card_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'event',
  text TEXT NOT NULL,
  mood TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npc_memories_card ON npc_memories(adventure_id, card_id, created_at);
ALTER TABLE adventures ADD COLUMN life_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adventures ADD COLUMN life_tick_seq INTEGER NOT NULL DEFAULT 0;

-- Rulesets de personagem (migration 025).
-- Banco novo não roda o arquivo de migration: o mesmo DDL e os builtins ficam aqui.
-- O runner de migrations parte no ';', então o texto das regras não pode conter ponto e vírgula.

CREATE TABLE IF NOT EXISTS rulesets(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT DEFAULT '',
  rules TEXT NOT NULL,
  builtin INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT);

INSERT OR IGNORE INTO rulesets(id, name, description, rules, builtin, created_at, updated_at) VALUES(
  'dnd-5e',
  'D&D 5e',
  'Fantasia heroica com classes, níveis e o d20.',
  'Criação de personagem. Atributos: Força, Destreza, Constituição, Inteligência, Sabedoria e Carisma. Role 4d6 descartando o menor, ou use o array 15, 14, 13, 12, 10 e 8. Escolha ancestralidade, classe e antecedente. Pontos de vida no nível 1: máximo do dado de vida mais Constituição. Proficiência inicial +2. Perícia treinada soma atributo e proficiência. Classe de Armadura depende de armadura, escudo e Destreza.

Testes. Role 1d20 mais o modificador contra a Classe de Dificuldade. Igual ou maior é sucesso. Em ataques, 1 natural falha e 20 natural é crítico com dados de dano em dobro. Vantagem fica com o maior de dois d20. Desvantagem fica com o menor.

Combate. Iniciativa é 1d20 mais Destreza. No turno: deslocamento, uma ação e às vezes ação bônus. O ataque acerta contra a Classe de Armadura. O dano é o dado da arma mais o modificador. A zero pontos de vida o personagem cai.

Magia. Espaços de magia voltam no descanso longo. A CD de resistência é 8 mais proficiência mais o atributo de conjuração. Concentração sob dano exige Constituição, CD 10 ou metade do dano. Truques não gastam espaço.

Morte. A zero pontos de vida, role 1d20 a cada turno. Dez ou mais é sucesso. Três sucessos estabilizam. Três falhas matam. Um 20 natural recupera 1 ponto de vida. Dano massivo igual ao máximo de pontos de vida mata na hora.',
  1,
  '2026-10-20T00:00:00.000Z',
  '2026-10-20T00:00:00.000Z');

INSERT OR IGNORE INTO rulesets(id, name, description, rules, builtin, created_at, updated_at) VALUES(
  'tormenta20',
  'Tormenta20',
  'Fantasia medieval com mana, perícias e o d20.',
  'Criação de personagem. Seis atributos: Força, Destreza, Constituição, Inteligência, Sabedoria e Carisma. Distribua pontos ou role 4d6 descartando o menor. Escolha raça, classe e origem. Pontos de vida iniciais vêm da classe mais Constituição. Pontos de mana vêm da classe mais o atributo de conjuração. Perícias treinam com pontos do nível 1 e sobem conforme o nível. Defesa é 10 mais Destreza e armadura, limitada pela carga.

Testes. Role 1d20 mais o bônus da perícia ou do atributo contra a CD. Igual ou maior é sucesso. Um 1 natural falha e um 20 natural é crítico, com efeito dobrado quando o narrador confirmar o acerto.

Combate. Iniciativa é 1d20 mais Destreza. No turno o personagem se move e faz uma ação padrão, e pode ter ação de movimento extra e reação. Ataque contra a Defesa do alvo. Dano soma o dado da arma ao modificador. Condições como sangrando e desprevenido alteram a cena.

Magia. Magias gastam pontos de mana. A CD de resistência é 10 mais metade do nível mais o modificador de conjuração. Magias sustentadas exigem concentração e caem se o conjurador ficar inconsciente. Truques e magias simples gastam pouco mana.

Morte. A zero pontos de vida o personagem fica morrendo e faz um teste de Constituição por turno. Três falhas matam. Dano que leve os pontos de vida a um valor negativo igual ao máximo mata na hora. Cura antes da morte estabiliza e tira o personagem de morrendo.',
  1,
  '2026-10-20T00:00:00.000Z',
  '2026-10-20T00:00:00.000Z');

INSERT OR IGNORE INTO rulesets(id, name, description, rules, builtin, created_at, updated_at) VALUES(
  'gurps',
  'GURPS',
  'Sistema genérico universal por pontos e 3d6.',
  'Criação de personagem. Quatro atributos: Força (ST), Destreza (DX), Inteligência (IQ) e Saúde (HT), cada um começando em 10. A campanha dá um orçamento de pontos, em geral 100 a 250. Atributos, vantagens, desvantagens e perícias custam ou devolvem pontos. Perícias têm nível baseado no atributo e no investimento. Escolha um conceito e compre o que a história precisa, sem ultrapassar o limite da mesa.

Testes. Role 3d6 e compare com o nível da perícia ou do atributo. Igual ou menor é sucesso. A margem é a diferença entre o nível e o resultado. Falha crítica ocorre com 18, ou 17 quando o nível é baixo. Sucesso crítico ocorre com 3 ou 4.

Combate. A ordem alterna por manobra. Cada turno escolhe uma manobra como ataque, defesa total ou movimento. Ataque é um teste da perícia de arma. Defesa é esquiva, bloqueio ou aparo. O dano depende da Força e da arma. A resistência a dano (RD) da armadura reduz o dano antes dos pontos de vida.

Magia. Magias são perícias de colégio. Lançar gasta fadiga conforme o custo da magia e exige um teste da perícia. Manter uma magia custa fadiga por minuto. Falha crítica em magia pode produzir um efeito adverso definido pelo narrador.

Morte. A zero pontos de vida o personagem cai e faz testes de Saúde para não morrer. Em pontos de vida negativos iguais ao máximo, a morte é quase certa sem ajuda imediata. Em menos cinco vezes os pontos de vida máximos a morte é instantânea. Cura e Primeiros Socorros estabilizam.',
  1,
  '2026-10-20T00:00:00.000Z',
  '2026-10-20T00:00:00.000Z');

INSERT OR IGNORE INTO rulesets(id, name, description, rules, builtin, created_at, updated_at) VALUES(
  'coc-7e',
  'Call of Cthulhu 7e',
  'Horror investigativo com perícias percentuais e Sanidade.',
  'Criação de personagem. Oito características: Força, Constituição, Tamanho, Destreza, Aparência, Inteligência, Poder e Educação. Role os dados indicados na ficha ou use um array da mesa. Escolha ocupação. Os pontos de perícia da ocupação saem de Educação, e os de interesse pessoal saem de Inteligência. Pontos de vida são a média de Constituição e Tamanho. Sanidade inicial é igual ao Poder. Pontos de magia são um quinto do Poder.

Testes. Role 1d100 e compare com a perícia. Igual ou menor é sucesso. Metade do valor é sucesso difícil. Um quinto é sucesso extremo. 01 é crítico. Falha acima da perícia, e 96 ou mais pode ser desastre quando a perícia é baixa. Teste oposto compara o nível de sucesso.

Combate. A iniciativa segue Destreza. No turno o investigador ataca, esquiva ou faz outra ação. Ataque de luta ou arma de fogo é um teste da perícia. Dano é o dado da arma mais o bônus de dano do corpo. Uma ferida grave ocorre quando um golpe tira metade ou mais dos pontos de vida cheios.

Magia. Grimórios e feitiços gastam pontos de magia e muitas vezes Sanidade. Ler um tomo pede teste e tempo. Lançar um feitiço é um teste de Poder ou da perícia indicada. O custo sai dos pontos de magia, que voltam com o descanso.

Morte e loucura. A zero pontos de vida o investigador está morrendo e morre se não for socorrido. Sanidade zero é loucura indefinida. Um choque que tire 5 ou mais de Sanidade pede um teste de insanidade. Falhar causa uma loucura temporária que o narrador descreve.',
  1,
  '2026-10-20T00:00:00.000Z',
  '2026-10-20T00:00:00.000Z');

INSERT OR IGNORE INTO rulesets(id, name, description, rules, builtin, created_at, updated_at) VALUES(
  'fate-core',
  'FATE Core',
  'Narrativa movida por aspectos, dados de fada e estresse.',
  'Criação de personagem. Defina um conceito, um problema e aspectos que descrevem quem a pessoa é. Escolha abordagens ou perícias numa pirâmide, do melhor para o mediano. Compre proezas que mudam uma regra a seu favor. O estresse tem caixas físicas e mentais. Consequências leve, moderada e grave absorvem dano maior e ficam na ficha até serem tratadas. O jogador escreve aspectos que possam ser invocados e compelidos.

Testes. Role quatro dados de fada, some o resultado de menos 4 a mais 4 e acrescente a perícia. Compare com a oposição, ativa ou passiva. Empate favorece quem resiste, ou gera um sucesso com custo. Sucesso faz o que queria. Sucesso com estilo, três ou mais acima, gera um fato extra ou um impulso.

Combate e conflito. Cada troca os personagens agem em ordem de iniciativa. As ações são superar, criar vantagem, atacar e defender. Ataque bem sucedido causa estresse igual à diferença. Estresse cheio empurra o dano para uma consequência. Sem caixa livre o personagem está fora de cena.

Magia. Trate magia como perícia, proeza ou extra da mesa. O custo aparece como aspecto, pontos de destino ou uma consequência. Invocar um aspecto de magia gasta um ponto de destino ou uma invocação gratuita criada antes.

Derrota. Não há morte automática. Fora de cena significa capturado, fugido ou incapacitado, conforme a história. Concessão antes da rolagem final evita o pior resultado e rende pontos de destino. Morte só ocorre se a mesa aceitar e o aspecto de consequência justificar.',
  1,
  '2026-10-20T00:00:00.000Z',
  '2026-10-20T00:00:00.000Z');

INSERT OR IGNORE INTO rulesets(id, name, description, rules, builtin, created_at, updated_at) VALUES(
  'savage-worlds',
  'Savage Worlds',
  'Ação cinematográfica com dados de traço, vantagens e complicações.',
  'Criação de personagem. Atributos e perícias usam dados de d4 a d12. O herói começa com Agilidade, Astúcia, Espírito, Força e Vigor em d4 e sobe gastando pontos. Perícias custam um ponto até o atributo e dois acima dele. Escolha vantagens e complicações. Complicações devolvem pontos para mais vantagens. Pontos de vida não existem: o dano vira condições. O dado curinga é rolado junto com o dado de traço e vale o maior.

Testes. Role o dado do traço e o dado curinga. O maior resultado conta. 4 ou mais é sucesso. Cada 4 acima disso é um aumento. 1 no dado de traço e no curinga é falha crítica. Bônus e penalidades somam ou subtraem do resultado, e o narrador pode pedir um teste oposto.

Combate. Iniciativa é uma carta de baralho, e o ás age antes. No turno o personagem se move e faz uma ação. Ataque é um teste de luta ou tiro contra 4, com penalidade se o alvo se defende. Dano que iguala ou supera a resistência deixa o alvo abalado. Dano maior causa ferimentos. Três ferimentos incapacitam.

Poderes. Poderes gastam pontos de poder e pedem um teste da perícia de foco, como conjuração ou fé. Falha gasta um ponto. Sucesso aplica o efeito. Um abalado pode interromper a manutenção de um poder contínuo.

Morte. Incapacitado pede um teste de Vigor. Falha deixa o herói sangrando e fora da cena. Sucesso deixa abalado e vulnerável. Complicação ou segundo golpe enquanto sangra pode matar, se a mesa quiser consequência permanente. Cura estabiliza e remove a condição de sangrando.',
  1,
  '2026-10-20T00:00:00.000Z',
  '2026-10-20T00:00:00.000Z');


