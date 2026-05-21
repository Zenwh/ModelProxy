/**
 * LLM Relay Console — vanilla JS, 不依赖外部库
 */

const API_BASE = window.location.pathname.replace(/\/admin\/dashboard.*$/, '');
const STORAGE_KEY = 'llm-relay-admin-key';

let adminKey = null;
let currentKeys = [];     // 缓存最近一次拉的 key 列表（含 full key）
let selectedKey = null;

// ---- 工具 -----------------------------------------------------------------

function $(id) { return document.getElementById(id); }
function show(id) { $(id).classList.remove('hidden'); }
function hide(id) { $(id).classList.add('hidden'); }

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (adminKey) headers['Authorization'] = `Bearer ${adminKey}`;
  if (options.body && typeof options.body === 'object') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const r = await fetch(API_BASE + path, { ...options, headers });
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const msg = data?.error?.message || data?.detail || `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

const fmtNum = n => (n === null || n === undefined) ? '-' : n.toLocaleString();
const fmtTime = s => !s ? '-' : s.replace('T', ' ');
const maskKey = k => (!k || k.length < 20) ? k : k.slice(0, 12) + '***' + k.slice(-4);
const escapeHtml = s => (s === null || s === undefined) ? '' :
  String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ---- 登录 -----------------------------------------------------------------

async function login() {
  const key = $('admin-key-input').value.trim();
  if (!key) return;
  adminKey = key;
  hide('login-err');
  try {
    await api('/admin/usage');
    localStorage.setItem(STORAGE_KEY, key);
    show('main-panel');
    hide('login-section');
    show('logout-btn');
    $('auth-info').textContent = `已登录 (${maskKey(key)})`;
    await refresh();
  } catch (e) {
    adminKey = null;
    $('login-err').textContent = '登录失败：' + e.message;
    show('login-err');
  }
}

function logout() {
  adminKey = null;
  localStorage.removeItem(STORAGE_KEY);
  hide('main-panel');
  show('login-section');
  hide('logout-btn');
  $('auth-info').textContent = '未登录';
  $('admin-key-input').value = '';
}

// ---- 刷新 -----------------------------------------------------------------

async function refresh() {
  hide('keys-table');
  show('keys-loading');
  show('nodes-loading');

  try {
    const [usage, health, nodes] = await Promise.all([
      api('/admin/usage'),
      api('/health').catch(() => null),
      api('/admin/nodes').catch(() => ({total: 0, online: 0, nodes: []})),
    ]);
    currentKeys = usage.keys;
    renderStats(usage, health, nodes);
    renderKeysTable(usage.keys);
    renderNodesTable(nodes);
  } catch (e) {
    if (e.message.includes('Invalid') || e.message.includes('Admin')) {
      logout();
    }
    $('keys-loading').textContent = '加载失败：' + e.message;
    $('nodes-loading').textContent = '加载失败：' + e.message;
  }
}

function renderStats(usage, health, nodes) {
  $('stat-nodes').textContent = nodes
    ? `${nodes.total} / ${nodes.total}`
    : '-';
  const activeCount = usage.keys.filter(k => k.enabled).length;
  $('stat-keys').textContent = `${activeCount} / ${usage.keys.length}`;
  $('stat-requests').textContent = fmtNum(usage.today.requests);
  $('stat-tokens').textContent = fmtNum(usage.today.total_tokens);
  if (health) {
    const mins = Math.floor(health.token_remaining_s / 60);
    const status = health.refresh_token_available ? '🟢 自动续期' : '🔴 即将过期';
    $('stat-token-remaining').textContent = `${status} (${mins}min)`;
  }
}

function renderKeysTable(keys) {
  const tbody = $('keys-tbody');
  tbody.innerHTML = '';
  if (!keys.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="muted" style="text-align:center;padding:24px;">还没有 key</td></tr>';
    hide('keys-loading');
    show('keys-table');
    return;
  }
  keys.forEach((k, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${escapeHtml(k.name)}</strong></td>
      <td><code>${escapeHtml(k.key_prefix)}</code></td>
      <td>${k.enabled
        ? '<span class="badge ok">启用</span>'
        : '<span class="badge no">禁用</span>'}</td>
      <td>${k.is_admin ? '<span class="badge admin">Admin</span>' : '<span class="muted">普通</span>'}</td>
      <td>${k.rpm_limit ? k.current_rpm + '/' + k.rpm_limit : '<span class="muted">∞</span>'}</td>
      <td>${k.daily_token_limit ? fmtNum(k.today_tokens) + '/' + fmtNum(k.daily_token_limit) : '<span class="muted">∞</span>'}</td>
      <td>${fmtNum(k.total_requests)}</td>
      <td>${fmtNum(k.today_tokens)}</td>
      <td class="muted">${fmtTime(k.last_used_at)}</td>
      <td>
        <button class="action-link" onclick="showDetailByIdx(${idx})">详情</button>
        ${k.enabled
          ? `<button class="action-link danger" onclick="revokeByIdx(${idx})">禁用</button>`
          : `<button class="action-link" onclick="enableByIdx(${idx})">启用</button>`}
        <button class="action-link" onclick="copyKey(${idx})">复制 key</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
  hide('keys-loading');
  show('keys-table');
}

// ---- 节点表 ---------------------------------------------------------------

function renderNodesTable(nodes) {
  const tbody = $('nodes-tbody');
  tbody.innerHTML = '';
  const list = nodes?.nodes || [];
  $('nodes-meta').textContent = nodes ? `共 ${nodes.total} 个节点` : '';

  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="muted" style="text-align:center;padding:24px;">还没有节点上报。先在某台机器装 feishu-relay-bot，并配 center.url</td></tr>';
    hide('nodes-loading');
    show('nodes-table');
    return;
  }

  list.forEach(n => {
    const badge = n.last_request_at
      ? '<span class="badge ok">online</span>'
      : '<span class="badge no">idle</span>';
    const models = (n.models || []).slice(0, 4).map(m => `<code style="font-size:11px;">${escapeHtml(m)}</code>`).join(' ');
    const moreModels = (n.models || []).length > 4
      ? ` <span class="muted">+${n.models.length - 4}</span>`
      : '';
    const reqTotal = fmtNum(n.request_count || 0);
    const tokTotal = fmtNum(n.total_tokens || 0);

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${badge}</td>
      <td><strong>${escapeHtml(n.node_id)}</strong></td>
      <td>${escapeHtml(n.hostname || '-')}${n.ip ? ' <span class="muted">' + escapeHtml(n.ip) + '</span>' : ''}</td>
      <td><code style="font-size:11px;">v${escapeHtml(n.version || '?')}</code></td>
      <td>${n.load != null ? n.load.toFixed(2) : '-'}</td>
      <td>${models}${moreModels}</td>
      <td>${reqTotal}</td>
      <td>${tokTotal}</td>
      <td class="muted">${fmtTime(n.started_at)}</td>
      <td class="muted">${fmtTime(n.last_request_at)}</td>
    `;
    tbody.appendChild(tr);
  });

  hide('nodes-loading');
  show('nodes-table');
}

// ---- 详情 -----------------------------------------------------------------

async function showDetailByIdx(idx) {
  const k = currentKeys[idx];
  if (!k) return;
  selectedKey = k;
  try {
    const detail = await api(`/admin/keys/${encodeURIComponent(k.key)}/usage`);
    renderDetail(detail, k);
    show('detail-section');
    $('detail-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    alert('加载详情失败：' + e.message);
  }
}

function renderDetail(d, k) {
  $('detail-name').textContent = `${d.name} (${d.key_prefix})`;

  // 按模型
  const tbm = $('detail-by-model');
  tbm.innerHTML = '';
  const models = Object.keys(d.by_model || {});
  if (!models.length) {
    tbm.innerHTML = '<tr><td colspan="4" class="muted">暂无数据</td></tr>';
  } else {
    models.forEach(m => {
      const v = d.by_model[m];
      tbm.innerHTML += `<tr>
        <td>${escapeHtml(m)}</td>
        <td>${fmtNum(v.requests)}</td>
        <td>${fmtNum(v.prompt_tokens)}</td>
        <td>${fmtNum(v.completion_tokens)}</td>
      </tr>`;
    });
  }

  // 按日（最近 14 天）
  const tbd = $('detail-by-day');
  tbd.innerHTML = '';
  const days = Object.keys(d.by_day || {}).sort().reverse().slice(0, 14);
  if (!days.length) {
    tbd.innerHTML = '<tr><td colspan="4" class="muted">暂无数据</td></tr>';
  } else {
    days.forEach(day => {
      const v = d.by_day[day];
      tbd.innerHTML += `<tr>
        <td>${day}</td>
        <td>${fmtNum(v.requests)}</td>
        <td>${fmtNum(v.prompt_tokens)}</td>
        <td>${fmtNum(v.completion_tokens)}</td>
      </tr>`;
    });
  }

  // 限额表单
  $('limit-rpm').value = k.rpm_limit ?? '';
  $('limit-daily').value = k.daily_token_limit ?? '';
  $('limit-status').textContent = '';
}

function closeDetail() {
  hide('detail-section');
  selectedKey = null;
}

async function saveLimits() {
  if (!selectedKey) return;
  const rpmRaw = $('limit-rpm').value.trim();
  const dailyRaw = $('limit-daily').value.trim();
  const body = {
    clear_rpm: rpmRaw === '',
    clear_daily: dailyRaw === '',
  };
  if (rpmRaw !== '') body.rpm_limit = parseInt(rpmRaw);
  if (dailyRaw !== '') body.daily_token_limit = parseInt(dailyRaw);
  try {
    $('limit-status').textContent = '保存中…';
    await api(`/admin/keys/${encodeURIComponent(selectedKey.key)}`, {
      method: 'PATCH',
      body,
    });
    $('limit-status').textContent = '✅ 已保存';
    await refresh();
  } catch (e) {
    $('limit-status').textContent = '失败：' + e.message;
  }
}

// ---- 操作 -----------------------------------------------------------------

async function revokeByIdx(idx) {
  const k = currentKeys[idx];
  if (!confirm(`确认禁用 key 「${k.name}」?`)) return;
  try {
    await api(`/admin/keys/${encodeURIComponent(k.key)}`, { method: 'DELETE' });
    await refresh();
  } catch (e) {
    alert('禁用失败：' + e.message);
  }
}

async function enableByIdx(idx) {
  const k = currentKeys[idx];
  try {
    await api(`/admin/keys/${encodeURIComponent(k.key)}`, {
      method: 'PATCH',
      body: { enabled: true },
    });
    await refresh();
  } catch (e) {
    alert('启用失败：' + e.message);
  }
}

async function copyKey(idx) {
  const k = currentKeys[idx];
  try {
    await navigator.clipboard.writeText(k.key);
    alert(`已复制 key 「${k.name}」 到剪贴板`);
  } catch (e) {
    prompt('请手动复制：', k.key);
  }
}

// ---- 创建 key -----------------------------------------------------------------

function openCreate() {
  $('new-key-name').value = '';
  $('new-key-admin').checked = false;
  $('new-key-rpm').value = '';
  $('new-key-daily').value = '';
  hide('new-key-result');
  show('create-modal');
}

function closeCreate() {
  hide('create-modal');
}

async function createKey() {
  const name = $('new-key-name').value.trim();
  if (!name) return alert('请填名称');
  const is_admin = $('new-key-admin').checked;
  try {
    const created = await api('/admin/keys', {
      method: 'POST',
      body: { name, is_admin },
    });
    const rpm = parseInt($('new-key-rpm').value);
    const daily = parseInt($('new-key-daily').value);
    if (!isNaN(rpm) || !isNaN(daily)) {
      await api('/admin/keys/' + encodeURIComponent(created.key), {
        method: 'PATCH',
        body: {
          rpm_limit: !isNaN(rpm) ? rpm : null,
          daily_token_limit: !isNaN(daily) ? daily : null,
        },
      });
    }
    $('new-key-display').textContent = created.key;
    show('new-key-result');
    await refresh();
  } catch (e) {
    alert('创建失败：' + e.message);
  }
}

// ---- 启动 -----------------------------------------------------------------

window.addEventListener('load', () => {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) {
    $('admin-key-input').value = stored;
    login();
  }
});
