// Real-browser navigation regression; all API writes use a disposable database.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const runtime = 'C:/Users/pc/.cache/codex-runtimes/codex-primary-runtime/dependencies';
const { chromium } = require(process.env.PLAYWRIGHT_PATH || runtime + '/node/node_modules/playwright');
const root = path.resolve(__dirname, '..');
const hash = () => crypto.createHash('sha256').update(fs.readFileSync(path.join(root, '数据存放/tasks.db'))).digest('hex');
const before = hash();
const fixture = spawn(process.env.PYTHON_PATH || runtime + '/python/python.exe',
  ['-B', path.join(__dirname, 'fixture_server.py'), ...(process.argv.includes('--exe') ? ['--exe'] : [])],
  { cwd: root, windowsHide: true });
let output = '', browser;
fixture.stdout.on('data', chunk => { output += chunk; });
fixture.stderr.on('data', chunk => { output += chunk; });
(async () => {
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(output)), 45000);
    fixture.stdout.on('data', () => {
      const match = output.match(/TEST_URL=(\S+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
    fixture.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + output)); });
  });
  let token;
  const api = async (endpoint, method = 'GET', body) => {
    const res = await fetch(url + '/api' + endpoint, { method,
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    const json = await res.json();
    assert.ok(res.ok && json.ok, JSON.stringify(json));
    return json;
  };
  const admin = await api('/auth/login', 'POST', { username: 'admin', password: 'refresh-test' });
  token = admin.token;
  await api('/tasks', 'POST', { title: 'My assigned task', assignee_id: admin.user.id, due_date: '2000-01-01' });
  for (let i = 0; i < 20; i++) await api('/tasks', 'POST', { title: 'Other task ' + i, due_date: '2000-01-01' });
  const today = new Date().toISOString().slice(0, 10);
  await api('/tasks', 'POST', { title: 'Upcoming task', due_date: today });
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(({ token, user }) => {
    localStorage.setItem('tt_token', token); localStorage.setItem('tt_me', JSON.stringify(user));
  }, admin);
  await page.goto(url + '/index.html');
  await page.waitForFunction(() => tasks.length === 22);
  await page.locator('.side-item[data-preset="mine"]').click();
  await page.waitForFunction(() => tasks.length === 1 && tasks[0].title === 'My assigned task');
  await page.locator('.side-item[data-tab="tasks"]').click();
  await page.waitForFunction(() => tasks.length === 22, { timeout: 10000 });
  assert.equal(await page.locator('#flt-mine').inputValue(), '');
  assert.equal(await page.locator('.side-item.active').count(), 1);
  console.log('PASS: my tasks -> workbench restores all tasks and one active navigation item');
  for (const preset of ['overdue', 'upcoming', 'pool']) {
    await page.locator('.side-item[data-preset="' + preset + '"]').click();
    assert.equal(await page.locator('.side-item.active').count(), 1);
    await page.locator('.side-item[data-tab="tasks"]').click();
    await page.waitForFunction(() => tasks.length === 22 && buildTaskQuery().toString() === '');
  }
  console.log('PASS: all task presets return to an unfiltered workbench');
  await page.locator('.side-item[data-preset="mine"]').click();
  await page.waitForFunction(() => tasks.length === 1);
  await page.locator('.side-item[data-preset="upcoming"]').click();
  await page.waitForFunction(() => tasks.length === 1 && tasks[0].title === 'Upcoming task');
  console.log('PASS: switching presets clears the previous ownership filter');
  await page.locator('.side-item[data-tab="tasks"]').click();
  await page.locator('#flt-search').fill('no matching task');
  await page.locator('#flt-status').selectOption('DOING');
  await page.locator('.side-item[data-tab="tasks"]').click();
  await page.waitForFunction(() => tasks.length === 22 && buildTaskQuery().toString() === '');
  await page.waitForFunction(() => document.getElementById('sum-overdue').textContent === '21' &&
    document.getElementById('sum-upcoming').textContent === '1');
  assert.equal(await page.locator('#sum-total').textContent(), '22');
  console.log('PASS: explicit workbench navigation clears filters; summary counts are numeric and not capped at 20');
  assert.deepEqual(errors, []);
  assert.equal(hash(), before);
  console.log('PASS: no browser errors; production database unchanged');
})().catch(e => { console.error(e); process.exitCode = 1; })
  .finally(async () => { if (browser) await browser.close(); fixture.stdin.end(); });
