/* 富文本编辑器：工具栏 + contenteditable + 一次性多文件上传（图片/附件） */
function createRichEditor(mount, options = {}) {
  mount.innerHTML = `
    <div class="re-toolbar">
      <button type="button" data-cmd="bold" title="加粗"><b>B</b></button>
      <button type="button" data-cmd="italic" title="斜体"><i>I</i></button>
      <button type="button" data-cmd="underline" title="下划线"><u>U</u></button>
      <button type="button" data-cmd="strikeThrough" title="删除线"><s>S</s></button>
      <span class="sep"></span>
      <button type="button" data-cmd="formatBlock" data-value="h3" title="标题">H</button>
      <button type="button" data-cmd="insertUnorderedList" title="无序列表">•&nbsp;列表</button>
      <button type="button" data-cmd="insertOrderedList" title="有序列表">1.&nbsp;列表</button>
      <button type="button" data-cmd="formatBlock" data-value="blockquote" title="引用">❝ 引用</button>
      <span class="sep"></span>
      <button type="button" data-action="link" title="插入链接">🔗</button>
      <button type="button" data-action="upload" title="上传图片/附件（可一次多选）">📎 图片/附件</button>
      <button type="button" data-cmd="removeFormat" title="清除格式">⌫</button>
    </div>
    <div class="re-content" contenteditable="true" spellcheck="false"></div>
    <input type="file" multiple hidden>
    <div class="re-status"></div>`;

  const toolbar = mount.querySelector('.re-toolbar');
  const content = mount.querySelector('.re-content');
  const fileInput = mount.querySelector('input[type=file]');
  const status = mount.querySelector('.re-status');

  // 点击工具栏不丢失光标
  toolbar.addEventListener('mousedown', e => e.preventDefault());
  toolbar.addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    content.focus();
    if (btn.dataset.cmd) {
      document.execCommand(btn.dataset.cmd, false, btn.dataset.value || null);
    } else if (btn.dataset.action === 'link') {
      const url = prompt('链接地址：', 'https://');
      if (url) document.execCommand('createLink', false, url);
    } else if (btn.dataset.action === 'upload') {
      fileInput.click();
    }
  });

  fileInput.addEventListener('change', async () => {
    if (fileInput.files.length) await uploadAndInsert(fileInput.files);
    fileInput.value = '';
  });

  // 粘贴图片直接上传
  content.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
    if (files.length) {
      e.preventDefault();
      uploadAndInsert(files);
    }
  });

  // 编辑框内：附件卡片单击打开；图片双击打开（单击用于选中编辑）
  content.addEventListener('click', e => {
    const a = e.target.closest('a.att-chip');
    if (a) {
      e.preventDefault();
      window.open(a.href, '_blank');
    }
  });
  content.addEventListener('dblclick', e => {
    if (e.target.tagName === 'IMG') window.open(e.target.src, '_blank');
  });

  function setStatus(t) { status.textContent = t || ''; }

  function insertHTML(html) {
    content.focus();
    document.execCommand('insertHTML', false, html);
  }

  function esc(s) {
    return String(s || '').replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function fmtSize(n) {
    if (n > 1048576) return (n / 1048576).toFixed(1) + 'MB';
    if (n > 1024) return (n / 1024).toFixed(0) + 'KB';
    return n + 'B';
  }

  /* 一次性上传多个文件：图片插入为 <img>，其他插入为附件卡片 */
  async function uploadAndInsert(fileList) {
    const files = [...fileList];
    setStatus(`正在上传 ${files.length} 个文件…`);
    try {
      const fd = new FormData();
      files.forEach(f => fd.append('files', f));
      if (options.itemId) fd.append('item_id', options.itemId);
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || '上传失败');
      for (const att of data.attachments) {
        if (att.is_image) {
          insertHTML(`<img src="${esc(att.url)}" alt="${esc(att.filename)}" data-att-id="${att.id}">`);
        } else {
          insertHTML(
            ` <a class="att-chip" href="${esc(att.url)}" target="_blank" ` +
            `data-att-id="${att.id}">📎 ${esc(att.filename)} (${fmtSize(att.size)})</a> `);
        }
      }
      setStatus(`已上传 ${data.attachments.length} 个文件`);
      options.onUpload && options.onUpload(data.attachments);
    } catch (err) {
      setStatus('上传失败：' + err.message);
    }
  }

  return {
    getHTML: () => content.innerHTML,
    setHTML: html => { content.innerHTML = html || ''; },
    focus: () => content.focus(),
    insertHTML,
    uploadAndInsert,
    element: content
  };
}

/* 从编辑器 HTML 中提取当前保留的附件 id（被删除的会从服务器/OSS 清理） */
function collectAttIds(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return [...doc.querySelectorAll('[data-att-id]')]
    .map(el => parseInt(el.dataset.attId, 10))
    .filter(Boolean);
}
