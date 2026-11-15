// Yalnızca yerel uygulamada yüklenen paneller: Keşfet (anlamsal genişletme), Gündem, Ayarlar, tarama.
import { el, fmt, barList } from './charts.js';
import { getJSON, mountTopic, pageHead, reloadData, route } from './app.js';

const view = document.getElementById('view');
const post = (url, body, method = 'POST') => getJSON(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });

// Uzun işlemler sunucudan Server-Sent Events olarak akar
async function job(url, body, { onLog, onProgress, onEvent } = {}) {
  const resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '', result = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let k;
    while ((k = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
      let event = 'message', data = '';
      for (const line of chunk.split('\n')) { if (line.startsWith('event: ')) event = line.slice(7); else if (line.startsWith('data: ')) data += line.slice(6); }
      const payload = data ? JSON.parse(data) : null;
      if (event === 'done') result = payload;
      else if (event === 'error') throw new Error(payload.message);
      else if (event === 'log') onLog?.(payload);
      else if (event === 'progress') onProgress?.(payload);
      else onEvent?.(event, payload);
    }
  }
  return result;
}

function jobPanel() {
  const bar = el('i'), log = el('div', { class: 'log', 'aria-live': 'polite' });
  const box = el('div', { hidden: true }, el('div', { class: 'progress' }, bar), log);
  const api = {
    box,
    start() { box.hidden = false; log.textContent = ''; bar.style.width = '2%'; },
    log(p) { log.textContent += `[${p.stage}] ${p.detail}\n`; log.scrollTop = log.scrollHeight; },
    progress(p) { if (p.total) bar.style.width = `${Math.max(2, (p.done / p.total) * 100)}%`; },
    done(msg) { bar.style.width = '100%'; if (msg) api.log({ stage: 'bitti', detail: msg }); },
    fail(err) { api.log({ stage: 'hata', detail: err.message }); },
    handlers() { return { onLog: api.log, onProgress: api.progress }; },
  };
  return api;
}

async function withButton(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Çalışıyor…';
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = label; }
}

function notice(host, text, error = false) {
  host.replaceChildren(el('div', { class: 'alert' + (error ? ' error' : ''), role: 'status' }, el('span', { class: 'ico', text: error ? '!' : '✓' }), el('div', { class: 'small', style: 'white-space:pre-wrap', text })));
}

// ---------------------------------------------------------------------------
// Keşfet: anahtar kelime → anlamsal genişletme → düzenle → önizle → kaydet
// ---------------------------------------------------------------------------
const slugify = (s) => s.replace(/I/g, 'ı').replace(/İ/g, 'i').toLowerCase().replace(/[çÇ]/g, 'c').replace(/[ğĞ]/g, 'g').replace(/ı/g, 'i').replace(/[öÖ]/g, 'o').replace(/[şŞ]/g, 's').replace(/[üÜ]/g, 'u').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48) || 'konu';
const KIND_LABEL = { core: 'çekirdek — tek başına yeterli', weak: 'bağlam gerektirir', off: 'kapalı' };

async function renderExplore(slug) {
  let draft = null, suggestions = [], neighbors = [], existing = false;
  if (slug) {
    const all = await getJSON('api/topics');
    draft = all.find((t) => t.slug === slug) || null;
    existing = !!draft;
  }
  const status = await getJSON('api/status');
  const kw = el('input', { type: 'text', class: 'grow', placeholder: 'Anahtar kelime — ör. fon, kentsel dönüşüm, su kesintisi', value: draft?.keyword || '', 'aria-label': 'Anahtar kelime' });
  const note = el('input', { type: 'text', class: 'grow', placeholder: 'İsteğe bağlı not: kapsamı tarif edin (ör. “hibe ve destek fonları; yatırım fonları değil”)', 'aria-label': 'Kapsam notu' });
  const optEmbed = el('input', { type: 'checkbox', checked: true }), optAudit = el('input', { type: 'checkbox', checked: true }), optDeep = el('input', { type: 'checkbox' });
  const panel = jobPanel();
  const editor = el('div'), previewHost = el('div'), msg = el('div');
  const runBtn = el('button', { class: 'btn primary', type: 'button', text: 'Genişlet' });

  runBtn.onclick = () => withButton(runBtn, async () => {
    if (kw.value.trim().length < 2) { kw.focus(); return; }
    panel.start(); msg.replaceChildren(); previewHost.replaceChildren();
    try {
      const res = await job('api/expand', { keyword: kw.value.trim(), note: note.value, embeddings: optEmbed.checked, audit: optAudit.checked, deep: optDeep.checked }, panel.handlers());
      draft = res.topic; suggestions = res.suggestions || []; neighbors = res.neighbors || [];
      if (existing && slug) draft.slug = slug;
      panel.done('Taslak hazır — aşağıdan düzenleyip önizleyebilirsiniz.');
      drawEditor();
    } catch (err) { panel.fail(err); }
  });
  kw.addEventListener('keydown', (e) => { if (e.key === 'Enter') runBtn.click(); });

  function drawEditor() {
    editor.replaceChildren();
    if (!draft) return;
    const title = el('input', { type: 'text', value: draft.title, 'aria-label': 'Konu başlığı', oninput: (e) => { draft.title = e.target.value; } });
    const desc = el('textarea', { rows: 2, style: 'width:100%', 'aria-label': 'Açıklama', oninput: (e) => { draft.description = e.target.value; } });
    desc.value = draft.description || '';
    const list = (key) => el('input', { type: 'text', class: 'grow', value: (draft[key] || []).join(', '), oninput: (e) => { draft[key] = e.target.value.split(',').map((s) => s.trim()).filter(Boolean); } });

    const chips = el('div', { class: 'suggest', style: 'gap:6px' });
    const drawChips = () => {
      chips.replaceChildren(...draft.terms.map((t, i) => {
        const state = t.on === false ? 'off' : t.kind === 'weak' ? 'weak' : 'core';
        const info = [KIND_LABEL[state], t.hits != null ? `arşivde ${t.hits} başlık` : null, t.prec != null ? `kesinlik ${Math.round(t.prec * 100)}%` : null, t.src ? `kaynak: ${t.src}` : null].filter(Boolean).join(' · ');
        return el('span', { class: `term-edit ${state}`, title: info },
          el('button', { type: 'button', class: 'name', text: (state === 'weak' ? '◐ ' : state === 'core' ? '● ' : '○ ') + t.t, 'aria-label': `${t.t}: ${KIND_LABEL[state]} — değiştirmek için tıklayın`,
            onclick: () => { if (state === 'core') { t.kind = 'weak'; t.on = true; } else if (state === 'weak') { t.on = false; } else { t.kind = 'core'; t.on = true; } drawChips(); } }),
          t.hits ? el('small', { text: t.hits }) : null,
          el('button', { type: 'button', class: 'x', 'aria-label': `${t.t} terimini sil`, text: '×', onclick: () => { draft.terms.splice(i, 1); drawChips(); } }));
      }));
    };
    drawChips();
    const add = el('input', { type: 'text', placeholder: 'Terim ekle — ör. hibe · fonla* · "tam ifade" · KOSGEB · re:\\bipard\\b', class: 'grow', 'aria-label': 'Terim ekle' });
    const addTerm = (text, kind = 'core', src = 'elle') => {
      const t = text.trim();
      if (t && !draft.terms.some((x) => x.t.toLowerCase() === t.toLowerCase())) draft.terms.unshift({ t, kind, on: true, src });
      drawChips();
    };
    add.addEventListener('keydown', (e) => { if (e.key === 'Enter') { addTerm(add.value); add.value = ''; } });

    const sugBox = el('div', { class: 'suggest' }, ...suggestions.map((s) => el('button', {
      type: 'button', text: `+ ${s.t}`, title: `${s.src} · ${s.hits ?? 0} başlık\nörn: ${s.example || ''}`,
      onclick: (e) => { addTerm(s.t, 'weak', s.src); e.target.remove(); },
    })));

    const saveBtn = el('button', { class: 'btn primary', type: 'button', text: existing ? 'Değişiklikleri kaydet' : 'Kaydet ve izlemeye al' });
    const prevBtn = el('button', { class: 'btn', type: 'button', text: 'Önizle' });
    const deepBtn = el('button', { class: 'btn', type: 'button', text: 'Derin arama', title: 'Çekirdek terimleri sitelerin kendi aramalarında arar (geçmişe dönük; birkaç dakika sürebilir)' });
    const delBtn = existing ? el('button', { class: 'btn', type: 'button', text: 'Konuyu sil' }) : null;
    const deepPanel = jobPanel(), deepHost = el('div');

    prevBtn.onclick = () => withButton(prevBtn, async () => {
      try {
        const rep = await post('api/preview', { topic: draft });
        mountTopic(previewHost, rep, { preview: true });
        previewHost.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (err) { notice(msg, err.message, true); }
    });
    saveBtn.onclick = () => withButton(saveBtn, async () => {
      try {
        draft.slug = draft.slug || slugify(draft.title || draft.keyword);
        const res = await post(`api/topics/${draft.slug}`, draft, 'PUT');
        existing = true;
        await reloadData();
        notice(msg, `Kaydedildi: data/topics/${res.slug}.json. Bu bilgisayarda hemen izlenir; GitHub'daki günlük taramaya eklemek için Ayarlar → “Konuları GitHub'a gönder”.`);
      } catch (err) { notice(msg, err.message, true); }
    });
    deepBtn.onclick = () => withButton(deepBtn, async () => {
      deepPanel.start(); deepHost.replaceChildren();
      try {
        const res = await job('api/deepsearch', { topic: draft, max_terms: 2 }, {
          onLog: deepPanel.log, onProgress: (p) => { deepPanel.progress(p); if (p.done % 100 === 0) deepPanel.log({ stage: 'arama', detail: `${p.done}/${p.total} site · ${p.found} sonuç` }); },
        });
        deepPanel.done(`${res.items.length} sonuç · ${res.searchable}/${res.sources} sitede çalışan arama bulundu`);
        const byCity = {}; for (const it of res.items) byCity[it.c] = (byCity[it.c] || 0) + 1;
        const cityHost = el('div');
        deepHost.append(el('div', { class: 'grid two', style: 'margin-top:12px' },
          el('div', {}, el('h3', { text: 'İllere göre sonuç' }), cityHost),
          el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'Tarih' }), el('th', { text: 'İl' }), el('th', { text: 'Başlık' }), el('th', { text: 'Kaynak' }))),
            el('tbody', {}, ...res.items.slice(0, 300).map((it) => el('tr', {}, el('td', { class: 'nowrap', text: it.p || '—' }), el('td', { class: 'nowrap', text: it.c }),
              el('td', {}, el('a', { href: it.u, target: '_blank', rel: 'noopener noreferrer', text: it.t })), el('td', { class: 'nowrap', text: it.s }))))))));
        barList(cityHost, Object.entries(byCity).sort((a, b) => b[1] - a[1]).map(([c, v]) => ({ key: c, label: c, value: v })), { valueFmt: fmt.int, limit: 12, emptyText: 'Sonuç yok' });
      } catch (err) { deepPanel.fail(err); }
    });
    if (delBtn) delBtn.onclick = async () => {
      if (delBtn.dataset.armed !== '1') { delBtn.dataset.armed = '1'; delBtn.textContent = 'Emin misiniz? Silmek için yeniden tıklayın'; return; }
      await getJSON(`api/topics/${draft.slug}`, { method: 'DELETE' });
      await reloadData(); location.hash = '#/';
    };

    editor.append(el('div', { class: 'card', style: 'margin-bottom:14px' },
      el('h2', { text: 'Konu tanımı' }), el('div', { class: 'sub', text: 'Terime tıklayarak türünü değiştirin: ● çekirdek → ◐ bağlam gerektirir → ○ kapalı. Sayı, terimin yerel arşivde yakaladığı başlık adedidir.' }),
      el('div', { class: 'form-row' }, el('label', { text: 'Başlık' }), title),
      desc,
      el('h3', { style: 'margin:12px 0 6px', text: 'Terimler' }), chips,
      el('div', { class: 'form-row', style: 'margin-top:8px' }, add, el('button', { class: 'btn', type: 'button', text: 'Ekle', onclick: () => { addTerm(add.value); add.value = ''; } })),
      suggestions.length ? el('div', {}, el('h3', { style: 'margin:10px 0 6px', text: 'Öneriler' }), el('div', { class: 'sub small muted', text: 'Arşivdeki konu haberlerinde orantısız sık geçen sözcükler ve anlamsal komşulardan çıkanlar. Tıklayınca “bağlam gerektirir” olarak eklenir.' }), sugBox) : null,
      el('div', { class: 'form-row', style: 'margin-top:12px' }, el('label', { text: 'Bağlam sözcükleri' }), list('context')),
      el('div', { class: 'form-row' }, el('label', { text: 'Dışlanan kalıplar' }), list('exclude')),
      neighbors.length ? el('details', { class: 'small', style: 'margin:6px 0 10px' }, el('summary', { text: `Anlamsal komşu başlıklar (${neighbors.length}) — terimlere takılmayan ama konuya yakın bulunanlar` }),
        el('ul', { style: 'margin:6px 0;padding-left:18px' }, ...neighbors.map((nb) => el('li', { text: `${nb.sc.toFixed(1)}σ · ${nb.t}` })))) : null,
      el('div', { class: 'form-row' }, prevBtn, saveBtn, deepBtn, delBtn),
      msg, deepPanel.box, deepHost));
  }

  view.replaceChildren(
    pageHead('Keşfet', 'Bir anahtar kelime yazın; yerel dil modeli onu ilgili terimlere genişletir, terimler yerel arşivdeki gerçek başlıklarla sınanır, siz son hâlini verirsiniz.'),
    status.store.articles ? '' : el('div', { class: 'alert' }, el('span', { class: 'ico', text: 'i' }), el('div', { class: 'small' }, 'Yerel arşiv boş. Terimlerin gerçek başlıklarla sınanabilmesi için önce ', el('a', { href: '#/kapsam', text: 'Kapsam sekmesinden bir tarama yapın' }), '.')),
    el('div', { class: 'card', style: 'margin-bottom:14px' },
      el('h2', { text: existing ? `“${draft.title}” konusunu yeniden genişlet` : 'Anahtar kelimeden konu üret' }),
      el('div', { class: 'sub', text: `Sohbet modeli: ${status.runtime.selected.chat.split('/').pop() || '—'} · gömme modeli: ${status.runtime.selected.embed.split('/').pop() || '—'} (Ayarlar'dan değiştirilebilir)` }),
      el('div', { class: 'form-row' }, kw, runBtn),
      el('div', { class: 'form-row' }, note),
      el('div', { class: 'form-row small' },
        el('label', {}, optAudit, ' Dil modeli denetimi (terimlerin yakaladığı örnek başlıklar konuyla ilgili mi?)'),
        el('label', {}, optEmbed, ' Gömme analizi (anlamsal komşular)'),
        el('label', {}, optDeep, ' Alt temaları da tara (daha geniş, daha yavaş)')),
      panel.box),
    editor, previewHost);
  drawEditor();
}

// ---------------------------------------------------------------------------
// Gündem: ulusal medyaya taşınabilecek önemli yerel haberler
// ---------------------------------------------------------------------------
async function renderDigest() {
  const panel = jobPanel(), list = el('div', { class: 'news' }), head = el('div', { class: 'sub' });
  const hours = el('select', { 'aria-label': 'Zaman aralığı' }, el('option', { value: 24, text: 'Bugün' }), el('option', { value: 48, text: 'Son 2 gün' }));
  const btn = el('button', { class: 'btn primary', type: 'button', text: 'Gündem özeti oluştur' });
  const item = (n) => el('div', { class: 'news-item' },
    el('h3', {}, n.url ? el('a', { href: n.url, target: '_blank', rel: 'noopener noreferrer', text: n.title }) : n.title),
    el('p', { class: 'small', style: 'color:var(--ink-2)', text: n.summary }),
    el('div', { class: 'meta' }, el('span', { class: 'pill core', text: n.city }), n.category ? el('span', { class: 'pill', text: n.category }) : null,
      n.sources > 1 ? el('span', { class: 'pill', text: `${n.sources} kaynak` }) : null, el('span', { class: 'pill', text: n.source })));
  btn.onclick = () => withButton(btn, async () => {
    panel.start(); list.replaceChildren(); head.textContent = '';
    try {
      const res = await job('api/digest', { hours: +hours.value }, { ...panel.handlers(), onEvent: (ev, n) => { if (ev === 'news') list.append(item(n)); } });
      panel.done(`${res.total} haber seçildi (${fmt.int(res.headlines)} başlık içinden)`);
    } catch (err) { panel.fail(err); }
  });
  view.replaceChildren(
    pageHead('Gündem', 'Yerel dil modeli, taranan başlıkları kümeler ve ulusal medyaya taşınmaya değer yerel haberleri seçip özetler.'),
    el('div', { class: 'card', style: 'margin-bottom:14px' }, el('div', { class: 'form-row' }, hours, btn), panel.box),
    el('div', { class: 'card' }, el('h2', { text: 'Seçilen haberler' }), head, list));
  const last = await getJSON('api/digest');
  if (last.items?.length) {
    head.textContent = `Son özet: ${new Date(last.generated).toLocaleString('tr-TR', { dateStyle: 'medium', timeStyle: 'short' })}`;
    list.append(...last.items.map(item));
  } else list.append(el('p', { class: 'muted', text: 'Henüz özet oluşturulmadı.' }));
}

// ---------------------------------------------------------------------------
// Ayarlar: model seçimi, çalışma zamanı, GitHub
// ---------------------------------------------------------------------------
async function renderSettings() {
  const data = await getJSON('api/models');
  const s = data.settings, rt = data.runtime;
  const msg = el('div');
  const option = (m) => el('option', { value: m.id, text: `${m.name} — ${m.size_gb} GB${m.params ? ' · ' + m.params : ''} (${m.file})` });
  const pick = (role, kind, key) => {
    const sel = el('select', { style: 'max-width:100%', 'aria-label': role }, el('option', { value: '', text: `Otomatik (${rt.selected[role === 'Sohbet modeli' ? 'chat' : 'embed'].split('/').pop() || 'model yok'})` }),
      ...data.models.filter((m) => m.kind === kind).map(option));
    sel.value = s[key] || '';
    sel.onchange = async () => { await post('api/settings', { [key]: sel.value }); notice(msg, 'Kaydedildi. Model bir sonraki kullanımda yüklenir.'); };
    return sel;
  };
  const field = (key, node) => { node.value = s[key]; node.onchange = async () => { await post('api/settings', { [key]: node.type === 'number' ? +node.value : node.value }); notice(msg, 'Kaydedildi.'); }; return node; };
  const loaded = Object.entries(rt.loaded).map(([role, v]) => `${role === 'chat' ? 'sohbet' : 'gömme'}: ${v.model.split('/').pop()}`).join(' · ') || 'yok';
  const dlPanel = jobPanel(), ghMsg = el('div'), syncPanel = jobPanel();
  const stopBtn = el('button', { class: 'btn', type: 'button', text: 'Modelleri bellekten at', onclick: async () => { await post('api/runtime/stop'); route(); } });
  const dlBtn = el('button', { class: 'btn', type: 'button', text: 'Güncel llama.cpp indir' });
  dlBtn.onclick = () => withButton(dlBtn, async () => { dlPanel.start(); try { const r = await job('api/runtime/download', {}, dlPanel.handlers()); dlPanel.done(r.exe); } catch (err) { dlPanel.fail(err); } });
  const pubBtn = el('button', { class: 'btn primary', type: 'button', text: "Konuları GitHub'a gönder" });
  pubBtn.onclick = () => withButton(pubBtn, async () => { try { const r = await post('api/publish'); notice(ghMsg, r.message); } catch (err) { notice(ghMsg, err.message, true); } });
  const syncBtn = el('button', { class: 'btn', type: 'button', text: 'Buluttaki arşivi indir' });
  syncBtn.onclick = () => withButton(syncBtn, async () => { syncPanel.start(); try { const r = await job('api/sync', { days: 90 }, syncPanel.handlers()); syncPanel.done(`${r.days} gün indirildi, ${fmt.int(r.articles)} yeni başlık içe aktarıldı`); await reloadData(); } catch (err) { syncPanel.fail(err); } });
  const status = await getJSON('api/status');

  view.replaceChildren(
    pageHead('Ayarlar', 'Yerel dil modeli bu bilgisayarda çalışır; LM Studio\'nun açık olması gerekmez. Uygulama, LM Studio\'nun indirdiği GGUF modelleri doğrudan llama.cpp ile yükler.'),
    el('div', { class: 'card', style: 'margin-bottom:14px' },
      el('h2', { text: 'Modeller' }), el('div', { class: 'sub', text: `${data.models.length} model bulundu · klasörler: ${data.model_dirs.join(' ; ')}` }),
      el('div', { class: 'form-row' }, el('label', { style: 'min-width:120px', text: 'Sohbet modeli' }), pick('Sohbet modeli', 'chat', 'chat_model')),
      el('p', { class: 'small muted', style: 'margin:-4px 0 10px', text: 'Terim üretimi ve denetimi için. Büyük modeller belirgin biçimde daha isabetli terim üretir; küçük modeller daha hızlıdır.' }),
      el('div', { class: 'form-row' }, el('label', { style: 'min-width:120px', text: 'Gömme modeli' }), pick('Gömme modeli', 'embedding', 'embed_model')),
      el('p', { class: 'small muted', style: 'margin:-4px 0 10px', text: 'Anlamsal komşu ve aday araması için (embedding).' }),
      el('div', { class: 'form-row' },
        el('label', { text: 'Düşünme' }), field('reasoning', el('select', {}, el('option', { value: 'off', text: 'kapalı (hızlı)' }), el('option', { value: 'auto', text: 'modelin varsayılanı' }), el('option', { value: 'on', text: 'açık' }))),
        el('label', { text: 'Bağlam' }), field('ctx', el('input', { type: 'number', min: 4096, step: 4096, style: 'width:100px' })),
        el('label', { text: 'Ekran kartı katmanı' }), field('gpu_layers', el('input', { type: 'text', style: 'width:80px', title: 'auto | all | sayı' })),
        el('label', { text: 'Boşta kapat (dk)' }), field('idle_stop_min', el('input', { type: 'number', min: 0, style: 'width:70px' }))),
      el('dl', { class: 'kv', style: 'margin-top:10px' },
        el('dt', { text: 'Çalışma zamanı' }), el('dd', { text: rt.llama_server ? `${rt.llama_origin} — ${rt.llama_server}` : 'llama-server bulunamadı; aşağıdan indirin' }),
        el('dt', { text: 'Ekran kartı' }), el('dd', { text: rt.gpu ? `${rt.gpu.name} · ${fmt.int(rt.gpu.free)} / ${fmt.int(rt.gpu.total)} MiB boş` : 'NVIDIA kart bulunamadı (CPU/Vulkan kullanılır)' }),
        el('dt', { text: 'Yüklü modeller' }), el('dd', { text: loaded }),
        rt.last_error ? el('dt', { text: 'Son hata' }) : null, rt.last_error ? el('dd', { style: 'white-space:pre-wrap;font-size:.8rem', text: rt.last_error.slice(0, 900) }) : null),
      el('div', { class: 'form-row', style: 'margin-top:12px' }, stopBtn, dlBtn), dlPanel.box, msg,
      el('details', { class: 'small', style: 'margin-top:8px' }, el('summary', { text: 'Harici sunucu kullan (LM Studio / Ollama / başka bir OpenAI uyumlu uç nokta)' }),
        el('div', { class: 'form-row', style: 'margin-top:8px' },
          field('backend', el('select', {}, el('option', { value: 'managed', text: 'Uygulama modeli kendisi yüklesin (önerilen)' }), el('option', { value: 'external', text: 'Harici sunucu' }))),
          field('external_url', el('input', { type: 'text', class: 'grow', placeholder: 'http://127.0.0.1:1234/v1' }))))),
    el('div', { class: 'card' },
      el('h2', { text: 'GitHub — bilgisayar kapalıyken günlük tarama' }),
      el('div', { class: 'sub', text: status.repo ? `Bağlı depo: ${status.repo}` : 'Bu klasör henüz bir GitHub deposuna bağlı değil (README → “GitHub’da yayınlama”).' }),
      el('p', { class: 'small', style: 'margin-bottom:10px;color:var(--ink-2)', text: 'Konu tanımları depoya gönderildiğinde GitHub Actions her gün tüm kaynakları tarar, eşleşmeleri kaydeder ve panoyu GitHub Pages üzerinde günceller. Bulutta dil modeli gerekmez: anlamsal genişletme burada yapılır, günlük tarama üretilen kalıpları kullanır.' }),
      el('div', { class: 'form-row' }, pubBtn, syncBtn), ghMsg, syncPanel.box,
      el('dl', { class: 'kv', style: 'margin-top:10px' }, el('dt', { text: 'Yerel arşiv' }),
        el('dd', { text: status.store.articles ? `${fmt.int(status.store.articles)} başlık · ${status.store.sources} kaynak · ${status.store.first_day} → ${status.store.last_day}` : 'boş' }))));
}

// ---------------------------------------------------------------------------
// Kapsam sayfasına yerel ek: anlık tarama + kaynak tablosu
// ---------------------------------------------------------------------------
function coverageExtras() {
  const panel = jobPanel(), tableHost = el('div');
  const btn = el('button', { class: 'btn primary', type: 'button', text: 'Şimdi tara' });
  btn.onclick = () => withButton(btn, async () => {
    panel.start();
    try {
      const r = await job('api/scan', {}, panel.handlers());
      panel.done(`${fmt.int(r.scanned)} başlık okundu · ${fmt.int(r.new)} yeni · ${r.sources_ok}/${r.sources} kaynak yanıt verdi`);
      await reloadData();
    } catch (err) { panel.fail(err); }
  });
  const showBtn = el('button', { class: 'btn', type: 'button', text: 'Kaynak listesini göster' });
  showBtn.onclick = async () => {
    const { sources } = await getJSON('api/sources');
    const q = el('input', { type: 'search', class: 'grow', placeholder: 'İl ya da alan adı ara…', 'aria-label': 'Kaynak ara' });
    const tbody = el('tbody');
    const fill = () => {
      const needle = q.value.toLowerCase();
      tbody.replaceChildren(...sources.filter((s) => !needle || s.domain.includes(needle) || s.city.toLowerCase().includes(needle)).slice(0, 400).map((s) => el('tr', {},
        el('td', { text: s.city }), el('td', {}, el('a', { href: `https://${s.domain}`, target: '_blank', rel: 'noopener noreferrer', text: s.domain })),
        el('td', { text: s.scope === 'ulusal' ? 'ulusal (taranmaz)' : s.method === 'rss' ? 'RSS' : s.method === 'html' ? 'ana sayfa' : s.status || '—' }), el('td', { text: (s.origin || []).join(', ') }), el('td', { text: s.bik || '' }))));
    };
    q.addEventListener('input', fill); fill();
    tableHost.replaceChildren(el('div', { class: 'toolbar', style: 'margin-top:12px' }, q, el('span', { class: 'small muted', text: `${sources.length} kayıt` })),
      el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'İl' }), el('th', { text: 'Site' }), el('th', { text: 'Okuma yöntemi' }), el('th', { text: 'Liste' }), el('th', { text: 'BİK' }))), tbody)));
  };
  return el('div', { class: 'card', style: 'margin-top:14px' },
    el('h2', { text: 'Yerel tarama' }), el('div', { class: 'sub', text: 'Tüm kaynakları şimdi tarar ve bu bilgisayardaki arşive ekler (yaklaşık 1 dakika). Kaynak listesini güncellemek için: python scripts/update_sources.py' }),
    el('div', { class: 'form-row' }, btn, showBtn), panel.box, tableHost);
}

function localActions(slug) {
  const btn = el('button', { class: 'btn', type: 'button', text: 'Anlamsal adayları bul', title: 'Gömme modeliyle, terimlere takılmayan ama konuya yakın başlıkları arar' });
  btn.onclick = () => withButton(btn, async () => {
    try { await job(`api/semantic/${slug}`, {}); route(); }
    catch (err) { btn.after(el('span', { class: 'small', style: 'color:var(--critical)', text: ' ' + err.message })); }
  });
  return [btn];
}

export function install(app) {
  Object.assign(app.routes, { kesfet: renderExplore, gundem: renderDigest, ayarlar: renderSettings });
  app.nav.push('|', ['kesfet', 'Keşfet'], ['gundem', 'Gündem'], ['ayarlar', 'Ayarlar']);
  app.coverageExtras = coverageExtras;
  app.localActions = localActions;
}
