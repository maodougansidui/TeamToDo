// Diagnostic: exits with code 1 when a confirmed application defect is found.
// All writes target the disposable fixture database, never deployment config.
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
let log = '';
let browser;
let findings = 0;
fixture.stdout.on('data', chunk => { log += chunk; });
fixture.stderr.on('data', chunk => { log += chunk; });
function check(name, expected, actual) {
  if (JSON.stringify(expected) === JSON.stringify(actual)) console.log('PASS: ' + name);
  else { findings++; console.log('FINDING: ' + name + ' | expected=' + JSON.stringify(expected) + ' actual=' + JSON.stringify(actual)); }
}
(async () => {
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(log)), 45000);
    fixture.stdout.on('data', () => {
      const match = log.match(/TEST_URL=(\S+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
    fixture.on('exit', code => { clearTimeout(timer); reject(new Error('Fixture exited ' + code + '\n' + log)); });
  });
  const request = async (endpoint, method = 'GET', body, token) => {
    const response = await fetch(url + '/api' + endpoint, { method,
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, body: await response.json() };
  };
  const ok = async (...args) => {
    const r = await request(...args);
    assert.ok(r.status < 300 && r.body.ok, JSON.stringify(r));
    return r.body;
  };
  const admin = await ok('/auth/login', 'POST', { username: 'admin', password: 'refresh-test' });
  const role = await ok('/roles', 'POST', { name: '班长测试岗位', permissions: ['task.feedback'] }, admin.token);
  const member = await ok('/users', 'POST', {
    username: 'leader-test', password: 'test-only', role: 'user', role_id: role.id, org_id: 1
  }, admin.token);
  const leader = await ok('/auth/login', 'POST', { username: 'leader-test', password: 'test-only' });
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(({ token, user }) => {
    localStorage.setItem('tt_token', token); localStorage.setItem('tt_me', JSON.stringify(user));
  }, admin);
  await page.goto(url + '/index.html');
  await page.waitForFunction(() => me && me.permissions);
  await page.evaluate(() => switchTab('roles'));
  await page.waitForFunction(id => roles.some(r => r.id === id), role.id);
  await page.evaluate(id => openRoleModal(id), role.id);
  await page.locator('input[value="task.create"]').check();
  const responsePromise = page.waitForResponse(r => r.url().endsWith('/roles/' + role.id + '/permissions') && r.request().method() === 'PUT');
  await page.locator('#modal-role .btn-submit').click();
  const saved = await responsePromise;
  check('permission save reports HTTP success', 200, saved.status());
  await page.waitForFunction(() => !document.getElementById('modal-role').classList.contains('show'));
  const persisted = await ok('/roles/' + role.id + '/permissions', 'GET', null, admin.token);
  check('clicking Save persists task.create', true, persisted.permissions.includes('task.create'));
  await page.reload();
  await page.evaluate(() => switchTab('roles'));
  await page.waitForFunction(id => roles.some(r => r.id === id), role.id);
  const listed = await page.evaluate(id => roles.find(r => r.id === id).permissions, role.id);
  check('saved permission survives full page reload', true, listed.includes('task.create'));
  const afterLogin = await ok('/auth/login', 'POST', { username: 'leader-test', password: 'test-only' });
  const canCreate = await request('/tasks', 'POST', { title: 'Task after permission save' }, afterLogin.token);
  check('leader can create after granting task.create and re-login', 201, canCreate.status);
  const task = await ok('/tasks', 'POST', { title: 'Admin-owned task' }, admin.token);
  const deleted = await request('/tasks/' + task.id, 'DELETE', null, leader.token);
  check('user without task.delete cannot delete admin task', 403, deleted.status);
  const surviving = await request('/tasks/' + task.id, 'GET', null, admin.token);
  check('admin task remains after unauthorized delete attempt', 200, surviving.status);
  const progressTask = await ok('/tasks', 'POST', { title: 'Progress test' }, admin.token);
  const progress = await request('/tasks/' + progressTask.id + '/progress', 'GET', null, admin.token);
  check('subtask progress endpoint returns valid data', 200, progress.status);

  const leaderContext = await browser.newContext();
  const leaderPage = await leaderContext.newPage();
  await leaderPage.addInitScript(({ token, user }) => {
    localStorage.setItem('tt_token', token); localStorage.setItem('tt_me', JSON.stringify(user));
  }, leader);
  await leaderPage.goto(url + '/index.html');
  await leaderPage.waitForFunction(() => me && me.permissions);
  check('leader initially has no creation permission in UI', false,
    await leaderPage.evaluate(() => hasPerm('task.create')));
  // Use role assignment to test live permission changes independently of the broken save route.
  const grantedRole = await ok('/roles', 'POST', { name: 'Granted-role', permissions: ['task.create', 'task.feedback'] }, admin.token);
  await ok('/users/' + member.id, 'PUT', { role_id: grantedRole.id }, admin.token);
  const fresh = await ok('/users/me', 'GET', null, leader.token);
  assert.ok(fresh.user.permissions.includes('task.create'));
  await leaderPage.waitForTimeout(7000);
  check('open leader page refreshes externally changed permissions', true,
    await leaderPage.evaluate(() => hasPerm('task.create')));
  await leaderPage.reload();
  await leaderPage.waitForFunction(() => me && me.permissions && hasPerm('task.create'));
  console.log('PASS: full reload refreshes the leader permission cache');
  await ok('/users/' + member.id, 'PUT', { status: 'disabled' }, admin.token);
  const disabled = await request('/tasks', 'POST', { title: 'Disabled-session write' }, leader.token);
  check('disabled user cannot write through an existing session', 401, disabled.status);

  const operator = await ok('/users', 'POST', { username: 'operator-test', password: 'test-only', role: 'admin' }, admin.token);
  const operatorLogin = await ok('/auth/login', 'POST', { username: 'operator-test', password: 'test-only' });
  await ok('/users/' + operator.id, 'PUT', { role: 'user', role_id: role.id }, admin.token);
  const demoted = await request('/users', 'GET', null, operatorLogin.token);
  check('demoted administrator immediately loses user management access', 403, demoted.status);
  assert.deepEqual(errors, []);
  assert.equal(hash(), before);
  console.log('PASS: production database SHA-256 unchanged: ' + before);
  console.log('CONFIRMED FAILED CHECKS: ' + findings);
  if (findings) process.exitCode = 1;
})().catch(e => { console.error(e); process.exitCode = 2; })
  .finally(async () => { if (browser) await browser.close(); fixture.stdin.end(); });
