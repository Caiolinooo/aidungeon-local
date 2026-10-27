import http from 'http';
import fs from 'fs';
import path from 'path';
import puppeteer from 'puppeteer-core';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '../..');
const distDir = path.join(rootDir, 'frontend', 'dist');

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
  last_excerpt: 'You venture into the dark subterranean crypts beneath the citadel, where ancient runes hum with forgotten power...',
  excerpt: 'You venture into the dark subterranean crypts beneath the citadel, where ancient runes hum with forgotten power...',
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
      { id: 'do', type: 'do', label: 'Action', placeholder: 'What do you do?' },
      { id: 'say', type: 'say', label: 'Dialogue', placeholder: 'What do you say?' },
      { id: 'story', type: 'story', label: 'Narrate', placeholder: 'Write the next beat...' }
    ],
    widgets: [
      { id: 'w1', type: 'dice', label: 'Dice Rollers', dice: [4, 6, 8, 12, 20] },
      { id: 'w2', type: 'stats', label: 'Vitality & Mana', stats: [{ key: 'hp', label: 'Health', value: 85, max: 100 }, { key: 'mp', label: 'Mana', value: 40, max: 50 }] },
      { id: 'w3', type: 'inventory', label: 'Inventory Pouch', items: [{ name: 'Silver Dagger', qty: 1 }, { name: 'Healing Salve', qty: 2 }] }
    ]
  },
  ui_state: {
    w2: { hp: 85, mp: 40 },
    w3: [{ name: 'Silver Dagger', qty: 1 }, { name: 'Healing Salve', qty: 2 }]
  },
  scene: {
    location: 'Outer Catacombs Entrance',
    atmosphere: 'night',
    cast: [{ id: 'c2', title: 'Lyra Silverthorne', mood: 'happy' }]
  }
};

function sendJson(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

const server = http.createServer((req, res) => {
  const reqPath = req.url.split('?')[0];
  if (reqPath.startsWith('/api/')) {
    if (reqPath === '/api/auth/login') return sendJson(res, 200, { token: 'mock_smoke_token', expires_at: new Date(Date.now() + 86400000).toISOString() });
    if (reqPath === '/api/auth/check') return sendJson(res, 200, { status: 'ok', user: 'Caio' });
    if (reqPath.endsWith('/action') || reqPath.endsWith('/continue') || reqPath.endsWith('/retry')) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
      res.write('event: token\ndata: {"text": "The ancient runes flare with emerald light."}\n\n');
      res.write('event: done\ndata: {"action": {"id": "4", "seq": 4, "type": "continue", "text": "The ancient runes flare with emerald light."}}\n\n');
      res.end();
      return;
    }
    if (reqPath.endsWith('/undo')) return sendJson(res, 200, mockAdventure);
    if (reqPath.startsWith('/api/actions/')) return sendJson(res, 200, { id: '3', seq: 3, type: 'continue', text: 'Edited text', edited_at: new Date().toISOString() });
    if (reqPath.endsWith('/see')) return sendJson(res, 200, { action: { id: '5', seq: 5, type: 'see', text: 'Scene illustration', image: { id: 'img1', prompt: 'Dark catacombs gate' } } });
    if (reqPath.endsWith('/export') || reqPath === '/api/adventures/export') {
      res.writeHead(200, { 'Content-Type': 'application/zip' });
      res.end(Buffer.from('PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'));
      return;
    }
    if (reqPath === '/api/assistant/scenario') {
      return sendJson(res, 200, {
        title: 'Clockwork Leviathan',
        description: 'Steampunk hunt across the brass seas.',
        prompt: 'You stand on the bridge of the ironclad airship.',
        plot_essentials: 'Captain of the sky-vessel Aether.',
        authors_note: 'Steampunk adventure.',
        ai_instructions: 'Second person narration.',
        tags: ['steampunk'],
        story_cards: [{ title: 'First Mate Vance', type: 'character', triggers: ['vance'], value: 'Veteran airship navigator.' }]
      });
    }
    if (reqPath.endsWith('/memories/regenerate')) return sendJson(res, 200, { status: 'regenerated', count: 2 });
    if (reqPath.endsWith('/memories')) return sendJson(res, 200, mockAdventure.memories);
    if (reqPath.endsWith('/memory')) return sendJson(res, 200, mockAdventure.memory);
    if (reqPath.endsWith('/cards/bulk')) return sendJson(res, 200, { updated: 1, action: 'pin' });
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
    if (reqPath === '/api/adventures') return sendJson(res, 200, [mockAdventure]);
    if (reqPath === '/api/scenarios') return sendJson(res, 200, [{ id: 's1', title: 'The Forgotten Catacombs', description: 'Dungeon crawl', prompt: 'Opening', tags: ['fantasy'] }]);
    if (reqPath === '/api/voices') return sendJson(res, 200, { voices: ['M1', 'F1'], narrator_voice: 'M1' });
    if (reqPath === '/api/health/llm') return sendJson(res, 200, { status: 'ok', model: 'Qwen3.6-35B-A3B' });
    if (reqPath === '/api/settings/llm') return sendJson(res, 200, { provider: 'llamacpp', base_url: 'http://vm.groupabz.com:1234/v1', model_alias: 'Qwen3.6-35B-A3B', context_size: 40960, context_size_limits: { min: 4096, max: 40960 }, max_tokens: 300, temperature: 0.85, top_p: 0.95, min_p: 0.08, repetition_penalty: 1.05, presence_penalty: 0.15, frequency_penalty: 0.1, dry_multiplier: 0.8, dry_base: 1.75, dry_allowed_length: 2, dry_penalty_last_n: 1024, streaming: true, disable_thinking: true });
    if (reqPath === '/api/settings/image') return sendJson(res, 200, { enabled: true, provider: 'sdwebui', base_url: 'http://127.0.0.1:7860', width: 1024, height: 1024, steps: 30 });
    if (reqPath === '/api/settings/voice') return sendJson(res, 200, { enabled: true, base_url: 'https://vm.groupabz.com/voice/v1', narrator_voice: 'M1', voices: ['M1', 'F1'] });
    if (reqPath === '/api/settings/embeddings') return sendJson(res, 200, { enabled: true, base_url: 'http://127.0.0.1:8081/v1', model: 'Qwen3-Embedding-0.6B' });
    if (reqPath === '/api/settings/scripting') return sendJson(res, 200, { enabled: true, timeout_ms: 1000, input_modifier: '', context_modifier: '', output_modifier: '' });
    return sendJson(res, 200, {});
  }

  let filePath = path.join(distDir, reqPath === '/' ? 'index.html' : reqPath);
  if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    filePath = path.join(distDir, 'index.html');
  }
  const ext = path.extname(filePath);
  res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
  fs.createReadStream(filePath).pipe(res);
});

const PORT = 4178;
await new Promise((r) => server.listen(PORT, r));
const base = `http://127.0.0.1:${PORT}`;

const browser = await puppeteer.launch({
  executablePath: '/usr/bin/google-chrome',
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
});

async function runA11yAudit(page, pageName) {
  return page.evaluate((name) => {
    const checks = [];
    const htmlLang = document.documentElement.getAttribute('lang');
    checks.push({ id: 'html-has-lang', passed: Boolean(htmlLang), detail: htmlLang });
    checks.push({ id: 'document-title', passed: Boolean(document.title && document.title.trim().length > 0), detail: document.title });

    const imgs = [...document.querySelectorAll('img')];
    const imgsMissingAlt = imgs.filter((img) => !img.hasAttribute('alt'));
    checks.push({ id: 'image-alt', passed: imgsMissingAlt.length === 0, detail: `${imgs.length - imgsMissingAlt.length}/${imgs.length}` });

    const buttons = [...document.querySelectorAll('button')];
    const unnamedButtons = buttons.filter((b) => {
      const text = (b.innerText || b.textContent || '').trim();
      const aria = b.getAttribute('aria-label') || b.getAttribute('title') || '';
      return !text && !aria;
    });
    checks.push({ id: 'button-name', passed: unnamedButtons.length === 0, detail: `${buttons.length - unnamedButtons.length}/${buttons.length}` });

    const inputs = [...document.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="file"]), textarea, select')];
    const unlabeledInputs = inputs.filter((inp) => {
      const hasLabel = inp.id && document.querySelector(`label[for="${inp.id}"]`);
      const parentLabel = inp.closest('label');
      const aria = inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || inp.getAttribute('title');
      return !hasLabel && !parentLabel && !aria;
    });
    checks.push({ id: 'form-field-labels', passed: unlabeledInputs.length === 0, detail: `${inputs.length - unlabeledInputs.length}/${inputs.length}` });

    const landmarks = document.querySelectorAll('main, header, nav, aside, section, [role="main"], [role="navigation"]');
    checks.push({ id: 'landmark-regions', passed: landmarks.length > 0, detail: `count=${landmarks.length}` });

    const headings = document.querySelectorAll('h1, h2, h3');
    checks.push({ id: 'heading-hierarchy', passed: headings.length > 0, detail: `count=${headings.length}` });

    const metaViewport = document.querySelector('meta[name="viewport"]');
    const viewportContent = metaViewport ? metaViewport.getAttribute('content') || '' : '';
    const zoomBlocked = viewportContent.includes('user-scalable=no') || viewportContent.includes('maximum-scale=1');
    checks.push({ id: 'meta-viewport-scalable', passed: Boolean(metaViewport) && !zoomBlocked, detail: viewportContent });

    const passedCount = checks.filter((c) => c.passed).length;
    const score = Math.round((passedCount / checks.length) * 100);
    return { pageName: name, score, checks };
  }, pageName);
}

async function runViewportSmoke(viewport) {
  const label = `${viewport.width}x${viewport.height}`;
  const isMobile = viewport.width < 1024;
  const page = await browser.newPage();
  await page.setViewport(viewport);

  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('WebGL') && !msg.text().includes('THREE')) {
      consoleErrors.push(msg.text());
    }
  });

  // 1. Login flow
  await page.goto(`${base}/login`, { waitUntil: 'networkidle0' });
  await page.evaluate(() => localStorage.removeItem('aidl_token'));
  await page.goto(`${base}/login`, { waitUntil: 'networkidle0' });
  await page.type('input[type="text"]', 'Caio');
  await page.type('input[type="password"]', 'secret');
  await page.click('button[type="submit"]');
  await page.waitForFunction(
    () => window.location.pathname === '/' && document.body.innerText.includes('The Forgotten Catacombs'),
    { timeout: 8000 }
  );

  // 2. Home screen features: New Adventure, Import, Backup All, Generate with AI, Reader Theme
  const homeA11y = await runA11yAudit(page, `Home (${label})`);
  const homeText = await page.evaluate(() => document.body.innerText);
  if (!homeText.includes('The Forgotten Catacombs')) {
    throw new Error(`[${label}] Home screen did not render adventure card`);
  }

  // Open & close Generate with AI modal
  const genBtn = await page.evaluateHandle(() =>
    [...document.querySelectorAll('button')].find((b) => b.innerText.includes('Gerar com IA'))
  );
  if (genBtn.asElement()) {
    await genBtn.asElement().click();
    await new Promise((r) => setTimeout(r, 250));
    await page.keyboard.press('Escape');
  }

  // 3. Scenario Editor
  await page.goto(`${base}/scenarios/new`, { waitUntil: 'networkidle0' });
  const scenText = await page.evaluate(() => document.body.innerText);
  if (!scenText.toLowerCase().includes('cenário') && !scenText.toLowerCase().includes('cenario')) {
    throw new Error(`[${label}] Scenario Editor did not render`);
  }

  // 4. Settings Page
  await page.goto(`${base}/settings`, { waitUntil: 'networkidle0' });
  const settingsText = await page.evaluate(() => document.body.innerText);
  if (!settingsText.includes('40,960') && !settingsText.includes('40.960')) {
    throw new Error(`[${label}] Settings page missing 40,960 context cap`);
  }

  // 5. Play screen & all feature entry points
  await page.goto(`${base}/play/efab03aeeadc41d2953dc2da6a81d63d`, { waitUntil: 'networkidle0' });
  await new Promise((r) => setTimeout(r, 500));
  const playA11y = await runA11yAudit(page, `Play (${label})`);

  // Check Do / Say / Story (Action / Dialogue / Narrate) tabs + input + Send
  const tabButtons = await page.$$('textarea ~ button, button');
  if (tabButtons.length < 5) {
    throw new Error(`[${label}] Play screen missing action/control buttons`);
  }

  // Exercise Inspector / Drawer tabs: Cards, NPCs, Memory, Controls, Avatars, Config, Details
  if (isMobile) {
    const opened = await page.evaluate(() => {
      const btn = document.querySelector('header button[aria-label="Abrir o inspetor"]');
      if (btn) {
        btn.click();
        return true;
      }
      return false;
    });
    if (!opened) throw new Error(`[${label}] Mobile drawer toggle button not found`);
    await new Promise((r) => setTimeout(r, 400));
  }

  const expectedTabs = ['Fichas', 'NPCs', 'Memória', 'Controles', 'Avatares', 'Configurações', 'Detalhes'];
  for (const tabLabel of expectedTabs) {
    const clicked = await page.evaluate((lbl) => {
      const btn = [...document.querySelectorAll('aside button')].find((b) => b.innerText.trim() === lbl);
      if (btn) {
        btn.click();
        return true;
      }
      return false;
    }, tabLabel);
    if (!clicked) {
      throw new Error(`[${label}] Inspector tab "${tabLabel}" not reachable`);
    }
    await new Promise((r) => setTimeout(r, 150));
  }

  // 6. PWA & Offline Banner check
  const pwaCheck = await page.evaluate(async () => {
    const manifestLink = document.querySelector('link[rel="manifest"]');
    const manifestRes = manifestLink ? await fetch(manifestLink.getAttribute('href')).then((r) => r.json()) : null;
    const swReg = 'serviceWorker' in navigator ? await navigator.serviceWorker.getRegistration() : null;
    return {
      hasManifestLink: Boolean(manifestLink),
      manifestName: manifestRes?.name,
      manifestDisplay: manifestRes?.display,
      manifestIconsCount: manifestRes?.icons?.length || 0,
      swActive: Boolean(swReg?.active),
    };
  });

  // Trigger offline event and verify OfflineBanner appears
  await page.setOfflineMode(true);
  await page.evaluate(() => window.dispatchEvent(new Event('offline')));
  await new Promise((r) => setTimeout(r, 300));
  const offlineBannerVisible = await page.evaluate(() =>
    document.body.innerText.toLowerCase().includes('offline') ||
    document.body.innerText.toLowerCase().includes('unreachable')
  );
  await page.setOfflineMode(false);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));

  await page.close();

  if (pageErrors.length > 0 || consoleErrors.length > 0) {
    throw new Error(`[${label}] Uncaught errors: pageErrors=${JSON.stringify(pageErrors)} consoleErrors=${JSON.stringify(consoleErrors)}`);
  }

  return {
    viewport: label,
    pageErrors: pageErrors.length,
    consoleErrors: consoleErrors.length,
    homeA11y,
    playA11y,
    pwaCheck,
    offlineBannerVisible,
  };
}

try {
  const desktop = await runViewportSmoke({ width: 1440, height: 900 });
  const mobile = await runViewportSmoke({ width: 390, height: 844 });
  const report = { status: 'PASS', desktop, mobile };
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser.close();
  server.close();
}
