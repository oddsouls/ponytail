#!/usr/bin/env node
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const gateway = path.join(root, 'termux', 'free_gateway.py');
const configure = path.join(root, 'termux', 'configure_opencode.py');
const install = path.join(root, 'termux', 'install.sh');
const cli = path.join(root, 'termux', 'ptmobile');
const example = path.join(root, 'termux', 'mobile.example.json');

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { cwd: root, encoding: 'utf8', ...opts });
  assert.equal(r.status, 0, `${cmd} ${args.join(' ')} failed:\n${r.stdout}\n${r.stderr}`);
  return r;
}

test('Termux profile scripts parse/compile', () => {
  run('bash', ['-n', install]);
  run('bash', ['-n', cli]);
  run('python3', ['-m', 'py_compile', gateway, configure]);
});

test('free gateway self-test covers failover, provider auth circuit and rate metadata', () => {
  const r = run('python3', [gateway, 'selftest']);
  assert.match(r.stdout, /OK: ponytail mobile gateway selftest/);
});

test('OpenCode config writer emits V2 schema and preserves unrelated config', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ponytail-mobile-v2-'));
  try {
    const dir = path.join(tmp, 'root', '.config', 'opencode');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'opencode.json'), JSON.stringify({
      plugin: ['legacy-plugin'],
      providers: { existing: { models: { x: {} } } },
      model: 'existing/x',
    }));
    run('python3', [configure, '--major', '2', '--root', tmp]);
    const cfg = JSON.parse(fs.readFileSync(path.join(dir, 'opencode.json'), 'utf8'));
    assert.deepEqual(cfg.plugins, ['legacy-plugin', '@dietrichgebert/ponytail']);
    assert.equal(cfg.plugin, undefined);
    assert.ok(cfg.providers.existing);
    assert.equal(cfg.providers['ponytail-free'].package, '@opencode/ai/providers/openai-compatible');
    assert.equal(cfg.providers['ponytail-free'].settings.apiKey, '{file:/root/.config/ponytail/gateway.token}');
    assert.equal(cfg.model, 'ponytail-free/free');
    assert.ok(fs.existsSync(path.join(dir, 'opencode.pre-ponytail-mobile.json')));
    assert.equal(fs.statSync(path.join(dir, 'opencode.json')).mode & 0o077, 0);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('OpenCode config writer emits V1 schema too', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ponytail-mobile-v1-'));
  try {
    const dir = path.join(tmp, 'root', '.config', 'opencode');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'opencode.json'), JSON.stringify({ plugins: ['newer-plugin'] }));
    run('python3', [configure, '--major', '1', '--root', tmp]);
    const cfg = JSON.parse(fs.readFileSync(path.join(dir, 'opencode.json'), 'utf8'));
    assert.deepEqual(cfg.plugin, ['newer-plugin', '@dietrichgebert/ponytail']);
    assert.equal(cfg.plugins, undefined);
    assert.equal(cfg.provider['ponytail-free'].npm, '@ai-sdk/openai-compatible');
    assert.equal(cfg.model, 'ponytail-free/free');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});


test('config writer refuses malformed existing JSON instead of overwriting it', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ponytail-mobile-badcfg-'));
  try {
    const dir = path.join(tmp, 'root', '.config', 'opencode');
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, 'opencode.json');
    fs.writeFileSync(file, '{ definitely not json');
    const r = spawnSync('python3', [configure, '--major', '2', '--root', tmp], { cwd: root, encoding: 'utf8' });
    assert.notEqual(r.status, 0);
    assert.equal(fs.readFileSync(file, 'utf8'), '{ definitely not json');
    assert.equal(fs.readFileSync(path.join(dir, 'opencode.pre-ponytail-mobile.json'), 'utf8'), '{ definitely not json');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('published package includes the Termux profile', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  assert.ok(pkg.files.includes('termux/'));
});

test('example is loopback-only, bounded, and OpenRouter cannot auto-pick paid IDs', () => {
  const cfg = JSON.parse(fs.readFileSync(example, 'utf8'));
  assert.equal(cfg.listen, '127.0.0.1');
  assert.equal(cfg.maxConcurrent, 2);
  for (const provider of cfg.providers) {
    assert.ok(provider.keyEnv);
    assert.equal('apiKey' in provider, false);
    assert.match(provider.baseURL, /^https:\/\//);
  }
  const openrouter = cfg.providers.find((p) => p.id === 'openrouter');
  assert.deepEqual(openrouter.models, ['openrouter/free']);
  assert.equal(openrouter.freeGuard, 'openrouter');
  const groq = cfg.providers.find((p) => p.id === 'groq');
  assert.ok(groq.models.includes('qwen/qwen3.8-27b'));
});

test('installer keeps upstream API keys out of the proot', () => {
  const s = fs.readFileSync(install, 'utf8');
  assert.match(s, /configure_opencode\.py/);
  assert.match(s, /gateway\.token/);
  assert.doesNotMatch(s, /GEMINI_API_KEY.*proot-distro|GROQ_API_KEY.*proot-distro|OPENROUTER_API_KEY.*proot-distro/);
});

test('CLI uses JSON secrets, not a sourced shell secret file', () => {
  const s = fs.readFileSync(cli, 'utf8');
  assert.match(s, /mobile-secrets\.json/);
  assert.doesNotMatch(s, /source\s+.*mobile|\.\s+.*mobile\.env/);
  assert.match(s, /doctor\)/);
  assert.match(s, /rotate-token\)/);
});
