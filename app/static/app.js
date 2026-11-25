'use strict';

/* ---------- helpers ---------- */

const $ = (sel) => document.querySelector(sel);

// Build DOM without innerHTML: names and descriptions come from the filesystem
// and must never be interpreted as markup.
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function append(parent, ...kids) {
  parent.append(...kids.flat(Infinity).filter((k) => k != null && k !== false));
}

// Settings live in this browser only (localStorage).
const SETTING_DEFAULTS = { theme: 'auto', size: 'normal', units: 'binary', clock: '24', tab: 'overview' };
const SETTING_VALUES = {
  theme: ['auto', 'light', 'dark'],
  size: ['small', 'normal', 'large'],
  units: ['binary', 'decimal'],
  clock: ['24', '12', 'auto'],
  tab: ['overview', 'subvolumes', 'snapper', 'schedules', 'maintenance'],
};

function loadSettings() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('settings')); } catch (e) { /* unavailable or damaged: use defaults */ }
  const out = { ...SETTING_DEFAULTS };
  for (const [key, allowed] of Object.entries(SETTING_VALUES)) {
    if (saved && allowed.includes(saved[key])) out[key] = saved[key];
  }
  return out;
}

function storeSettings() {
  try { localStorage.setItem('settings', JSON.stringify(state.settings)); } catch (e) { /* ignore */ }
}

// theme.js applies the same attributes before the page paints; this keeps them current.
function applySettings() {
  const root = document.documentElement;
  if (state.settings.theme === 'auto') delete root.dataset.theme; else root.dataset.theme = state.settings.theme;
  if (state.settings.size === 'normal') delete root.dataset.size; else root.dataset.size = state.settings.size;
}

function fmtBytes(n) {
  if (n == null) return '–';
  const decimal = state.settings.units === 'decimal';
  const base = decimal ? 1000 : 1024;
  const units = decimal ? ['B', 'kB', 'MB', 'GB', 'TB', 'PB'] : ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let i = 0, v = n;
  while (v >= base && i < units.length - 1) { v /= base; i++; }
  return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)) + ' ' + units[i];
}

function use12Hour() {
  const choice = state.settings.clock;
  if (choice !== 'auto') return choice === '12';
  return !!new Intl.DateTimeFormat(undefined, { hour: 'numeric' }).resolvedOptions().hour12;
}

// hour 0-23, minute and optional second to '16:05' or '4:05 PM'
function fmtClock(hour, minute, second) {
  const pad = (n) => String(n).padStart(2, '0');
  const rest = pad(minute) + (second == null ? '' : ':' + pad(second));
  if (!use12Hour()) return `${pad(hour)}:${rest}`;
  return `${hour % 12 || 12}:${rest} ${hour < 12 ? 'AM' : 'PM'}`;
}

// Times arrive as 'YYYY-MM-DD HH:MM' (seconds optional) in the server's time zone, the
// same clock snapper and btrfs use. Anything that does not look like that is shown as it came.
function fmtTime(text) {
  const m = /^(\d{4}-\d\d-\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?/.exec(text || '');
  return m ? `${m[1]} ${fmtClock(Number(m[2]), Number(m[3]), m[4] == null ? null : Number(m[4]))}` : (text || '');
}

const DOCS = 'https://btrfs.readthedocs.io/en/latest/';

function docsLink(page, text) {
  return h('a', {
    class: 'docs', href: DOCS + page, target: '_blank', rel: 'noopener noreferrer',
    title: 'Open the btrfs documentation in a new tab',
  }, text);
}

// The little (i) beside a heading. `about` says what the page is about, for the tooltip.
function infoLink(page, about) {
  return h('a', {
    class: 'info', href: DOCS + page, target: '_blank', rel: 'noopener noreferrer',
    title: `About ${about} (btrfs docs, opens in a new tab)`,
    'aria-label': `About ${about} in the btrfs documentation, opens in a new tab`,
  });
}

function bar(pct, extra) {
  const p = Math.max(0, Math.min(100, pct || 0));
  const span = h('span');
  span.style.width = p + '%';
  return h('div', { class: 'bar' + (p >= 95 ? ' full' : p >= 85 ? ' warn' : '') + (extra ? ' ' + extra : '') }, span);
}

function badge(text, kind) {
  return h('span', { class: 'badge' + (kind ? ' ' + kind : '') }, text);
}

// colour for a scrub / job state word
function stateKind(word) {
  if (['finished', 'done'].includes(word)) return 'ok';
  if (['running', 'paused'].includes(word)) return 'accent';
  if (['failed', 'aborted', 'interrupted'].includes(word)) return 'err';
  if (word === 'never run') return 'warn';
  return '';
}

const state = { session: null, filesystems: [], fsId: null, tab: 'overview', timer: null, settings: loadSettings() };

async function api(method, url, body) {
  const opts = { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'btrfs-ui' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* not json */ }
  if (res.status === 401 && url !== '/api/login') {
    showLogin();
    throw new Error('Sign in required');
  }
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

let noticeTimer = null;
function flash(message, isError) {
  const n = $('#notice');
  const wrap = $('#notice-wrap');
  n.textContent = message;
  n.className = isError ? 'error' : '';
  wrap.hidden = false;
  clearTimeout(noticeTimer);
  if (!isError) noticeTimer = setTimeout(() => { wrap.hidden = true; }, 5000);
}

// Run an action, report the result, refresh the view.
async function perform(fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) flash(okMessage);
    await render();
    return result;
  } catch (e) {
    flash(e.message, true);
  }
}

/* ---------- dialogs ---------- */

function ask({ title, message, fields = [], submit = 'OK', danger = false }) {
  return new Promise((resolve) => {
    const form = h('form', { method: 'dialog' });
    const dlg = h('dialog', null, form);
    const wrappers = [];
    form.append(h('h3', null, title));
    if (message) form.append(h('p', null, message));
    for (const f of fields) {
      let input;
      if (f.type === 'select') {
        input = h('select', { name: f.name },
          f.options.map(([v, l]) => h('option', { value: v, selected: v === f.value }, l)));
      } else if (f.type === 'checkbox') {
        input = h('input', { type: 'checkbox', name: f.name, checked: !!f.value });
      } else {
        input = h('input', {
          type: f.type || 'text', name: f.name, value: f.value ?? '',
          placeholder: f.placeholder, required: !!f.required, autocomplete: 'off',
        });
      }
      const label = f.type === 'checkbox'
        ? h('label', { class: 'check' }, input, f.label)
        : h('label', null, f.label);
      const wrap = h('div', { class: 'field' },
        label, f.type === 'checkbox' ? null : input,
        f.hint ? h('span', { class: 'hint' }, f.hint) : null);
      form.append(wrap);
      wrappers.push([f, wrap, input]);
    }
    // showIf: { name, is: [values] } shows a field only while another field has one of those values.
    // Hidden fields are disabled so they neither block submitting nor appear in the result.
    const syncFields = () => {
      for (const [f, wrap, input] of wrappers) {
        if (!f.showIf) continue;
        const show = f.showIf.is.includes(form.elements[f.showIf.name].value);
        wrap.hidden = !show;
        input.disabled = !show;
      }
    };
    form.addEventListener('change', syncFields);
    syncFields();
    form.append(h('div', { class: 'buttons' },
      h('button', { class: danger ? 'danger' : 'primary', value: 'ok' }, submit),
      h('button', { value: 'cancel', formnovalidate: true }, 'Cancel')));
    dlg.addEventListener('close', () => {
      let result = null;
      if (dlg.returnValue === 'ok') {
        result = {};
        for (const f of fields) {
          const el = form.elements[f.name];
          if (el.disabled) continue;
          result[f.name] = f.type === 'checkbox' ? el.checked : el.value;
        }
      }
      dlg.remove();
      resolve(result);
    });
    document.body.append(dlg);
    dlg.showModal();
    const first = form.querySelector('input:not([type=checkbox]), select');
    if (first) first.focus();
  });
}

/* ---------- views ---------- */

const TABS = [
  ['overview', 'Overview'],
  ['subvolumes', 'Subvolumes'],
  ['snapper', 'Snapper'],
  ['schedules', 'Schedules'],
  ['maintenance', 'Maintenance'],
  ['settings', 'Settings'],
];

// The Snapper tab only appears when /etc/snapper is mounted into the container.
const visibleTabs = () => TABS.filter(([id]) => id !== 'snapper' || state.session.snapper);

function panel(...kids) {
  return h('section', { class: 'panel' }, ...kids);
}

function section(title, ...kids) {
  return panel(h('h2', null, title), ...kids);
}

const SCRUB_HINT = 'No scrub history found. If scrubs have run on the host, share its ' +
  '/var/lib/btrfs directory with this container (see the README).';

function kv(pairs, extra) {
  return h('dl', { class: 'kv' + (extra ? ' ' + extra : '') },
    pairs.filter(Boolean).flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
}

function noFilesystems() {
  return h('div', { class: 'panel empty' },
    h('p', null, 'No btrfs filesystems are mounted inside this container.'),
    h('p', null, 'Bind-mount your btrfs mount points into the container, for example ',
      h('span', { class: 'mono' }, '/mnt/pool:/mnt/pool'), ', and restart it.'));
}

/* Overview */

async function viewOverview(root) {
  const d = await api('GET', `/api/fs/${state.fsId}/overview`);
  const u = d.usage || {};
  const pct = u.device_size ? (u.used / u.device_size) * 100 : 0;

  append(root, 
    section('Space',
      kv([
        ['Label', d.label || h('span', { class: 'muted' }, '(none)')],
        ['UUID', h('span', { class: 'mono' }, d.uuid)],
        ['Mounted at', d.mounts.map((m) =>
          h('div', null, h('span', { class: 'mono' }, m.path), ' ',
            h('span', { class: 'muted' }, 'subvol ' + m.subvol)))],
        ['Raw size', fmtBytes(u.device_size)],
        ['Allocated', fmtBytes(u.device_allocated)],
        ['Used', `${fmtBytes(u.used)} (${pct.toFixed(1)}%)`],
        ['Free (estimated)', u.free_estimated != null
          ? fmtBytes(u.free_estimated) + (u.free_estimated_min != null && u.free_estimated_min !== u.free_estimated
            ? ` (min ${fmtBytes(u.free_estimated_min)})` : '')
          : '–'],
      ]),
      bar(pct, 'space-bar')),

    section('Allocation',
      h('table', { class: 'cards' },
        h('thead', null, h('tr', null,
          h('th', null, 'Type'), h('th', null, 'Profile'),
          h('th', { class: 'num' }, 'Allocated'), h('th', { class: 'num' }, 'Used'), h('th', null, ''))),
        h('tbody', null, d.allocation.map((a) => h('tr', null,
          h('td', { class: 'title' }, a.type), h('td', { 'data-label': 'Profile' }, a.profile),
          h('td', { class: 'num', 'data-label': 'Allocated' }, fmtBytes(a.total)),
          h('td', { class: 'num', 'data-label': 'Used' }, fmtBytes(a.used)),
          h('td', { class: 'wide' }, a.total ? bar((a.used / a.total) * 100) : null)))))),

    section('Devices',
      h('table', { class: 'cards' },
        h('thead', null, h('tr', null,
          h('th', null, 'ID'), h('th', null, 'Device'),
          h('th', { class: 'num' }, 'Size'), h('th', { class: 'num' }, 'Allocated'), h('th', null, 'Errors'))),
        h('tbody', null, d.devices.map((dev) => h('tr', null,
          h('td', { 'data-label': 'ID' }, dev.devid),
          h('td', { class: 'mono title' }, dev.path, dev.missing ? [' ', badge('missing', 'err')] : null),
          h('td', { class: 'num', 'data-label': 'Size' }, fmtBytes(dev.size)),
          h('td', { class: 'num', 'data-label': 'Allocated' }, fmtBytes(dev.used)),
          h('td', { 'data-label': 'Errors' }, errorCell(dev))))))),

    section('Status',
      kv([
        ['Scrub', h('div', null, badge(d.scrub.status, stateKind(d.scrub.status)), ' ',
          h('a', { href: '#maintenance' }, 'details'),
          d.scrub.status === 'never run' ? h('p', { class: 'muted' }, SCRUB_HINT) : null)],
        ['Balance', badge(d.balance.state === 'none' ? 'not running' : d.balance.state,
          stateKind(d.balance.state))],
      ])),

    d.warnings.length ? section('Warnings', d.warnings.map((w) => h('p', { class: 'warn' }, w))) : null,

    panel(h('details', null,
      h('summary', null, 'Raw filesystem usage'),
      h('pre', null, d.usage_text || '(unavailable)'))));
}

function errorCell(dev) {
  if (!dev.error_total) return badge('none');
  const labels = [['write_io_errs', 'write'], ['read_io_errs', 'read'], ['flush_io_errs', 'flush'],
    ['corruption_errs', 'corruption'], ['generation_errs', 'generation']];
  return badge(labels.filter(([k]) => dev.errors[k]).map(([k, l]) => `${l} ${dev.errors[k]}`).join(', '), 'err');
}

/* Subvolumes */

const SNAPPER_PATH = /(^|\/)\.snapshots\/\d+\/snapshot$/;

async function viewSubvolumes(root) {
  const d = await api('GET', `/api/fs/${state.fsId}/subvolumes`);
  const all = d.subvolumes;
  // keep the filter across refreshes (every action re-renders the view)
  const hideSnapper = h('input', { type: 'checkbox', checked: state.svHide !== false });
  const filter = h('input', { type: 'search', placeholder: 'Filter by path', value: state.svFilter || '' });
  const tbody = h('tbody');
  const count = h('span', { class: 'muted' });

  function drawRows() {
    const q = filter.value.trim().toLowerCase();
    const rows = all.filter((s) =>
      (!hideSnapper.checked || !SNAPPER_PATH.test(s.path)) && (!q || s.path.toLowerCase().includes(q)));
    count.textContent = `${rows.length} of ${all.length}`;
    tbody.replaceChildren(...rows.map(subvolRow));
    if (!rows.length) {
      tbody.append(h('tr', null, h('td', { colspan: 6, class: 'muted wide' }, 'No subvolumes match.')));
    }
  }

  function subvolRow(s) {
    const flags = [];
    if (s.readonly) flags.push(badge('read-only'));
    if (s.default) flags.push(badge('default', 'accent'));
    if (s.mounted) flags.push(badge('mounted', 'ok'));
    return h('tr', null,
      h('td', { class: 'mono title' }, s.path),
      h('td', { 'data-label': 'ID' }, s.id),
      h('td', { 'data-label': 'Type' }, s.snapshot ? 'snapshot' : 'subvolume',
        s.created ? h('div', { class: 'muted' }, fmtTime(s.created)) : null),
      h('td', { class: 'num', 'data-label': 'Size' }, s.referenced != null
        ? h('span', null, fmtBytes(s.referenced), h('div', { class: 'muted' }, fmtBytes(s.exclusive) + ' excl.')) : ''),
      h('td', { class: 'wide' }, flags),
      h('td', { class: 'actions wide' },
        h('button', { class: 'link', disabled: !s.abs, onclick: () => snapshotDialog(s) }, 'Snapshot'),
        h('button', { class: 'link', disabled: !s.abs, onclick: () => toggleReadonly(s) },
          s.readonly ? 'Make writable' : 'Make read-only'),
        h('button', { class: 'link danger', disabled: s.default || s.mounted, onclick: () => deleteSubvol(s) }, 'Delete')));
  }

  async function snapshotDialog(s) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}`;
    const r = await ask({
      title: 'Create snapshot',
      message: `Snapshot of ${s.path}`,
      fields: [
        { name: 'dest', label: 'Destination path', value: `${s.path}-${stamp}`, required: true,
          hint: 'Relative to the filesystem top level. Must not exist yet.' },
        { name: 'readonly', label: 'Read-only', type: 'checkbox', value: true },
      ],
      submit: 'Create snapshot',
    });
    if (r) perform(() => api('POST', `/api/fs/${state.fsId}/subvolumes/snapshot`,
      { id: s.id, dest: r.dest, readonly: r.readonly }), 'Snapshot created');
  }

  async function toggleReadonly(s) {
    const r = await ask({
      title: s.readonly ? 'Make writable?' : 'Make read-only?',
      message: s.readonly
        ? `${s.path} will become writable. If it was received with btrfs receive, this breaks incremental sends.`
        : `${s.path} will become read-only.`,
      submit: 'Confirm',
    });
    if (r) perform(() => api('POST', `/api/fs/${state.fsId}/subvolumes/readonly`,
      { id: s.id, readonly: !s.readonly }), 'Updated');
  }

  async function deleteSubvol(s) {
    const r = await ask({
      title: 'Delete subvolume?',
      message: `${s.path} (ID ${s.id}) and everything in it will be deleted. This cannot be undone.`,
      submit: 'Delete', danger: true,
    });
    if (r) perform(() => api('POST', `/api/fs/${state.fsId}/subvolumes/delete`, { id: s.id }), 'Deleted');
  }

  async function createDialog() {
    const r = await ask({
      title: 'New subvolume',
      fields: [{ name: 'path', label: 'Path', required: true, placeholder: '@data/photos',
        hint: 'Relative to the filesystem top level. The parent directory must exist.' }],
      submit: 'Create',
    });
    if (r) perform(() => api('POST', `/api/fs/${state.fsId}/subvolumes/create`, { path: r.path }), 'Subvolume created');
  }

  filter.addEventListener('input', () => { state.svFilter = filter.value; drawRows(); });
  hideSnapper.addEventListener('change', () => { state.svHide = hideSnapper.checked; drawRows(); });

  append(root, 
    h('div', { class: 'toolbar' },
      filter,
      h('label', { class: 'check' }, hideSnapper, 'Hide snapper snapshots'),
      count,
      h('span', { class: 'spacer' }),
      infoLink('Subvolumes.html', 'subvolumes'),
      h('button', { class: 'primary', onclick: createDialog }, 'New subvolume')),
    d.top_level_mounted ? null : h('div', { class: 'panel warn' },
      'The filesystem top level (subvolid=5) is not mounted in this container, so some subvolumes are not reachable.'),
    panel(
      h('table', { class: 'cards' },
        h('thead', null, h('tr', null,
          h('th', null, 'Path'), h('th', null, 'ID'), h('th', null, 'Type'),
          h('th', { class: 'num' }, d.quotas ? 'Size' : ''), h('th', null, ''), h('th', null, ''))),
        tbody),
      d.quotas ? null : h('p', { class: 'muted' },
        'Sizes appear here when quotas are enabled (btrfs quota enable). ', infoLink('Qgroups.html', 'quotas'))));
  drawRows();
}

/* Snapper */

async function viewSnapper(root) {
  const cfgs = await api('GET', '/api/snapper/configs');
  if (!cfgs.available) {
    append(root, h('div', { class: 'panel empty' }, 'snapper is not installed in this container.'));
    return;
  }
  if (!cfgs.configs.length) {
    append(root, h('div', { class: 'panel empty' },
      h('p', null, 'No snapper configurations found.'),
      h('p', null, 'Mount ', h('span', { class: 'mono' }, '/etc/snapper'),
        ' from the host into the container, and mount the snapshotted directories at the same paths as on the host.')));
    return;
  }

  const current = sessionStorage.getItem('snapperConfig');
  let name = cfgs.configs.some((c) => c.config === current) ? current : cfgs.configs[0].config;
  const picker = h('select', null, cfgs.configs.map((c) =>
    h('option', { value: c.config, selected: c.config === name }, `${c.config}  —  ${c.subvolume}`)));
  const body = h('div', { class: 'stack' });
  picker.addEventListener('change', () => {
    name = picker.value;
    sessionStorage.setItem('snapperConfig', name);
    loadConfig();
  });
  append(root, h('div', { class: 'toolbar' }, picker), body);

  async function loadConfig() {
    body.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
    let snaps, settings;
    try {
      [snaps, settings] = await Promise.all([
        api('GET', `/api/snapper/${name}/snapshots`),
        api('GET', `/api/snapper/${name}/settings`)]);
    } catch (e) {
      body.replaceChildren(panel(h('p', { class: 'err' }, e.message)));
      return;
    }
    drawConfig(snaps, settings);
  }

  function drawConfig(snaps, settings) {
    const selected = new Set();
    const compareBtn = h('button', { disabled: true }, 'Show changes');
    const deleteBtn = h('button', { class: 'danger', disabled: true }, 'Delete selected');
    const changes = h('div', { class: 'stack' });

    function syncButtons() {
      deleteBtn.disabled = selected.size === 0;
      compareBtn.disabled = selected.size !== 2;
    }

    const rows = [...snaps].reverse().map((s) => {
      const box = h('input', { type: 'checkbox' });
      box.addEventListener('change', () => {
        if (box.checked) selected.add(s.number); else selected.delete(s.number);
        syncButtons();
      });
      return h('tr', null,
        h('td', { class: 'pick' }, box), h('td', { class: 'title' }, s.number),
        h('td', { 'data-label': 'Type' }, s.type + (s.pre_number ? ` (of ${s.pre_number})` : '')),
        h('td', { 'data-label': 'Date' }, fmtTime(s.date)), h('td', { 'data-label': 'User' }, s.user),
        h('td', { class: 'muted', 'data-label': 'Cleanup' }, s.cleanup),
        h('td', { class: 'wide' }, s.description, s.userdata ? h('div', { class: 'muted' }, s.userdata) : null));
    });

    deleteBtn.addEventListener('click', async () => {
      const nums = [...selected].sort((a, b) => a - b);
      const r = await ask({
        title: 'Delete snapshots?',
        message: `Snapshot${nums.length > 1 ? 's' : ''} ${nums.join(', ')} will be deleted.`,
        submit: 'Delete', danger: true,
      });
      if (r) perform(() => api('POST', `/api/snapper/${name}/delete`, { numbers: nums }), 'Deleted');
    });

    compareBtn.addEventListener('click', async () => {
      const [a, b] = [...selected].sort((x, y) => x - y);
      changes.replaceChildren(panel(h('p', { class: 'muted' }, 'Comparing…')));
      try {
        const d = await api('GET', `/api/snapper/${name}/status?from=${a}&to=${b}`);
        changes.replaceChildren(panel(
          h('h2', null, `Changes from #${a} to #${b}`, h('small', null, `${d.changes.length}${d.truncated ? '+' : ''} files`)),
          d.changes.length
            ? h('table', null, h('tbody', null, d.changes.map((c) => h('tr', null,
              h('td', { class: 'mono' }, c.flags), h('td', { class: 'mono' }, c.path)))))
            : h('p', { class: 'muted' }, 'No differences.'),
          d.truncated ? h('p', { class: 'muted' }, 'List truncated.') : null));
      } catch (e) {
        changes.replaceChildren(panel(h('p', { class: 'err' }, e.message)));
      }
    });

    async function createSnapshot() {
      const r = await ask({
        title: 'Create snapshot',
        fields: [
          { name: 'description', label: 'Description', value: 'manual' },
          { name: 'cleanup', label: 'Cleanup algorithm', type: 'select', value: '',
            options: [['', 'None (keep until deleted)'], ['number', 'number'], ['timeline', 'timeline']] },
        ],
        submit: 'Create snapshot',
      });
      if (r) perform(() => api('POST', `/api/snapper/${name}/snapshots`, r), 'Snapshot created');
    }

    async function runCleanup() {
      const r = await ask({
        title: 'Run cleanup',
        message: 'Deletes snapshots that exceed the limits in the settings below.',
        fields: [{ name: 'algorithm', label: 'Algorithm', type: 'select', value: 'number',
          options: [['number', 'number'], ['timeline', 'timeline'], ['empty-pre-post', 'empty-pre-post']] }],
        submit: 'Run cleanup',
      });
      if (r) perform(() => api('POST', `/api/snapper/${name}/cleanup`, r), 'Cleanup started (see Maintenance → Jobs)');
    }

    const SETTING_LABELS = [
      ['TIMELINE_CREATE', 'Create timeline snapshots', 'bool'],
      ['TIMELINE_CLEANUP', 'Clean up timeline snapshots', 'bool'],
      ['TIMELINE_LIMIT_HOURLY', 'Keep hourly', 'text'],
      ['TIMELINE_LIMIT_DAILY', 'Keep daily', 'text'],
      ['TIMELINE_LIMIT_WEEKLY', 'Keep weekly', 'text'],
      ['TIMELINE_LIMIT_MONTHLY', 'Keep monthly', 'text'],
      ['TIMELINE_LIMIT_YEARLY', 'Keep yearly', 'text'],
      ['TIMELINE_MIN_AGE', 'Timeline minimum age (seconds)', 'text'],
      ['NUMBER_CLEANUP', 'Clean up numbered snapshots', 'bool'],
      ['NUMBER_LIMIT', 'Keep numbered', 'text'],
      ['NUMBER_LIMIT_IMPORTANT', 'Keep numbered (important)', 'text'],
      ['NUMBER_MIN_AGE', 'Number minimum age (seconds)', 'text'],
      ['EMPTY_PRE_POST_CLEANUP', 'Clean up empty pre/post pairs', 'bool'],
      ['EMPTY_PRE_POST_MIN_AGE', 'Empty pair minimum age (seconds)', 'text'],
    ];
    const inputs = {};
    const grid = h('div', { class: 'form-grid' }, SETTING_LABELS.flatMap(([key, label, kind]) => {
      const val = settings.values[key] ?? '';
      inputs[key] = kind === 'bool'
        ? h('select', null, ['yes', 'no'].map((o) => h('option', { value: o, selected: o === val }, o)))
        : h('input', { type: 'text', value: val, size: 8 });
      return [h('label', null, label), inputs[key]];
    }));
    const saveBtn = h('button', { onclick: () => {
      const values = {};
      for (const [key] of SETTING_LABELS) values[key] = inputs[key].value;
      perform(() => api('POST', `/api/snapper/${name}/settings`, { values }), 'Settings saved');
    } }, 'Save settings');

    body.replaceChildren(
      h('div', { class: 'toolbar' },
        h('button', { class: 'primary', onclick: createSnapshot }, 'Create snapshot'),
        compareBtn, deleteBtn,
        h('span', { class: 'spacer' }),
        h('button', { onclick: runCleanup }, 'Run cleanup…')),
      panel(snaps.length
        ? h('table', { class: 'cards snaps' },
          h('thead', null, h('tr', null,
            h('th', null, ''), h('th', null, '#'), h('th', null, 'Type'), h('th', null, 'Date'),
            h('th', null, 'User'), h('th', null, 'Cleanup'), h('th', null, 'Description'))),
          h('tbody', null, rows))
        : h('p', { class: 'empty' }, 'No snapshots yet.'),
        snaps.length ? h('p', { class: 'muted' }, 'Tick two snapshots to compare them.') : null),
      changes,
      panel(h('details', null, h('summary', null, 'Settings for ' + name),
        h('p', { class: 'muted' }, settings.subvolume), grid, h('div', { class: 'row' }, saveBtn))));
  }

  await loadConfig();
}

/* Schedules */

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function whenText(r) {
  const [hour, minute] = r.time.split(':').map(Number);
  const at = fmtClock(hour, minute);
  if (r.frequency === 'hourly') return `Every hour at :${r.time.slice(3)}`;
  if (r.frequency === 'daily') return `Every day at ${at}`;
  if (r.frequency === 'weekly') return `Every ${WEEKDAYS[r.weekday]} at ${at}`;
  return `Monthly on day ${r.monthday} at ${at}`;
}

function ruleName(r) {
  return r.type === 'snapshot' ? 'Snapshot of ' + r.source : { scrub: 'Scrub', balance: 'Balance', trim: 'Trim' }[r.type];
}

// The grey line under a task's name.
function ruleDetail(r) {
  if (r.type === 'snapshot') return `${r.dest}/${r.prefix}-…, keep ${r.keep}`;
  if (r.type !== 'balance') return null;
  const parts = [];
  if (r.data_usage != null) parts.push(`data chunks under ${r.data_usage}% full`);
  if (r.metadata_usage != null) parts.push(`metadata chunks under ${r.metadata_usage}% full`);
  return parts.join(', ');
}

function resultKind(status) {
  return { ok: 'ok', error: 'err', skipped: 'warn' }[status] || '';
}

async function viewSchedules(root) {
  const id = state.fsId;
  const box = h('div', { class: 'stack' });
  let sig = '';

  function ruleRow(r) {
    return h('tr', null,
      h('td', { class: 'title' },
        r.type === 'snapshot' ? h('span', null, 'Snapshot of ', h('span', { class: 'mono' }, r.source)) : ruleName(r),
        ruleDetail(r) ? h('div', { class: 'muted' }, ruleDetail(r)) : null),
      h('td', { 'data-label': 'Frequency' }, whenText(r)),
      h('td', { class: 'nowrap', 'data-label': 'Next run' }, r.enabled ? fmtTime(r.next_text) : badge('paused', 'warn')),
      h('td', { class: 'nowrap', 'data-label': 'Last run' },
        r.last_status ? badge(r.last_status, resultKind(r.last_status)) : h('span', { class: 'muted' }, 'not yet'),
        r.last_text ? h('div', { class: 'muted' }, fmtTime(r.last_text)) : null),
      h('td', { class: 'actions wide' },
        h('button', { class: 'link', disabled: ro, onclick: () => runNow(r) }, 'Run now'),
        h('button', { class: 'link', disabled: ro, onclick: () => setEnabled(r, !r.enabled) },
          r.enabled ? 'Pause' : 'Resume'),
        h('button', { class: 'link danger', disabled: ro, onclick: () => remove(r) }, 'Delete')));
  }

  let ro = false;

  function draw(o) {
    ro = o.readonly;
    const mine = o.rules.filter((r) => r.fs === id);
    const log = o.history.filter((e) => e.fs === id).slice(0, 30);
    newBtn.disabled = !o.enabled || o.readonly;
    const parts = [];
    if (!o.enabled) {
      parts.push(h('div', { class: 'panel warn' },
        o.missing
          ? h('p', null, 'Schedules are off because this container has nowhere to keep them. Mount a folder at ',
            h('span', { class: 'mono' }, o.dataDir), ' and restart, for example ',
            h('span', { class: 'mono' }, `- ./data:${o.dataDir}`), ' under volumes in your compose file.')
          : h('p', null, o.reason)));
    } else if (o.readonly) {
      parts.push(h('div', { class: 'panel warn' }, 'Read-only mode is on, so schedules are shown but will not run.'));
    }
    if (o.enabled) {
      parts.push(panel(mine.length
        ? h('table', { class: 'cards' },
          h('thead', null, h('tr', null, ['Task', 'Frequency', 'Next run', 'Last run', ''].map((t) => h('th', null, t)))),
          h('tbody', null, mine.map(ruleRow)))
        : h('p', { class: 'muted' }, 'No schedules yet for this filesystem.')));
      parts.push(section('Recent runs', log.length
        ? h('table', { class: 'cards' }, h('tbody', null, log.map((e) => h('tr', null,
          h('td', { class: 'nowrap' }, fmtTime(e.time_text)),
          h('td', { class: 'title' }, e.what),
          h('td', null, badge(e.status, resultKind(e.status))),
          h('td', { class: 'wide' }, e.message, e.trigger === 'manual' ? h('span', { class: 'muted' }, ' (run by hand)') : null)))))
        : h('p', { class: 'muted' }, 'Nothing has run yet.')));
    }
    box.replaceChildren(...parts);
  }

  async function refresh(force) {
    const o = await api('GET', '/api/schedules');
    const next = JSON.stringify([o, id]);
    if (force || next !== sig) { sig = next; draw(o); }
  }

  async function runNow(r) {
    await perform(async () => {
      const res = (await api('POST', `/api/schedules/${r.id}/run`, {})).result;
      flash(res.message, res.status === 'error');
    });
  }

  function setEnabled(r, enabled) {
    perform(() => api('POST', `/api/schedules/${r.id}/enabled`, { enabled }), enabled ? 'Resumed' : 'Paused');
  }

  async function remove(r) {
    const ok = await ask({
      title: 'Delete schedule?',
      message: `${ruleName(r)} (${whenText(r)}). ` +
        'Snapshots it already made are kept.',
      submit: 'Delete', danger: true,
    });
    if (ok) perform(() => api('POST', `/api/schedules/${r.id}/delete`, {}), 'Schedule deleted');
  }

  async function createDialog() {
    const subs = (await api('GET', `/api/fs/${id}/subvolumes`)).subvolumes
      .filter((s) => s.abs && !SNAPPER_PATH.test(s.path));
    const snap = { name: 'type', is: ['snapshot'] };
    const bal = { name: 'type', is: ['balance'] };
    const r = await ask({
      title: 'New schedule',
      fields: [
        { name: 'type', label: 'What to run', type: 'select', value: 'snapshot',
          options: [['snapshot', 'Snapshot with automatic cleanup'], ['scrub', 'Scrub'],
            ['balance', 'Balance'], ['trim', 'Trim (SSDs)']] },
        { name: 'source', label: 'Subvolume', type: 'select', showIf: snap,
          options: subs.map((s) => [s.path, s.path]) },
        { name: 'dest', label: 'Save snapshots in', required: true, placeholder: 'snaps', showIf: snap,
          hint: 'A folder that already exists, relative to the filesystem top level.' },
        { name: 'prefix', label: 'Name prefix', placeholder: 'defaults to the subvolume name', showIf: snap,
          hint: 'Snapshots are named prefix-YYYY-MM-DD_HHMM. Only snapshots with this name pattern are ever cleaned up.' },
        { name: 'keep', label: 'Keep the newest', type: 'number', value: 7, required: true, showIf: snap,
          hint: 'Older snapshots made by this schedule are deleted after each run.' },
        { name: 'data_usage', label: 'Data chunks under (% full)', type: 'number', value: 10, showIf: bal,
          hint: 'Only chunks less full than this are rewritten, so it stays gentle. Leave it empty to skip data.' },
        { name: 'metadata_usage', label: 'Metadata chunks under (% full)', type: 'number', value: 5, showIf: bal,
          hint: 'Fill in at least one of the two.' },
        { name: 'frequency', label: 'How often', type: 'select', value: 'daily',
          options: [['hourly', 'Hourly'], ['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly']] },
        { name: 'minute', label: 'Minute past the hour', type: 'number', value: 0, required: true,
          showIf: { name: 'frequency', is: ['hourly'] } },
        { name: 'weekday', label: 'Day of the week', type: 'select', value: '6',
          options: WEEKDAYS.map((d, i) => [String(i), d]), showIf: { name: 'frequency', is: ['weekly'] } },
        { name: 'monthday', label: 'Day of the month', type: 'number', value: 1, required: true,
          hint: '1 to 28.', showIf: { name: 'frequency', is: ['monthly'] } },
        { name: 'time', label: 'At', type: 'time', value: '03:00', required: true,
          showIf: { name: 'frequency', is: ['daily', 'weekly', 'monthly'] } },
      ],
      submit: 'Create schedule',
    });
    if (r) perform(() => api('POST', '/api/schedules', { ...r, fs: id }), 'Schedule created');
  }

  const newBtn = h('button', { class: 'primary', onclick: createDialog }, 'New schedule');
  append(root, h('div', { class: 'toolbar' }, h('span', { class: 'spacer' }), newBtn), box);
  await refresh(true);
  state.timer = setInterval(() => refresh().catch(() => {}), 15000);
}

/* Maintenance */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const BINARY = { '': 1, Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4, Pi: 1024 ** 5, Ei: 1024 ** 6 };

// `btrfs scrub status` prints times like 'Sat Nov 14 11:34:19 2026' and sizes like
// '80.72MiB' or '80.75MiB/s (some device limits set)'. Show those in the chosen
// formats and leave any other line exactly as printed.
function fmtScrubValue(text) {
  const t = /^[A-Z][a-z]{2} ([A-Z][a-z]{2}) +(\d{1,2}) (\d\d:\d\d:\d\d) (\d{4})$/.exec(text);
  if (t && MONTHS.includes(t[1])) {
    const month = String(MONTHS.indexOf(t[1]) + 1).padStart(2, '0');
    return fmtTime(`${t[4]}-${month}-${t[2].padStart(2, '0')} ${t[3]}`);
  }
  const n = /^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei)?B(\/s)?(.*)$/.exec(text);
  if (n) return fmtBytes(Number(n[1]) * BINARY[n[2] || '']) + (n[3] || '') + n[4];
  return text;
}

async function viewMaintenance(root) {
  const id = state.fsId;

  // scrub
  const scrubStatus = h('div');
  const scrubRO = h('input', { type: 'checkbox' });
  const scrubStart = h('button', { class: 'primary' }, 'Start scrub');
  const scrubCancel = h('button', null, 'Cancel');
  const scrubResume = h('button', null, 'Resume');
  const scrubAct = (action) => perform(
    () => api('POST', `/api/fs/${id}/scrub`, { action, readonly: scrubRO.checked }), 'Done');
  scrubStart.onclick = () => scrubAct('start');
  scrubCancel.onclick = () => scrubAct('cancel');
  scrubResume.onclick = () => scrubAct('resume');

  // balance
  const balStatus = h('div');
  const dUsage = h('input', { type: 'number', min: 0, max: 100, value: 50, size: 4 });
  const mUsage = h('input', { type: 'number', min: 0, max: 100, value: 50, size: 4 });
  const balStart = h('button', { class: 'primary' }, 'Start balance');
  const balCancel = h('button', null, 'Cancel');
  balStart.onclick = async () => {
    const r = await ask({
      title: 'Start balance?',
      message: 'Balance rewrites chunks and can take a long time and add load. ' +
        'Only chunks less than the given percent full are rewritten.',
      submit: 'Start',
    });
    if (!r) return;
    perform(() => api('POST', `/api/fs/${id}/balance`, {
      action: 'start',
      data_usage: dUsage.value === '' ? null : dUsage.value,
      metadata_usage: mUsage.value === '' ? null : mUsage.value,
    }), 'Balance started');
  };
  balCancel.onclick = () => perform(() => api('POST', `/api/fs/${id}/balance`, { action: 'cancel' }), 'Cancel requested');

  // defrag
  const defragPath = h('input', { type: 'text', placeholder: '@data/photos', size: 28 });
  const defragComp = h('select', null, [['', 'No recompression'], ['zstd', 'zstd'], ['zlib', 'zlib'], ['lzo', 'lzo']]
    .map(([v, l]) => h('option', { value: v }, l)));
  const defragSize = h('input', { type: 'text', placeholder: '32M', size: 6 });
  const defragBtn = h('button', null, 'Defragment');
  defragBtn.onclick = async () => {
    if (!defragPath.value.trim()) { flash('Enter a path to defragment', true); return; }
    const r = await ask({
      title: 'Defragment?',
      message: 'Defragmenting breaks the sharing between snapshots and the original data, ' +
        'so space usage can grow a lot if the path has snapshots.',
      submit: 'Defragment',
    });
    if (!r) return;
    perform(() => api('POST', `/api/fs/${id}/defrag`, {
      path: defragPath.value, recursive: true,
      compression: defragComp.value, target_size: defragSize.value,
    }), 'Defragment started');
  };

  const trimBtn = h('button', null, 'Run trim');
  trimBtn.onclick = () => perform(() => api('POST', `/api/fs/${id}/trim`, {}), 'Trim started');

  const jobsBox = h('div');

  append(root, 
    section(['Scrub', infoLink('Scrub.html', 'scrub')],
      scrubStatus,
      h('div', { class: 'row' }, scrubStart, scrubResume, scrubCancel,
        h('label', { class: 'check' }, scrubRO, 'Check only, do not repair'))),
    section(['Balance', infoLink('Balance.html', 'balance')],
      balStatus,
      h('div', { class: 'row' },
        h('label', { class: 'check' }, 'Data chunks under', dUsage, '% full'),
        h('label', { class: 'check' }, 'Metadata chunks under', mUsage, '% full')),
      h('div', { class: 'row' }, balStart, balCancel)),
    section(['Defragment', infoLink('Defragmentation.html', 'defragmenting')],
      h('div', { class: 'row first' },
        h('label', { class: 'check grow' }, 'Folder', defragPath), defragComp,
        h('label', { class: 'check' }, 'Target extent size', defragSize), defragBtn)),
    section(['Trim', infoLink('Trim.html', 'trim')],
      h('p', null, 'Discard unused blocks on SSDs.'), h('div', { class: 'row' }, trimBtn)),
    section('Jobs', jobsBox));

  function drawScrub(s) {
    // the status word is already shown as a badge, so drop it from the detail rows
    const rows = s.pairs.filter(([k]) => k !== 'Status').map(([k, v]) => [k, fmtScrubValue(v)]);
    scrubStatus.replaceChildren(...[
      h('p', null, badge(s.status, stateKind(s.status))),
      s.status === 'never run' ? h('p', { class: 'muted' }, SCRUB_HINT) : null,
      rows.length ? kv(rows) : (s.status === 'never run' ? null : h('pre', null, s.text || 'No status')),
    ].filter(Boolean));
    scrubStart.disabled = s.running;
    scrubCancel.disabled = !s.running;
    scrubResume.disabled = s.running || !['aborted', 'interrupted'].includes(s.status);
  }

  function drawBalance(b) {
    balStatus.replaceChildren(h('p', null, b.text || 'No status'));
    balStart.disabled = b.state !== 'none';
    balCancel.disabled = !['running', 'paused'].includes(b.state);
  }

  let jobsSig = '';
  function drawJobs(list) {
    const sig = JSON.stringify(list);
    if (sig === jobsSig) return;
    jobsSig = sig;
    const open = new Set([...jobsBox.querySelectorAll('details[open]')].map((d) => d.dataset.id));
    if (!list.length) { jobsBox.replaceChildren(h('p', { class: 'muted' }, 'No jobs yet.')); return; }
    jobsBox.replaceChildren(h('table', { class: 'cards' }, h('tbody', null, list.map((j) => {
      const det = h('details', { 'data-id': j.id },
        h('summary', null, 'output'),
        h('pre', null, j.output.join('\n') || '(no output)'));
      if (open.has(j.id)) det.open = true;
      return h('tr', null,
        h('td', { class: 'title' }, j.title, h('div', { class: 'muted' }, fmtTime(j.started_text)), det),
        h('td', null, badge(j.state, stateKind(j.state))),
        h('td', { class: 'actions' }, j.state === 'running'
          ? h('button', { class: 'link', onclick: () => perform(() => api('POST', `/api/jobs/${j.id}/cancel`, {})) }, 'Cancel')
          : null));
    }))));
  }

  async function refresh() {
    const [s, b, j] = await Promise.all([
      api('GET', `/api/fs/${id}/scrub`), api('GET', `/api/fs/${id}/balance`), api('GET', '/api/jobs')]);
    drawScrub(s); drawBalance(b); drawJobs(j);
  }
  await refresh();
  state.timer = setInterval(() => refresh().catch(() => {}), 3000);
}

/* Settings */

function viewSettings(root) {
  const sess = state.session;
  const preview = h('span', { class: 'mono' });
  const showPreview = () => {
    preview.textContent = `${fmtBytes(1.5 * 2 ** 40)}   ·   ${fmtTime('2026-11-23 16:05')}`;
  };

  function choice(label, key, options, hint) {
    const sel = h('select', null, options.map(([v, l]) => h('option', { value: v, selected: v === state.settings[key] }, l)));
    sel.addEventListener('change', () => {
      state.settings[key] = sel.value;
      storeSettings();
      applySettings();
      showPreview();
    });
    return [label, h('div', null, sel, hint ? h('p', { class: 'muted' }, hint) : null)];
  }

  const tabOptions = visibleTabs().filter(([id]) => id !== 'settings');
  const display = kv([
    choice('Theme', 'theme', [['auto', 'Automatic (follows your system)'], ['light', 'Light'], ['dark', 'Dark']]),
    choice('Text size', 'size', [['small', 'Small'], ['normal', 'Normal'], ['large', 'Large']]),
    choice('File sizes', 'units', [['binary', 'Binary (KiB, MiB, GiB, TiB)'], ['decimal', 'Decimal (kB, MB, GB, TB)']],
      'Only changes how sizes are shown. Raw command output stays as btrfs prints it, and sizes you type, like 32M, are always binary.'),
    choice('Time format', 'clock', [['24', '24-hour'], ['12', '12-hour'], ['auto', 'Your browser\'s setting']],
      `Times are in the server's time zone, ${sess.timezone}, the same clock snapper and your schedules use. ` +
      'Set TZ in the compose file to change it.'),
    choice('Opens to', 'tab', tabOptions.map(([id, label]) => [id, label])),
  ], 'form');
  showPreview();

  const reset = h('button', { onclick: () => {
    state.settings = { ...SETTING_DEFAULTS };
    storeSettings();
    applySettings();
    render();
  } }, 'Reset to defaults');

  const test = h('button', { onclick: async () => {
    test.disabled = true;
    try {
      await api('POST', '/api/notify/test');
      flash('Test message sent');
    } catch (e) {
      flash(e.message, true);
    }
    test.disabled = false;
  } }, 'Send test message');

  const notifications = sess.webhook
    ? [
      h('p', null,
        `Messages go to ${sess.webhook} when a scrub finds errors or a scheduled snapshot or scrub fails` +
        (sess.webhookScrubOk ? ', and when a scrub finishes cleanly.' : '.')),
      h('div', { class: 'row first' }, test),
    ]
    : [h('p', null, 'Not set up. Set ', h('span', { class: 'mono' }, 'BTRFS_UI_WEBHOOK_URL'),
      ' in the compose file to get a message when a scrub finds errors or a schedule fails.')];

  append(root,
    section('Display',
      display,
      h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Preview'), preview),
      h('div', { class: 'row' }, reset),
      h('p', { class: 'muted' }, 'These settings are saved in this browser only.')),
    section('Notifications', ...notifications),
    section('About',
      kv([
        ['btrfs-ui', sess.version],
        sess.btrfsProgs ? ['btrfs-progs', sess.btrfsProgs] : null,
        ['Server time zone', sess.timezone],
        ['Mode', sess.readonly ? 'Read-only (browse only)' : 'Read-write'],
        ['Schedules', sess.schedules ? 'On' : 'Off (no data folder, see the Schedules tab)'],
      ]),
      h('p', null, docsLink('index.html', 'btrfs documentation'))));
}

/* ---------- shell ---------- */

const VIEWS = {
  overview: viewOverview, subvolumes: viewSubvolumes, snapper: viewSnapper,
  schedules: viewSchedules, maintenance: viewMaintenance, settings: viewSettings,
};

async function render() {
  clearInterval(state.timer);
  const root = $('#view');
  const needsFs = !['snapper', 'settings'].includes(state.tab);
  $('#fs').hidden = !state.filesystems.length;   // always shown, so the header does not jump between tabs
  document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.tab === state.tab));
  const holder = h('div');
  if (needsFs && !state.fsId) { root.replaceChildren(noFilesystems()); return; }
  try {
    await VIEWS[state.tab](holder);
    root.replaceChildren(...holder.childNodes);
  } catch (e) {
    if (e.message !== 'Sign in required') root.replaceChildren(panel(h('p', { class: 'err' }, e.message)));
  }
}

// The tab row scrolls sideways on a narrow screen: fade the edge that is cut off, and keep
// the tab you are on in view.
function tabEdges() {
  const row = $('#tabs');
  const nav = row.parentElement;
  nav.classList.toggle('more-left', row.scrollLeft > 1);
  nav.classList.toggle('more-right', row.scrollLeft + row.clientWidth < row.scrollWidth - 1);
}

function showActiveTab() {
  const here = document.querySelector('#tabs a.active');
  if (here) $('#tabs').scrollLeft = here.offsetLeft - ($('#tabs').clientWidth - here.offsetWidth) / 2;
  tabEdges();
}

function route() {
  const shown = visibleTabs().map(([id]) => id);
  const t = location.hash.slice(1);
  state.tab = shown.includes(t) ? t : shown.includes(state.settings.tab) ? state.settings.tab : 'overview';
  render();
  showActiveTab();
}

function showLogin() {
  clearInterval(state.timer);
  $('#app').hidden = true;
  $('#login').hidden = false;
  $('#password').focus();
}

async function startApp() {
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#ro-badge').hidden = !state.session.readonly;
  $('#logout').hidden = !state.session.authRequired;
  const parts = ['btrfs-ui ' + state.session.version];
  if (state.session.btrfsProgs) parts.push('btrfs-progs ' + state.session.btrfsProgs);
  $('#footer').textContent = parts.join(' · ');

  $('#tabs').replaceChildren(...visibleTabs().map(([id, label]) =>
    h('a', { href: '#' + id, 'data-tab': id }, label)));
  $('#tabs').addEventListener('scroll', tabEdges, { passive: true });
  window.addEventListener('resize', tabEdges);

  state.filesystems = await api('GET', '/api/filesystems');
  const fsSel = $('#fs');
  fsSel.replaceChildren(...state.filesystems.map((f) =>
    h('option', { value: f.id }, `${f.label || f.uuid.slice(0, 8)}  (${f.primary})`)));
  let saved = null;
  try { saved = localStorage.getItem('fsId'); } catch (e) { /* storage unavailable */ }
  const pick = state.filesystems.find((f) => f.id === saved) || state.filesystems[0];
  state.fsId = pick ? pick.id : null;
  if (pick) fsSel.value = pick.id;
  route();
}

async function boot() {
  state.session = await api('GET', '/api/session');
  if (state.session.authenticated) await startApp(); else showLogin();
}

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#login-error');
  err.hidden = true;
  try {
    await api('POST', '/api/login', { password: $('#password').value, remember: $('#remember').checked });
    $('#password').value = '';
    await boot();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

$('#logout').addEventListener('click', async () => {
  await api('POST', '/api/logout', {});
  location.hash = '';
  await boot();
});

$('#fs').addEventListener('change', (e) => {
  state.fsId = e.target.value;
  try { localStorage.setItem('fsId', state.fsId); } catch (err) { /* ignore */ }
  render();
});
window.addEventListener('hashchange', route);

boot().catch((e) => { document.body.textContent = 'Failed to load: ' + e.message; });
