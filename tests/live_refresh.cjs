// Run with node tests/live_refresh.cjs. Playwright and Python paths can be overridden.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const runtime = 'C:/Users/pc/.cache/codex-runtimes/codex-primary-runtime/dependencies';
const { chromium } = require(process.env.PLAYWRIGHT_PATH || runtime + '/node/node_modules/playwright');
const root = path.resolve(__dirname, '..');
const db = path.join(root, '数据存放/tasks.db');
const hash = () => require('node:crypto').createHash('sha256').update(fs.readFileSync(db)).digest('hex');
const before = hash();
const fixture = spawn(process.env.PYTHON_PATH || runtime + '/python/python.exe',
  ['-B', path.join(__dirname, 'fixture_server.py')], { cwd: root });
let browser;
let log = '';
fixture.stdout.on('data', chunk => { log += chunk; });
fixture.stderr.on('data', chunk => { log += chunk; });

(async () => {
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(log)), 15000);
    fixture.stdout.on('data', () => {
      const match = log.match(/TEST_URL=(\S+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
    fixture.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + '\n' + log)); });
  });
  const api = async (endpoint, method = 'GET', body, token) => {
    const res = await fetch(url + '/api' + endpoint, { method,
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    const json = await res.json();
    assert.equal(res.ok, true, JSON.stringify(json));
    assert.equal(json.ok, true, JSON.stringify(json));
    return json;
  };
  const a = await api('/auth/login', 'POST', { username: 'admin', password: 'refresh-test' });
  const b = await api('/auth/login', 'POST', { username: 'admin', password: 'refresh-test' });
  const made = await api('/tasks', 'POST', { title: 'Initial task', task_type: 'NORMAL', priority: 'M' }, b.token);
  const id = made.id || made.task?.id || (await api('/tasks', 'GET', null, b.token)).tasks[0].id;
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(({ token, user }) => {
    localStorage.setItem('tt_token', token);
    localStorage.setItem('tt_me', JSON.stringify(user));
  }, a);
  const original = execFileSync('git', ['-c', 'safe.directory=' + root.replaceAll('\\', '/'),
    'show', 'baseline-before-live-refresh:TeamToDo V1.1/static/index.html'], { cwd: root, encoding: 'utf8' });
  await page.route('**/index.html', route => route.fulfill({ contentType: 'text/html', body: original }));
  await page.goto(url + '/index.html');
  await page.waitForFunction(() => tasks.some(t => t.title === 'Initial task'));
  await api('/tasks/' + id, 'PUT', { title: 'Remote update' }, b.token);
  await page.waitForTimeout(4000);
  assert.equal(await page.evaluate(() => tasks[0].title), 'Initial task');
  console.log('PASS: original version reproduces stale page after another session writes');
  await page.unroute('**/index.html');
  await page.reload();
  await page.waitForFunction(() => tasks.some(t => t.title === 'Remote update'));
  await api('/tasks/' + id, 'PUT', { title: 'Live update', status: 'DOING' }, b.token);
  await page.waitForFunction(() => tasks.some(t => t.title === 'Live update' && t.status === 'DOING'), { timeout: 10000 });
  console.log('PASS: remote task edit and status automatically refresh');
  await page.evaluate(id => openDetail(id), id);
  await page.locator('#comment-input').fill('Unsaved feedback');
  await api('/tasks/' + id + '/comments', 'POST', { content: 'Remote feedback' }, b.token);
  await page.waitForFunction(() => document.getElementById('comment-list').textContent.includes('Remote feedback'), { timeout: 10000 });
  assert.equal(await page.locator('#comment-input').inputValue(), 'Unsaved feedback');
  await api('/tasks/' + id, 'PUT', { title: 'Detail updated' }, b.token);
  await page.waitForFunction(() => document.getElementById('detail-title').textContent === 'Detail updated', { timeout: 10000 });
  assert.equal(await page.locator('#comment-input').inputValue(), 'Unsaved feedback');
  console.log('PASS: open detail and feedback refresh while preserving unsaved input');
  await page.evaluate(() => closeDetailModal());
  await page.evaluate(id => openTaskModal(id), id);
  await page.locator('#t-title').fill('Unsaved edit');
  await api('/tasks/' + id, 'PUT', { title: 'While editing' }, b.token);
  await page.waitForTimeout(4000);
  assert.equal(await page.locator('#t-title').inputValue(), 'Unsaved edit');
  await page.evaluate(() => closeTaskModal());
  await page.waitForFunction(() => tasks.some(t => t.title === 'While editing'), { timeout: 10000 });
  console.log('PASS: edit draft survives polling and refresh resumes after closing');
  await page.evaluate(() => switchTab('dashboard'));
  await page.waitForFunction(() => document.getElementById('dash-kpis').textContent.includes('任务总数'));
  const statsBefore = await page.locator('#dash-kpis').innerText();
  await api('/tasks', 'POST', { title: 'New remote task' }, b.token);
  await page.waitForFunction(before => document.getElementById('dash-kpis').innerText !== before,
    statsBefore, { timeout: 10000 });
  console.log('PASS: dashboard automatically refreshes');
  await page.evaluate(() => switchTab('tasks'));
  await page.route('**/api/tasks?*', route => route.abort());
  await page.waitForTimeout(3500);
  await page.unroute('**/api/tasks?*');
  await api('/tasks/' + id, 'PUT', { title: 'Recovered network' }, b.token);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.waitForFunction(() => tasks.some(t => t.title === 'Recovered network'), { timeout: 10000 });
  console.log('PASS: polling recovers after failed requests');
  await page.evaluate(id => openDetail(id), id);
  await api('/tasks/' + id, 'DELETE', null, b.token);
  await page.waitForFunction(id => !tasks.some(t => t.id === id), id, { timeout: 10000 });
  await page.waitForFunction(() => !document.getElementById('modal-detail').classList.contains('show'));
  console.log('PASS: remote deletion automatically disappears and closes obsolete detail');
  await page.evaluate(() => document.getElementById('flt-search').value = 'New remote');
  await page.evaluate(() => loadTasks());
  await api('/tasks', 'POST', { title: 'Unrelated task' }, b.token);
  await page.waitForTimeout(4000);
  assert.equal(await page.locator('#flt-search').inputValue(), 'New remote');
  assert.equal(await page.evaluate(() => tasks.some(t => t.title === 'Unrelated task')), false);
  console.log('PASS: polling preserves the active task search');
  await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, value: true }); });
  let reads = 0;
  const countReads = req => {
    const url = new URL(req.url());
    // Count list refreshes, not the summary's one-row count queries.
    if (url.pathname === '/api/tasks' && url.search && url.searchParams.get('limit') !== '1') reads++;
  };
  page.on('request', countReads);
  await page.evaluate(() => refreshLiveData());
  await page.waitForTimeout(3500);
  assert.equal(reads, 0);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForFunction(() => !_liveBusy);
  assert.equal(reads, 1);
  page.off('request', countReads);
  console.log('PASS: hidden page pauses reads and returning triggers immediate refresh');
  assert.deepEqual(errors, []);
  assert.equal(hash(), before);
  console.log('PASS: no browser script errors; production database SHA-256 unchanged (' + before + ')');
})().catch(e => { console.error(e); process.exitCode = 1; })
  .finally(async () => { if (browser) await browser.close(); fixture.stdin.end(); });
