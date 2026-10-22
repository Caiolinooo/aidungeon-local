import http from 'http';
import fs from 'fs';
import path from 'path';
import puppeteer from 'puppeteer-core';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '../..');
const distDir = path.join(rootDir, 'frontend', 'dist');

const targetDir = process.argv[2] || 'after';
const outDir = path.join(rootDir, 'docs', 'screenshots', targetDir);
if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

const mimeTypes = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.webmanifest': 'application/manifest+json',
  '.wasm': 'application/wasm',
};

const mockAdventure = {
  id: 'efab03aeeadc41d2953dc2da6a81d63d',
  title: 'The Forgotten Catacombs',
  character_name: 'Kaelen',
  excerpt: 'You venture into the dark subterranean crypts beneath the citadel...',
  action_count: 24,
  updated_at: new Date().toISOString(),
  actions: [
    {
      id: '1',
      seq: 1,
      action_number: 1,
      type: 'story',
      text: 'You find yourself standing before the imposing iron gates of the ancient catacombs. Torches flicker along the weathered granite walls, casting long, dancing shadows into the abyss.\n\nBeside you, Lyra Silverthorne adjusts the strap of her quiver, her keen eyes scanning the runic archway.',
      created_at: new Date(Date.now() - 3600000).toISOString()
    },
    {
      id: '2',
      seq: 2,
      action_number: 2,
      type: 'do',
      text: '\n> You inspect the ancient runic seal etched across the heavy iron threshold.\n',
      created_at: new Date(Date.now() - 1800000).toISOString()
    },
    {
      id: '3',
      seq: 3,
      action_number: 3,
      type: 'continue',
      text: 'Tracing your fingers along the silver inlay, the runes faintly hum with forgotten resonance. An inscription warns: "He who wakes the dormant will inherit the silence."\n\n"Careful," Lyra whispers, drawing a curved silver dagger. "The ward is still active, and something on the other side just heard us."',
      created_at: new Date().toISOString()
    }
  ],
  cards: [
    {
      id: 'c1',
      title: 'Ancient Runic Seal',
      type: 'concept',
      triggers: ['rune', 'seal', 'inscription'],
      value: 'Wards containing the shadow entities of the Nether Era.',
      entry: 'Wards containing the shadow entities of the Nether Era.',
      pinned: true
    },
    {
      id: 'c2',
      title: 'Lyra Silverthorne',
      type: 'character',
      triggers: ['lyra', 'elf', 'rogue'],
      value: 'A nimble elven scout with sharp senses and dual curved silver daggers.',
      entry: 'A nimble elven scout with sharp senses and dual curved silver daggers.',
      pinned: true,
      voice: 'F1',
      description: 'Elven scout companion'
    },
    {
      id: 'c3',
      title: 'Subterranean Crypts',
      type: 'location',
      triggers: ['crypt', 'catacombs', 'ruins'],
      value: 'Vast underground labyrinth dating back to the First Age.',
      entry: 'Vast underground labyrinth dating back to the First Age.',
      pinned: false
    }
  ],
  memories: [
    { id: 1, text: 'The party entered through the outer gate after defeating the gargoyle sentinels.', seq_start: 1, seq_end: 10, embedding_dim: 1024, embedding_model: 'Qwen3-Embedding-0.6B' },
    { id: 2, text: 'Lyra uncovered an engraved brass key hidden within a hollow stone.', seq_start: 11, seq_end: 20, embedding_dim: 1024, embedding_model: 'Qwen3-Embedding-0.6B' }
  ],
  memory: {
    plot_essentials: 'You are Kaelen, a relic hunter seeking the Sunstone before midnight alongside elven scout Lyra Silverthorne.',
    authors_note: 'Grim dark fantasy tone, vivid sensory detail, grounded consequences.',
    story_summary: 'The adventurers penetrated the outer barrier and are currently deciphering the runic gate.',
    summary: 'The adventurers penetrated the outer barrier and are currently deciphering the runic gate.'
  },
  choices: [
    { type: 'do', text: 'Press your palm against the center rune' },
    { type: 'say', text: 'Ask Lyra to check for hidden tripwires' },
    { type: 'story', text: 'The heavy iron door groans as the seal unlocks.' }
  ],
  ui_profile: {
    tabs: [
      { type: 'do', label: 'Action', placeholder: 'What do you do?' },
      { type: 'say', label: 'Dialogue', placeholder: 'What do you say?' },
      { type: 'story', label: 'Narrate', placeholder: 'Write the next beat...' }
    ],
    widgets: [
      { id: 'w1', type: 'dice', label: 'Dice Rollers', dice: [4, 6, 8, 12, 20] },
      { id: 'w2', type: 'stats', label: 'Vitality & Mana', stats: [{ key: 'hp', label: 'Health', value: 85, max: 100 }, { key: 'mp', label: 'Mana', value: 40, max: 50 }] },
      { id: 'w3', type: 'inventory', label: 'Inventory Pouch', items: [{ name: 'Silver Dagger', qty: 1 }, { name: 'Healing Salve', qty: 2 }, { name: 'Runic Key', qty: 1 }] }
    ]
  },
  ui_state: {
    w2: { hp: 85, mp: 40 },
    w3: [{ name: 'Silver Dagger', qty: 1 }, { name: 'Healing Salve', qty: 2 }, { name: 'Runic Key', qty: 1 }]
  },
  scene: {
    location: 'Outer Catacombs Entrance',
    atmosphere: 'torchlit subterranean stone archway',
    image_url: null,
    pending: false,
    cast: [
      { id: 'c2', title: 'Lyra Silverthorne', role: 'Elven Scout', portrait_url: null }
    ]
  }
};

const mockAdventuresList = [
  {
    id: 'efab03aeeadc41d2953dc2da6a81d63d',
    title: 'The Forgotten Catacombs',
    excerpt: 'You venture into the dark subterranean crypts beneath the citadel, where ancient runes hum with forgotten power...',
    action_count: 24,
    updated_at: new Date().toISOString()
  },
  {
    id: '08964ce1010140d49d6dfad944393da8',
    title: 'Cyberpunk Neon Requiem',
    excerpt: 'Rain slicks the chrome pavement of Sector 7 as the orbital uplink goes dark and corporate hunters close in...',
    action_count: 42,
    updated_at: new Date(Date.now() - 86400000).toISOString()
  },
  {
    id: 'faerun-caio-reference-adventure',
    title: 'Faerûn: Swords of the North',
    excerpt: 'Across the frost-bitten passes of the Spine of the World, a caravan carries a sealed chest from Luskan...',
    action_count: 201,
    updated_at: new Date(Date.now() - 172800000).toISOString()
  }
];

const mockScenariosList = [
  {
    id: 'scen-1',
    title: 'The Forgotten Catacombs',
    description: 'Delve into ancient subterranean ruins beneath the fallen citadel.',
    prompt: 'You stand before the iron gates of the ancient catacombs.',
    tags: ['fantasy', 'dungeon', 'mystery']
  }
];

const mockLlmSettings = {
  provider: 'llamacpp',
  base_url: 'http://vm.groupabz.com:1234/v1',
  model_alias: 'Qwen3.6-35B-A3B',
  context_size: 40960,
  context_size_limits: { min: 4096, max: 40960 },
  max_tokens: 300,
  streaming: true,
  disable_thinking: true,
  timeout_seconds: 180,
  temperature: 0.85,
  top_p: 0.95,
  min_p: 0.08,
  repetition_penalty: 1.05,
  presence_penalty: 0.15,
  frequency_penalty: 0.1,
  dry_multiplier: 0.8,
  dry_base: 1.75,
  dry_allowed_length: 2,
  dry_penalty_last_n: 1024,
  retry_temperature_step: 0.05,
  max_retry_temperature: 1.15
};

const mockImageSettings = {
  enabled: true,
  provider: 'sdwebui',
  base_url: 'http://127.0.0.1:7860',
  sdwebui_base_url: 'http://127.0.0.1:7860',
  steps: 30,
  sdwebui_steps: 30,
  width: 1024,
  height: 1024,
  sdwebui_width: 1024,
  sdwebui_height: 1024,
  negative_prompt: 'text, watermark, low quality, deformed',
  style_suffix: 'photorealistic photograph, natural skin texture, real lighting, sharp detail'
};

const mockVoiceSettings = {
  enabled: true,
  base_url: 'https://vm.groupabz.com/voice/v1',
  tts_model: 'supertonic-3',
  stt_model: 'whisper-1',
  narrator_voice: 'M1',
  voices: ['M1', 'M2', 'M3', 'F1', 'F2', 'F3'],
  timeout_seconds: 300
};

const mockEmbeddingsSettings = {
  enabled: true,
  base_url: 'http://127.0.0.1:8081/v1',
  model: 'Qwen3-Embedding-0.6B',
  timeout_seconds: 10
};

const mockScriptingSettings = {
  enabled: true,
  timeout_ms: 1000,
  input_modifier: 'def modify(text):\n    # Normalize player input spacing\n    return text.strip()\n',
  context_modifier: 'def modify(text):\n    return text\n',
  output_modifier: 'def modify(text):\n    return text.replace("  ", " ")\n'
};

let forceUnauth = false;

function sendJson(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

const server = http.createServer((req, res) => {
  const reqPath = req.url.split('?')[0];
  if (reqPath.startsWith('/api/')) {
    if (reqPath === '/api/auth/check') {
      if (forceUnauth) return sendJson(res, 401, { error: { code: 'unauthorized', message: 'Unauthorized' } });
      return sendJson(res, 200, { status: 'ok', user: 'Caio' });
    }
    if (reqPath.endsWith('/memories/regenerate')) return sendJson(res, 200, { status: 'regenerated', count: 2 });
    if (reqPath.endsWith('/memories')) return sendJson(res, 200, mockAdventure.memories);
    if (reqPath.endsWith('/memory')) return sendJson(res, 200, mockAdventure.memory);
    if (reqPath.endsWith('/cards/bulk')) return sendJson(res, 200, { updated: 2, action: 'pin' });
    if (reqPath.endsWith('/cards')) return sendJson(res, 200, mockAdventure.cards);
    if (reqPath.endsWith('/choices')) return sendJson(res, 200, { choices: mockAdventure.choices });
    if (reqPath.endsWith('/ui-profile')) return sendJson(res, 200, { profile: mockAdventure.ui_profile, state: mockAdventure.ui_state });
    if (reqPath.endsWith('/ui-state')) return sendJson(res, 200, { state: mockAdventure.ui_state });
    if (reqPath.endsWith('/scene')) return sendJson(res, 200, mockAdventure.scene);
    if (reqPath.endsWith('/jobs')) return sendJson(res, 200, []);
    if (reqPath.endsWith('/events')) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
      res.end('retry: 60000\n\n');
      return;
    }
    if (reqPath.startsWith('/api/adventures/')) return sendJson(res, 200, mockAdventure);
    if (reqPath === '/api/adventures') return sendJson(res, 200, mockAdventuresList);
    if (reqPath.startsWith('/api/scenarios/')) return sendJson(res, 200, mockScenariosList[0]);
    if (reqPath === '/api/scenarios') return sendJson(res, 200, mockScenariosList);
    if (reqPath === '/api/voices') return sendJson(res, 200, { voices: mockVoiceSettings.voices, narrator_voice: 'M1' });
    if (reqPath === '/api/health/llm') return sendJson(res, 200, { status: 'ok', model: 'Qwen3.6-35B-A3B' });
    if (reqPath === '/api/settings/llm') return sendJson(res, 200, mockLlmSettings);
    if (reqPath === '/api/settings/image') return sendJson(res, 200, mockImageSettings);
    if (reqPath === '/api/settings/voice') return sendJson(res, 200, mockVoiceSettings);
    if (reqPath === '/api/settings/embeddings') return sendJson(res, 200, mockEmbeddingsSettings);
    if (reqPath === '/api/settings/scripting') return sendJson(res, 200, mockScriptingSettings);
    if (reqPath === '/api/settings') return sendJson(res, 200, { llm: mockLlmSettings });
    return sendJson(res, 200, {});
  }

  let filePath = path.join(distDir, reqPath === '/' ? 'index.html' : reqPath);
  if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    filePath = path.join(distDir, 'index.html');
  }

  const ext = path.extname(filePath);
  const contentType = mimeTypes[ext] || 'application/octet-stream';
  res.writeHead(200, { 'Content-Type': contentType });
  fs.createReadStream(filePath).pipe(res);
});

const PORT = 4175;
await new Promise((resolve) => server.listen(PORT, resolve));
console.log(`Static + mock API server running on http://127.0.0.1:${PORT}`);

const browser = await puppeteer.launch({
  executablePath: '/usr/bin/google-chrome',
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
});

async function captureScreen(url, filename, viewport, options = {}) {
  forceUnauth = Boolean(options.unauth);
  const page = await browser.newPage();
  await page.setViewport(viewport);

  page.on('console', (msg) => {
    if (msg.type() === 'error') console.log(`[PAGE CONSOLE ERROR ${filename}]`, msg.text());
  });
  page.on('pageerror', (err) => console.log(`[PAGE ERROR ${filename}]`, err.message));

  await page.evaluateOnNewDocument((unauth, theme) => {
    if (unauth) {
      localStorage.removeItem('aidl_token');
    } else {
      localStorage.setItem('aidl_token', 'mock_jwt_token_for_screenshots');
    }
    if (theme) localStorage.setItem('aidl_theme', theme);
  }, Boolean(options.unauth), options.theme || '');

  await page.goto(url, { waitUntil: 'networkidle0' });
  await new Promise((r) => setTimeout(r, 800));

  if (options.actions) {
    await options.actions(page);
    await new Promise((r) => setTimeout(r, 600));
  }

  const outPath = path.join(outDir, filename);
  await page.screenshot({ path: outPath, fullPage: false });
  console.log(`Saved screenshot: ${outPath}`);
  await page.close();
}

try {
  const base = `http://127.0.0.1:${PORT}`;

  // 1. Login screen
  console.log('Capturing Login...');
  await captureScreen(`${base}/login`, 'login_1440x900.png', { width: 1440, height: 900 }, { unauth: true });
  await captureScreen(`${base}/login`, 'login_390x844.png', { width: 390, height: 844 }, { unauth: true });

  // 2. Home screen
  console.log('Capturing Home...');
  await captureScreen(`${base}/`, 'home_1440x900.png', { width: 1440, height: 900 });
  await captureScreen(`${base}/`, 'home_390x844.png', { width: 390, height: 844 });

  // 3. Play screen (Desktop 2-pane & Mobile)
  console.log('Capturing Play screen...');
  await captureScreen(`${base}/play/efab03aeeadc41d2953dc2da6a81d63d`, 'play_1440x900.png', { width: 1440, height: 900 });
  await captureScreen(`${base}/play/efab03aeeadc41d2953dc2da6a81d63d`, 'play_390x844.png', { width: 390, height: 844 });

  // 4. Play Drawer / Inspector
  console.log('Capturing Play Drawer / Mobile Drawer...');
  await captureScreen(
    `${base}/play/efab03aeeadc41d2953dc2da6a81d63d`,
    'play_drawer_1440x900.png',
    { width: 1440, height: 900 },
    {
      actions: async (page) => {
        const memoryTabBtn = await page.$('aside button:nth-child(3)');
        if (memoryTabBtn) await memoryTabBtn.click();
      }
    }
  );
  await captureScreen(
    `${base}/play/efab03aeeadc41d2953dc2da6a81d63d`,
    'play_drawer_390x844.png',
    { width: 390, height: 844 },
    {
      actions: async (page) => {
        const drawerBtn = await page.$('header button[aria-label="Abrir o inspetor"]');
        if (drawerBtn) await drawerBtn.click();
      }
    }
  );

  // 5. Dedicated Settings page
  console.log('Capturing Settings screen...');
  await captureScreen(`${base}/settings`, 'settings_1440x900.png', { width: 1440, height: 900 });
  await captureScreen(`${base}/settings`, 'settings_390x844.png', { width: 390, height: 844 });

  // 6. Scenario Editor page
  console.log('Capturing Scenario Editor screen...');
  await captureScreen(`${base}/scenarios/new`, 'scenario_editor_1440x900.png', { width: 1440, height: 900 });
  await captureScreen(`${base}/scenarios/new`, 'scenario_editor_390x844.png', { width: 390, height: 844 });

  // 7. Reader Preferences Modal
  console.log('Capturing Reader Preferences popup...');
  await captureScreen(
    `${base}/play/efab03aeeadc41d2953dc2da6a81d63d`,
    'reader_preferences_1440x900.png',
    { width: 1440, height: 900 },
    {
      actions: async (page) => {
        const readerBtn = await page.$('button[title*="Tipografia"]');
        if (readerBtn) await readerBtn.click();
      }
    }
  );

  const playId = 'efab03aeeadc41d2953dc2da6a81d63d';
  for (const theme of ['obsidian', 'parchment', 'oled']) {
    console.log(`Capturing theme ${theme}...`);
    await captureScreen(`${base}/`, `home_${theme}_1440x900.png`, { width: 1440, height: 900 }, { theme });
    await captureScreen(
      `${base}/play/${playId}`,
      `play_${theme}_1440x900.png`,
      { width: 1440, height: 900 },
      { theme },
    );
  }

  console.log('All after screenshots captured successfully!');
} catch (err) {
  console.error('Error capturing screenshots:', err);
} finally {
  await browser.close();
  server.close();
}
