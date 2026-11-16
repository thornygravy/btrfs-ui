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

function fmtBytes(n) {
  if (n == null) return '–';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)) + ' ' + units[i];
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : '';
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

const state = { session: null, filesystems: [], fsId: null, tab: 'overview', timer: null };

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
      form.append(h('div', { class: 'field' },
        label, f.type === 'checkbox' ? null : input,
        f.hint ? h('span', { class: 'hint' }, f.hint) : null));
    }
    form.append(h('div', { class: 'buttons' },
      h('button', { class: danger ? 'danger' : 'primary', value: 'ok' }, submit),
      h('button', { value: 'cancel', formnovalidate: true }, 'Cancel')));
    dlg.addEventListener('close', () => {
      let result = null;
      if (dlg.returnValue === 'ok') {
        result = {};
        for (const f of fields) {
          const el = form.elements[f.name];
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
  ['maintenance', 'Maintenance'],
];

function panel(...kids) {
  return h('section', { class: 'panel' }, ...kids);
}

function section(title, ...kids) {
  return panel(h('h2', null, title), ...kids);
}

const SCRUB_HINT = 'No scrub history found. If scrubs have run on the host, share its ' +
  '/var/lib/btrfs directory with this container (see the README).';

function kv(pairs) {
  return h('dl', { class: 'kv' },
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
      h('table', null,
        h('thead', null, h('tr', null,
          h('th', null, 'Type'), h('th', null, 'Profile'),
          h('th', { class: 'num' }, 'Allocated'), h('th', { class: 'num' }, 'Used'), h('th', null, ''))),
        h('tbody', null, d.allocation.map((a) => h('tr', null,
          h('td', null, a.type), h('td', null, a.profile),
          h('td', { class: 'num' }, fmtBytes(a.total)), h('td', { class: 'num' }, fmtBytes(a.used)),
          h('td', null, a.total ? bar((a.used / a.total) * 100) : null)))))),

    section('Devices',
      h('table', null,
        h('thead', null, h('tr', null,
          h('th', null, 'ID'), h('th', null, 'Device'),
          h('th', { class: 'num' }, 'Size'), h('th', { class: 'num' }, 'Allocated'), h('th', null, 'Errors'))),
        h('tbody', null, d.devices.map((dev) => h('tr', null,
          h('td', null, dev.devid),
          h('td', { class: 'mono' }, dev.path, dev.missing ? [' ', badge('missing', 'err')] : null),
          h('td', { class: 'num' }, fmtBytes(dev.size)), h('td', { class: 'num' }, fmtBytes(dev.used)),
          h('td', null, errorCell(dev))))))),

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
      tbody.append(h('tr', null, h('td', { colspan: 6, class: 'muted' }, 'No subvolumes match.')));
    }
  }

  function subvolRow(s) {
    const flags = [];
    if (s.readonly) flags.push(badge('read-only'));
    if (s.default) flags.push(badge('default', 'accent'));
    if (s.mounted) flags.push(badge('mounted', 'ok'));
    return h('tr', null,
      h('td', { class: 'mono' }, s.path),
      h('td', null, s.id),
      h('td', null, s.snapshot ? 'snapshot' : 'subvolume',
        s.created ? h('div', { class: 'muted' }, s.created) : null),
      h('td', { class: 'num' }, s.referenced != null
        ? h('span', null, fmtBytes(s.referenced), h('div', { class: 'muted' }, fmtBytes(s.exclusive) + ' excl.')) : ''),
      h('td', null, flags),
      h('td', { class: 'actions' },
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
      h('button', { class: 'primary', onclick: createDialog }, 'New subvolume')),
    d.top_level_mounted ? null : h('div', { class: 'panel warn' },
      'The filesystem top level (subvolid=5) is not mounted in this container, so some subvolumes are not reachable.'),
    panel(
      h('table', null,
        h('thead', null, h('tr', null,
          h('th', null, 'Path'), h('th', null, 'ID'), h('th', null, 'Type'),
          h('th', { class: 'num' }, d.quotas ? 'Size' : ''), h('th', null, ''), h('th', null, ''))),
        tbody),
      d.quotas ? null : h('p', { class: 'muted' },
        'Sizes appear here when quotas are enabled (btrfs quota enable).')));
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
        h('td', null, box), h('td', null, s.number),
        h('td', null, s.type + (s.pre_number ? ` (of ${s.pre_number})` : '')),
        h('td', null, s.date), h('td', null, s.user),
        h('td', { class: 'muted' }, s.cleanup),
        h('td', null, s.description, s.userdata ? h('div', { class: 'muted' }, s.userdata) : null));
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
        ? h('table', null,
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

/* Maintenance */

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
  const defragPath = h('input', { type: 'text', class: 'grow', placeholder: '@data/photos', size: 28 });
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
    section('Scrub',
      scrubStatus,
      h('div', { class: 'row' }, scrubStart, scrubResume, scrubCancel,
        h('label', { class: 'check' }, scrubRO, 'Check only, do not repair'))),
    section('Balance',
      balStatus,
      h('div', { class: 'row' },
        h('label', { class: 'check' }, 'Data chunks under', dUsage, '% full'),
        h('label', { class: 'check' }, 'Metadata chunks under', mUsage, '% full')),
      h('div', { class: 'row' }, balStart, balCancel)),
    section('Defragment',
      h('div', { class: 'row first' }, defragPath, defragComp,
        h('label', { class: 'check' }, 'Target extent size', defragSize), defragBtn)),
    section('Trim',
      h('p', null, 'Discard unused blocks on SSDs.'), h('div', { class: 'row' }, trimBtn)),
    section('Jobs', jobsBox));

  function drawScrub(s) {
    // the status word is already shown as a badge, so drop it from the detail rows
    const rows = s.pairs.filter(([k]) => k !== 'Status');
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
    jobsBox.replaceChildren(h('table', null, h('tbody', null, list.map((j) => {
      const det = h('details', { 'data-id': j.id },
        h('summary', null, 'output'),
        h('pre', null, j.output.join('\n') || '(no output)'));
      if (open.has(j.id)) det.open = true;
      return h('tr', null,
        h('td', null, j.title, h('div', { class: 'muted' }, fmtTime(j.started)), det),
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

/* ---------- shell ---------- */

const VIEWS = { overview: viewOverview, subvolumes: viewSubvolumes, snapper: viewSnapper, maintenance: viewMaintenance };

async function render() {
  clearInterval(state.timer);
  const root = $('#view');
  $('#fs').hidden = state.tab === 'snapper' || !state.filesystems.length;
  document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.tab === state.tab));
  const needsFs = state.tab !== 'snapper';
  const holder = h('div');
  if (needsFs && !state.fsId) { root.replaceChildren(noFilesystems()); return; }
  try {
    await VIEWS[state.tab](holder);
    root.replaceChildren(...holder.childNodes);
  } catch (e) {
    if (e.message !== 'Sign in required') root.replaceChildren(panel(h('p', { class: 'err' }, e.message)));
  }
}

function route() {
  const t = location.hash.slice(1);
  state.tab = VIEWS[t] ? t : 'overview';
  render();
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

  $('#tabs').replaceChildren(...TABS.map(([id, label]) =>
    h('a', { href: '#' + id, 'data-tab': id }, label)));

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
    await api('POST', '/api/login', { password: $('#password').value });
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
