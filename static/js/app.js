/* Twos 主逻辑：今日清单（未完成自动顺延）、日历、清单、星标、搜索、提醒 */
const $ = s => document.querySelector(s);

const state = {
  view: 'day',        // day | calendar | lists | files | starred | search | list
  date: null,         // 当前查看的日期
  listId: null,       // 当前查看的清单
  query: '',
  fileQuery: '',     // 文件库筛选
  lists: [],
  today: null,
  items: [],          // 当前视图的事项
  editingId: null,    // 正在编辑的事项 id（null = 新建）
  editor: null,
};

/* ---------------- 工具 ---------------- */
const esc = s => String(s || '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts = {}) {
  const cfg = { headers: {}, ...opts };
  if (cfg.body && !(cfg.body instanceof FormData)) {
    cfg.headers['Content-Type'] = 'application/json';
    cfg.body = JSON.stringify(cfg.body);
  }
  const res = await fetch(path, cfg);
  if (res.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}

function fmtRemind(v) {
  if (!v) return '';
  const d = v.replace('T', ' ').slice(5, 16);
  return d;
}

function fmtSize(n) {
  if (n > 1048576) return (n / 1048576).toFixed(1) + 'MB';
  if (n > 1024) return (n / 1024).toFixed(0) + 'KB';
  return n + 'B';
}

function toast(title, body) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<div class="t-title">${esc(title)}</div>` +
    (body ? `<div class="t-body">${esc(body)}</div>` : '');
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function addDays(dateStr, n) {
  const d = new Date(dateStr + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}

function dayTitle(date) {
  const t = state.today;
  if (date === t) return `今天 · ${date.slice(5).replace('-', '月')}日`;
  if (date === addDays(t, 1)) return `明天 · ${date.slice(5).replace('-', '月')}日`;
  if (date === addDays(t, -1)) return `昨天 · ${date.slice(5).replace('-', '月')}日`;
  return `${date.slice(0, 4)}年${date.slice(5, 7)}月${date.slice(8)}日`;
}

/* ---------------- 视图切换 ---------------- */
function setView(view, param) {
  state.view = view;
  if (view === 'day') state.date = param || state.today;
  if (view === 'list') state.listId = param;
  renderNav();
  const v = $('#view');
  v.innerHTML = '';
  if (view === 'day') renderDay(state.date);
  else if (view === 'calendar') renderCalendar();
  else if (view === 'lists') renderLists();
  else if (view === 'files') renderFiles();
  else if (view === 'starred') renderStarred();
  else if (view === 'search') renderSearch();
  else if (view === 'list') renderListItems(state.listId);
}

function renderNav() {
  document.querySelectorAll('.nav-item').forEach(a =>
    a.classList.toggle('active', a.dataset.view === state.view));
  document.querySelectorAll('.list-nav-item').forEach(a =>
    a.classList.toggle('active', state.view === 'list' && +a.dataset.id === state.listId));
}

/* ---------------- 今日 / 某日清单 ---------------- */
async function renderDay(date) {
  $('#viewTitle').textContent = dayTitle(date);
  $('#viewActions').innerHTML = date === state.today
    ? `<button class="ghost" id="btnPrevDay">← 前一天</button>
       <button class="ghost" id="btnNextDay">后一天 →</button>
       <button class="ghost" id="btnClearDone">清除已完成</button>`
    : `<button class="ghost" id="btnPrevDay">← 前一天</button>
       <button class="ghost" id="btnNextDay">后一天 →</button>
       <button class="primary" id="btnGoToday">回到今天</button>
       <button class="ghost" id="btnClearDone">清除已完成</button>`;
  const data = await api(`/api/day/${date}`);
  state.items = data.items;
  const v = $('#view');
  v.innerHTML = `
    <div class="quick-add">
      <input type="text" id="quickInput" placeholder="记点什么…（回车添加，点击事项可富文本编辑）" autofocus>
    </div>
    <ul class="items" id="itemList"></ul>`;
  renderItems();
  $('#quickInput').addEventListener('keydown', async e => {
    if (e.key === 'Enter' && e.target.value.trim()) {
      const text = e.target.value.trim();
      e.target.value = '';
      await api('/api/items', { method: 'POST', body: { date, content_html: esc(text) } });
      renderDay(date);
    }
  });
  $('#btnPrevDay').onclick = () => setView('day', addDays(date, -1));
  $('#btnNextDay').onclick = () => setView('day', addDays(date, 1));
  $('#btnClearDone').onclick = async () => {
    for (const it of state.items.filter(i => i.done)) {
      await api(`/api/items/${it.id}`, { method: 'DELETE' });
    }
    renderDay(date);
  };
  const goToday = $('#btnGoToday');
  if (goToday) goToday.onclick = () => setView('day', state.today);
  $('#quickInput').focus();
}

/* ---------------- 渲染事项列表 ---------------- */
function renderItems() {
  const ul = $('#itemList');
  if (!state.items.length) {
    ul.innerHTML = `<div class="empty">这里还没有内容<br>写点什么，把事情从脑子里清空 ✌</div>`;
    return;
  }
  ul.innerHTML = state.items.map(it => `
    <li class="item ${it.done && it.is_todo ? 'done' : ''}" data-id="${it.id}">
      ${it.is_todo
        ? `<button class="check" title="完成/取消完成">✓</button>`
        : `<button class="check note" title="转为待办">📝</button>`}
      <div class="body">
        <div class="text">${esc((it.content_text || '').split('\n')[0].slice(0, 120) || '（空）')}</div>
        <div class="meta">
          ${it.remind_at ? `<span class="badge remind">⏰ ${esc(fmtRemind(it.remind_at))}</span>` : ''}
          ${it.carried_from && it.date === state.today ? `<span class="badge carried">从 ${esc(it.carried_from)} 顺延</span>` : ''}
          ${it.list_id ? `<span class="badge">${esc(listName(it.list_id))}</span>` : ''}
          ${it.date && state.view === 'list' ? `<span class="badge">${esc(it.date)}</span>` : ''}
          ${it.attachments.length ? `<span class="badge atts">📎 ${it.attachments.length} 个附件</span>` : ''}
          ${it.attachments.filter(a => a.match).map(a =>
            `<span class="badge atts">📄 命中附件：${esc(a.filename)}</span>`).join('')}
        </div>
        ${it.attachments.filter(a => a.match && a.snippet).slice(0, 2).map(a =>
          `<div class="snippet">${esc(a.snippet)}</div>`).join('')}
        ${it.attachments.filter(a => a.is_image).length ? `
          <div class="thumbs">${it.attachments.filter(a => a.is_image).slice(0, 4).map(a =>
            `<img src="${esc(attHref(a))}" data-url="${esc(attHref(a))}" alt="${esc(a.filename)}">`).join('')}</div>` : ''}
        ${it.attachments.filter(a => !a.is_image).length ? `
          <div class="atts-row">${it.attachments.filter(a => !a.is_image).map(a =>
            `<a class="att-chip" href="${esc(attHref(a))}" target="_blank">📎 ${esc(a.filename)}</a>`).join('')}</div>` : ''}
      </div>
      <button class="star ${it.starred ? 'on' : ''}" title="星标">★</button>
      <div class="order">
        <button class="up" title="上移">▲</button>
        <button class="down" title="下移">▼</button>
      </div>
    </li>`).join('');

  ul.querySelectorAll('.item').forEach(li => {
    const id = +li.dataset.id;
    const item = state.items.find(i => i.id === id);
    li.querySelector('.check').onclick = async () => {
      if (item.is_todo) {
        await api(`/api/items/${id}`, { method: 'PATCH', body: { done: !item.done } });
      } else {
        // 笔记 -> 待办
        await api(`/api/items/${id}`, { method: 'PATCH', body: { is_todo: true } });
        toast('已转为待办');
      }
      refreshCurrent();
    };
    li.querySelector('.star').onclick = async () => {
      await api(`/api/items/${id}`, { method: 'PATCH', body: { starred: !item.starred } });
      refreshCurrent();
    };
    li.querySelector('.body').onclick = e => {
      if (e.target.closest('img')) { window.open(e.target.dataset.url || e.target.src); return; }
      openEditor(item);
    };
    li.querySelector('.up').onclick = () => reorderItem(id, -1);
    li.querySelector('.down').onclick = () => reorderItem(id, 1);
    // 附件卡片：Office 文件调起本地软件打开（可编辑保存回服务器），其他浏览器打开
    li.querySelectorAll('a.att-chip').forEach(a => {
      a.onclick = e => { e.preventDefault(); openAttHref(a.getAttribute('href')); };
    });
  });
}

function listName(listId) {
  const l = state.lists.find(x => x.id === listId);
  return (l ? (l.emoji ? l.emoji + ' ' : '') + l.name : '清单');
}

async function reorderItem(id, dir) {
  const idx = state.items.findIndex(i => i.id === id);
  const j = idx + dir;
  if (j < 0 || j >= state.items.length) return;
  const arr = [...state.items];
  [arr[idx], arr[j]] = [arr[j], arr[idx]];
  await api('/api/items/reorder', { method: 'POST', body: { ids: arr.map(i => i.id) } });
  state.items = arr;
  renderItems();
}

async function refreshCurrent() {
  if (state.view === 'day') await renderDay(state.date);
  else if (state.view === 'list') await renderListItems(state.listId);
  else if (state.view === 'starred') await renderStarred();
  else if (state.view === 'search') await doSearch();
}

/* ---------------- 日历 ---------------- */
async function renderCalendar() {
  let month = state.today.slice(0, 7);
  await drawCalendar(month);
}

async function drawCalendar(month) {
  $('#viewTitle').textContent = `${month.replace('-', '年')}月`;
  $('#viewActions').innerHTML = `
    <button class="ghost" id="calPrev">← 上月</button>
    <button class="ghost" id="calNext">下月 →</button>
    <button class="primary" id="calToday">今天</button>`;
  const data = await api(`/api/calendar?month=${month}`);
  const counts = {};
  data.days.forEach(d => counts[d.date] = d);

  const [y, m] = month.split('-').map(Number);
  const first = new Date(y, m - 1, 1);
  const startDow = first.getDay();
  const daysInMonth = new Date(y, m, 0).getDate();
  const prevDays = new Date(y, m - 1, 0).getDate();
  let cells = '';
  for (let i = 0; i < 42; i++) {
    let d, cls = 'cal-cell', label;
    if (i < startDow) {
      d = `${y}-${String(m - 1).padStart(2, '0')}-${String(prevDays - startDow + 1 + i).padStart(2, '0')}`;
      cls += ' other';
    } else if (i < startDow + daysInMonth) {
      const day = i - startDow + 1;
      d = `${y}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    } else {
      const day = i - startDow - daysInMonth + 1;
      d = `${y}-${String(m + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      cls += ' other';
    }
    if (d === state.today) cls += ' today';
    const c = counts[d];
    const dots = c ? Array.from({ length: Math.min(c.total, 8) }, (_, k) =>
      `<span class="dot ${k < c.done ? 'done' : ''}"></span>`).join('') : '';
    cells += `<div class="${cls}" data-date="${d}">
      <div class="d">${+d.slice(8)}</div><div class="dots">${dots}</div></div>`;
  }
  $('#view').innerHTML = `
    <div class="cal-grid">
      ${['日', '一', '二', '三', '四', '五', '六'].map(d => `<div class="cal-dow">周${d}</div>`).join('')}
      ${cells}
    </div>`;
  $('#calPrev').onclick = () => {
    const [y2, m2] = month.split('-').map(Number);
    const dt = new Date(y2, m2 - 2, 1);
    drawCalendar(`${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`);
  };
  $('#calNext').onclick = () => {
    const [y2, m2] = month.split('-').map(Number);
    const dt = new Date(y2, m2, 1);
    drawCalendar(`${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`);
  };
  $('#calToday').onclick = () => renderCalendar();
  document.querySelectorAll('.cal-cell').forEach(c =>
    c.onclick = () => setView('day', c.dataset.date));
}

/* ---------------- 我的清单 ---------------- */
async function renderLists() {
  $('#viewTitle').textContent = '我的清单';
  $('#viewActions').innerHTML = '';
  await refreshLists();
  $('#view').innerHTML = `
    <div class="new-list">
      <input type="text" id="newListName" placeholder="新建清单名称…">
      <input type="text" id="newListEmoji" placeholder="图标(可选)" style="width:110px">
      <button class="primary" id="btnNewList">创建</button>
    </div>
    <div id="listRows"></div>`;
  drawListRows();
  $('#btnNewList').onclick = async () => {
    const name = $('#newListName').value.trim();
    if (!name) return;
    await api('/api/lists', { method: 'POST', body: { name, emoji: $('#newListEmoji').value.trim() } });
    renderLists();
  };
  $('#newListName').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btnNewList').click(); });
}

function drawListRows() {
  $('#listRows').innerHTML = state.lists.map(l => `
    <div class="list-row" data-id="${l.id}">
      <span>${esc((l.emoji ? l.emoji + ' ' : '') + l.name)}</span>
      <span class="cnt" style="color:var(--muted);font-size:13px">${l.count} 项</span>
      <button class="link-btn" data-act="rename" data-id="${l.id}">重命名</button>
      <button class="link-btn" data-act="del" data-id="${l.id}" style="color:var(--danger)">删除</button>
    </div>`).join('') || '<div class="empty">还没有自定义清单</div>';
  document.querySelectorAll('.list-row').forEach(r => {
    r.onclick = e => {
      if (e.target.dataset.act) return;
      setView('list', +r.dataset.id);
    };
  });
  document.querySelectorAll('[data-act]').forEach(b => {
    b.onclick = async e => {
      e.stopPropagation();
      const id = +b.dataset.id;
      if (b.dataset.act === 'rename') {
        const name = prompt('新的清单名称：', state.lists.find(l => l.id === id).name);
        if (name && name.trim()) {
          await api(`/api/lists/${id}`, { method: 'PATCH', body: { name: name.trim() } });
          renderLists();
        }
      } else {
        if (confirm('删除该清单及其所有事项（含附件）？')) {
          await api(`/api/lists/${id}`, { method: 'DELETE' });
          renderLists();
        }
      }
    };
  });
}

async function renderListItems(listId) {
  const l = state.lists.find(x => x.id === listId);
  $('#viewTitle').textContent = (l ? (l.emoji ? l.emoji + ' ' : '') + l.name : '清单');
  $('#viewActions').innerHTML = `<button class="ghost" id="btnClearDone">清除已完成</button>`;
  const data = await api(`/api/lists/${listId}/items`);
  state.items = data.items;
  $('#view').innerHTML = `
    <div class="quick-add">
      <input type="text" id="quickInput" placeholder="在此清单添加事项…（回车）" autofocus>
    </div>
    <ul class="items" id="itemList"></ul>`;
  renderItems();
  $('#quickInput').addEventListener('keydown', async e => {
    if (e.key === 'Enter' && e.target.value.trim()) {
      const text = e.target.value.trim();
      e.target.value = '';
      await api('/api/items', { method: 'POST', body: { list_id: listId, content_html: esc(text) } });
      renderListItems(listId);
    }
  });
  $('#btnClearDone').onclick = async () => {
    for (const it of state.items.filter(i => i.done)) {
      await api(`/api/items/${it.id}`, { method: 'DELETE' });
    }
    renderListItems(listId);
  };
  $('#quickInput').focus();
}

/* ---------------- 文件库 ---------------- */
function fileIcon(name) {
  const e = (String(name || '').split('.').pop() || '').toLowerCase();
  if (e === 'pdf') return '📕';
  if (['doc', 'docx', 'rtf', 'odt', 'txt', 'md'].includes(e)) return '📘';
  if (['xls', 'xlsx', 'csv'].includes(e)) return '📗';
  if (['ppt', 'pptx'].includes(e)) return '📙';
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(e)) return '🗜️';
  if (['mp3', 'wav', 'flac', 'm4a'].includes(e)) return '🎵';
  if (['mp4', 'mkv', 'mov', 'avi', 'webm'].includes(e)) return '🎬';
  return '📄';
}

async function renderFiles() {
  $('#viewTitle').textContent = '文件库';
  $('#viewActions').innerHTML = '';
  const q = state.fileQuery || '';
  const data = await api('/api/files' + (q ? `?q=${encodeURIComponent(q)}` : ''));
  $('#view').innerHTML = `
    <div class="search-bar">
      <input type="text" id="fileSearch" placeholder="按文件名筛选…" value="${esc(q)}">
      <button class="primary" id="btnFileSearch">筛选</button>
    </div>
    <div class="files-grid" id="filesGrid"></div>`;
  drawFiles(data.files);
  const doFilter = () => { state.fileQuery = $('#fileSearch').value.trim(); renderFiles(); };
  $('#btnFileSearch').onclick = doFilter;
  $('#fileSearch').addEventListener('keydown', e => { if (e.key === 'Enter') doFilter(); });
}

function drawFiles(files) {
  const grid = $('#filesGrid');
  if (!files.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1">文件库是空的<br>在事项编辑框里上传的图片和附件都会出现在这里</div>';
    return;
  }
  grid.innerHTML = files.map(f => `
    <div class="file-card" data-id="${f.id}">
      <div class="fc-thumb" title="打开">${f.is_image
        ? `<img src="${esc(attHref(f))}" loading="lazy">` : `<span>${fileIcon(f.filename)}</span>`}</div>
      <div class="fc-name" title="${esc(f.filename)}">${esc(f.filename)}</div>
      <div class="fc-meta">${fmtSize(f.size)} · ${(f.created_at || '').slice(0, 10)}</div>
      ${f.item_id ? `<button class="fc-item" data-item="${f.item_id}" title="查看所属事项">
        🗒 ${(esc(f.item_preview) || '（空）')}</button>` : '<div class="fc-item none">未关联事项</div>'}
    </div>`).join('');
  grid.querySelectorAll('.file-card').forEach(card => {
    const f = files.find(x => x.id === +card.dataset.id);
    card.querySelector('.fc-thumb').onclick = () => openAttHref(attHref(f));
    card.querySelector('.fc-name').onclick = () => openAttHref(attHref(f));
    const btn = card.querySelector('.fc-item[data-item]');
    if (btn) btn.onclick = async () => {
      const item = await api(`/api/items/${btn.dataset.item}`);
      openEditor(item);
    };
  });
}

/* ---------------- 星标 / 搜索 ---------------- */
async function renderStarred() {
  $('#viewTitle').textContent = '星标事项';
  $('#viewActions').innerHTML = '';
  const data = await api('/api/starred');
  state.items = data.items;
  $('#view').innerHTML = `<ul class="items" id="itemList"></ul>`;
  renderItems();
}

function renderSearch() {
  $('#viewTitle').textContent = '搜索';
  $('#viewActions').innerHTML = '';
  $('#view').innerHTML = `
    <div class="search-bar">
      <input type="text" id="searchInput" placeholder="搜索所有事项和附件内容…" autofocus>
      <button class="primary" id="btnSearch">搜索</button>
    </div>
    <ul class="items" id="itemList"></ul>`;
  $('#searchInput').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
  $('#btnSearch').onclick = doSearch;
  $('#searchInput').focus();
}

async function doSearch() {
  state.query = $('#searchInput').value.trim();
  const data = state.query ? await api(`/api/search?q=${encodeURIComponent(state.query)}`) : { items: [] };
  state.items = data.items;
  renderItems();
  if (state.query && !state.items.length) {
    $('#itemList').innerHTML = '<div class="empty">没有找到相关内容</div>';
  }
}

/* ---------------- 事项编辑弹窗（富文本） ---------------- */
function openEditor(item) {
  state.editingId = item ? item.id : null;
  $('#editorTitle').textContent = item ? '编辑事项' : '新建事项';
  const editor = createRichEditor($('#editorMount'), { itemId: state.editingId });
  state.editor = editor;
  if (item) {
    editor.setHTML(item.content_html);
    $('#remindInput').value = item.remind_at ? item.remind_at.slice(0, 16) : '';
    $('#isTodoCheck').checked = !!item.is_todo;
  } else {
    editor.setHTML('');
    $('#remindInput').value = '';
    $('#isTodoCheck').checked = false;
  }
  // 移动目标
  const moveSel = $('#moveTarget');
  const t = state.today;
  let opts = `<option value="">（不移动）</option>
    <option value="day:${t}">今天</option>
    <option value="day:${addDays(t, 1)}">明天</option>
    <option value="day:${addDays(t, 2)}">后天</option>
    <option value="day:pick">指定日期…</option>`;
  if (state.lists.length) {
    opts += `<optgroup label="清单">` + state.lists.map(l =>
      `<option value="list:${l.id}">${esc((l.emoji ? l.emoji + ' ' : '') + l.name)}</option>`).join('') + `</optgroup>`;
  }
  moveSel.innerHTML = opts;
  moveSel.value = '';
  $('#moveDate').classList.add('hidden');
  moveSel.onchange = () => $('#moveDate').classList.toggle('hidden', moveSel.value !== 'day:pick');

  $('#btnDeleteItem').classList.toggle('hidden', !item);
  $('#editorModal').classList.remove('hidden');
  editor.focus();
}

function closeEditor() {
  $('#editorModal').classList.add('hidden');
  $('#editorMount').innerHTML = '';
  state.editor = null;
}

async function saveEditor() {
  const editor = state.editor;
  const html = editor.getHTML();
  if (!html.trim() || !html.replace(/<br\s*\/?>|&nbsp;/g, '').trim()) {
    toast('内容为空'); return;
  }
  const attIds = collectAttIds(html);
  const body = {
    content_html: html,
    remind_at: $('#remindInput').value || null,
    attachment_ids: attIds,
    is_todo: $('#isTodoCheck').checked,
  };
  const moveVal = $('#moveTarget').value;
  if (moveVal === 'day:pick') {
    const d = $('#moveDate').value;
    if (d) body.date = d;
  } else if (moveVal.startsWith('day:')) {
    body.date = moveVal.slice(4);
  } else if (moveVal.startsWith('list:')) {
    body.list_id = +moveVal.slice(5);
  }
  if (state.editingId) {
    await api(`/api/items/${state.editingId}`, { method: 'PATCH', body });
  } else {
    if (!body.date && !body.list_id) {
      if (state.view === 'list') body.list_id = state.listId;
      else body.date = state.view === 'day' ? state.date : state.today;
    }
    await api('/api/items', { method: 'POST', body });
  }
  closeEditor();
  refreshCurrent();
}

/* ---------------- 提醒轮询 ---------------- */
async function checkReminders() {
  try {
    const data = await api('/api/reminders/due');
    for (const it of data.items) {
      const text = (it.content_text || '').split('\n')[0].slice(0, 60);
      toast('⏰ 提醒：' + (text || '事项'), it.remind_at?.replace('T', ' '));
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification('⏰ Twos 提醒', { body: text });
      }
    }
  } catch (e) { /* 忽略轮询错误 */ }
}

/* ---------------- 侧边栏清单 ---------------- */
async function refreshLists() {
  state.lists = await api('/api/lists');
  const nav = $('#listsNav');
  nav.innerHTML = `<h4>我的清单</h4>` + state.lists.map(l => `
    <div class="list-nav-item" data-id="${l.id}">
      <span>${esc((l.emoji ? l.emoji + ' ' : '') + l.name)}</span>
      <span class="cnt">${l.count}</span>
    </div>`).join('');
  nav.querySelectorAll('.list-nav-item').forEach(el =>
    el.onclick = () => setView('list', +el.dataset.id));
}

/* ---------------- 初始化 ---------------- */
async function init() {
  const boot = await api('/api/bootstrap');
  state.today = boot.today;
  window.ATT_TOKEN = boot.att_token || '';
  const [y, m, d] = boot.today.split('-');
  $('#todayLabel').textContent = `今天是 ${y}年${+m}月${+d}日（北京时间）`;
  await refreshLists();

  document.querySelectorAll('.nav-item').forEach(a =>
    a.onclick = () => setView(a.dataset.view));
  $('#logoutBtn').classList.remove('hidden');
  $('#logoutBtn').onclick = async () => {
    await api('/auth/logout', { method: 'POST' });
    location.href = '/login';
  };
  $('#editorClose').onclick = closeEditor;
  $('#textAttachmentClose').onclick = closeTextAttachment;
  $('#textAttachmentSave').onclick = saveTextAttachment;
  $('#editorModal').addEventListener('mousedown', e => {
    if (e.target === e.currentTarget) closeEditor();
  });
  $('#textAttachmentModal').addEventListener('mousedown', e => {
    if (e.target === e.currentTarget) closeTextAttachment();
  });
  $('#btnSaveItem').onclick = saveEditor;
  $('#btnDeleteItem').onclick = async () => {
    if (state.editingId && confirm('删除该事项及其附件？')) {
      await api(`/api/items/${state.editingId}`, { method: 'DELETE' });
      closeEditor();
      refreshCurrent();
    }
  };
  $('#clearRemind').onclick = () => { $('#remindInput').value = ''; };
  document.addEventListener('keydown', e => {
    if (!$('#textAttachmentModal').classList.contains('hidden') &&
        (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveTextAttachment();
      return;
    }
    if (e.key === 'Escape' && !$('#textAttachmentModal').classList.contains('hidden')) closeTextAttachment();
    if (e.key === 'Escape' && !$('#editorModal').classList.contains('hidden')) closeEditor();
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !$('#editorModal').classList.contains('hidden')) saveEditor();
  });
  // 首次点击时申请通知权限
  document.addEventListener('click', function ask() {
    if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
    document.removeEventListener('click', ask);
  });

  setView('day', state.today);
  setInterval(checkReminders, 20000);
  checkReminders();
}

init();
