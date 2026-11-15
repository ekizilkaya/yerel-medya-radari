// Yerel Medya Radarı — pano. Aynı kod GitHub Pages'te (statik JSON) ve yerel uygulamada (canlı API) çalışır.
import { el, fmt, lineChart, sparkline, barList, choropleth, downloadCSV } from './charts.js';

export const app = { index: null, coverage: null, map: null, local: false, routes: {}, nav: [] };
const view = document.getElementById('view');

export async function getJSON(url, opts) {
  const r = await fetch(url, { cache: 'no-store', ...opts });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* gövde JSON değil */ }
    throw new Error(msg);
  }
  return r.json();
}

const trLower = (s) => s.replace(/I/g, 'ı').replace(/İ/g, 'i').toLowerCase();
const sum = (a, i0 = 0, i1 = a.length - 1) => { let t = 0; for (let i = Math.max(0, i0); i <= i1; i++) t += a[i] || 0; return t; };

function tile(label, value, note) {
  return el('div', { class: 'card tile' }, el('div', { class: 'label', text: label }), el('div', { class: 'value', text: value }),
    note ? el('div', { class: 'note' }, note) : null);
}

export function pageHead(title, desc, ...right) {
  return el('div', { class: 'page-head' }, el('div', {}, el('h1', { text: title }), desc ? el('p', { text: desc }) : null),
    right.length ? el('div', { class: 'toolbar', style: 'margin:0' }, ...right) : null);
}

function seg(options, value, onChange) {
  const box = el('div', { class: 'seg', role: 'group' });
  for (const [key, label] of options) {
    box.append(el('button', { type: 'button', class: key === value ? 'is-on' : '', 'aria-pressed': key === value, text: label, onclick: () => onChange(key) }));
  }
  return box;
}

// ---------------------------------------------------------------------------
// Genel bakış
// ---------------------------------------------------------------------------
function renderOverview() {
  const ix = app.index;
  const s = ix.sources, ls = ix.last_scan;
  view.replaceChildren(
    pageHead(ix.site.title, `Türkiye'nin ${s.cities} ilindeki ${fmt.int(s.usable)} yerel haber sitesi her gün taranır; seçili konuların yerel basındaki seyri burada izlenir.`),
    el('div', { class: 'grid tiles' },
      tile('İzlenen konu', fmt.int(ix.topics.filter((t) => t.active !== false).length)),
      tile('Yerel kaynak', fmt.int(s.usable), `${s.cities} il · toplam ${fmt.int(s.total)} aday`),
      tile('Son tarama', fmt.day(ls.day), ls.n ? `${fmt.int(ls.n)} yeni başlık` : 'henüz veri yok'),
      tile('Arşiv başlangıcı', fmt.day(ix.first_day), ix.first_day.slice(0, 4))),
  );
  if (!ix.topics.length) {
    view.append(el('div', { class: 'card empty' }, el('p', { text: 'Henüz izlenen konu yok.' }),
      app.local ? el('p', {}, el('a', { href: '#/kesfet', text: 'Keşfet sekmesinden bir anahtar kelimeyle başlayın →' })) : null));
    return;
  }
  const cards = el('div', { class: 'grid cards' });
  for (const t of ix.topics) {
    const delta = t.prev7 ? (t.last7 - t.prev7) / t.prev7 : null;
    cards.append(el('a', { class: 'card topic-card', href: `#/konu/${t.slug}` },
      el('div', { class: 'row' }, el('h2', { text: t.title }),
        el('span', {}, t.spike ? el('span', { class: 'pill warn', title: `${fmt.day(t.spike.d)}: ${t.spike.n} haber (beklenen ≈ ${t.spike.expected})`, text: '▲ sıçrama' }) : null,
          t.active === false ? el('span', { class: 'pill', text: 'duraklatıldı' }) : null)),
      t.description ? el('p', { class: 'small muted', text: t.description }) : null,
      el('div', { class: 'row' },
        el('div', {}, el('div', { class: 'big', text: fmt.int(t.last7) }),
          el('div', { class: 'delta', text: 'son 7 günde haber' + (delta != null ? ` · önceki 7 güne göre ${fmt.pct(delta)}` : '') })),
        sparkline(t.spark)),
      t.latest.length ? el('ul', {}, ...t.latest.map((it) => el('li', { text: `${it.c} — ${it.t}` }))) : null));
  }
  view.append(cards);
}

// ---------------------------------------------------------------------------
// Konu sayfası (yerel önizleme de aynı bileşeni kullanır)
// ---------------------------------------------------------------------------
export function mountTopic(host, rep, { preview = false, actions = [] } = {}) {
  const cov = app.coverage;
  const covIdx = new Map(cov.days.map((d, i) => [d, i]));
  const n = rep.days.length;
  const st = { range: n > 45 ? '30' : 'all', metric: 'n', city: null, q: '', term: '', unique: false, shown: 60 };
  const MIN_DENOM = 50;

  const bounds = () => { const len = st.range === 'all' ? n : Math.min(n, +st.range); return [n - len, n - 1]; };
  const dense = (pairs, col) => { const a = new Array(n).fill(0); for (const p of pairs || []) a[p[0]] = p[col]; return a; };
  const denomAt = (i, city) => { const k = covIdx.get(rep.days[i]); return k == null ? 0 : city ? (cov.cities[city]?.[k] || 0) : cov.total[k] || 0; };
  const counts = (city) => (city ? { n: dense(rep.city_days[city], 1), u: dense(rep.city_days[city], 2) } : { n: rep.total, u: rep.uniq });
  const METRICS = { n: 'Haber sayısı', u: 'Tekil hikâye', rate: 'Bin başlıkta' };

  function series() {
    const c = counts(st.city);
    if (st.metric !== 'rate') return c[st.metric];
    return c.n.map((v, i) => { const d = denomAt(i, st.city); return d >= MIN_DENOM ? (v / d) * 1000 : null; });
  }

  function cityValues(i0, i1) {
    const vals = {}, detail = {}, noData = new Set();
    for (const p of app.map.provinces) {
      const c = counts(p.name);
      const nn = sum(c.n, i0, i1), uu = sum(c.u, i0, i1);
      let d = 0; for (let i = i0; i <= i1; i++) d += denomAt(i, p.name);
      detail[p.name] = { n: nn, u: uu, d };
      if (st.metric === 'rate') { if (d < MIN_DENOM) noData.add(p.name); else vals[p.name] = (nn / d) * 1000; }
      else vals[p.name] = st.metric === 'u' ? uu : nn;
    }
    return { vals, detail, noData };
  }

  function filteredItems(i0) {
    const from = rep.days[i0], q = trLower(st.q.trim());
    return rep.items.filter((it) => it.d >= from
      && (!st.city || it.c.split(' / ').includes(st.city))
      && (!st.term || it.k.includes(st.term))
      && (!st.unique || it.gn != null)
      && (!q || trLower(it.t).includes(q) || trLower(it.s).includes(q)));
  }

  const set = (patch) => { Object.assign(st, patch); st.shown = 60; draw(); };

  function draw() {
    const [i0, i1] = bounds();
    const c = counts(st.city);
    const total = sum(c.n, i0, i1), uniq = sum(c.u, i0, i1);
    const len = i1 - i0 + 1;
    const prev = i0 - len >= 0 ? sum(c.n, i0 - len, i0 - 1) : null;
    let denom = 0; for (let i = i0; i <= i1; i++) denom += denomAt(i, st.city);
    const { vals, detail, noData } = cityValues(i0, i1);
    const citiesHit = Object.values(detail).filter((d) => d.n > 0).length;
    const rangeLabel = len === n ? `${fmt.day(rep.days[0])} – ${fmt.day(rep.days[n - 1])}` : `son ${len} gün`;
    const scope = st.city ? `${st.city} · ${rangeLabel}` : rangeLabel;
    const valueFmt = st.metric === 'rate' ? (v) => v.toLocaleString('tr-TR', { maximumFractionDigits: 1 }) : fmt.num;
    const unit = st.metric === 'rate' ? ' ‰' : '';

    host.replaceChildren();
    host.append(pageHead(rep.title + (preview ? ' — önizleme' : ''), rep.description,
      ...actions,
      !preview ? el('a', { class: 'btn', href: `data/feeds/${rep.slug}.xml`, target: '_blank', rel: 'noopener', title: 'Bu konunun yeni haberlerini RSS okuyucunuzdan izleyin', text: 'RSS' }) : null));

    const spike = rep.spikes[rep.spikes.length - 1];
    if (spike && spike.d >= rep.days[Math.max(0, n - 7)]) {
      host.append(el('div', { class: 'alert', role: 'status' }, el('span', { class: 'ico', text: '▲' }),
        el('div', {}, el('strong', { text: `Olağandışı artış — ${fmt.dayLong(spike.d)}` }),
          el('div', { class: 'small muted', text: `${spike.n} haber; önceki haftaların ortalaması ≈ ${fmt.num(spike.expected)}.` }))));
    }

    host.append(el('div', { class: 'filters' },
      seg([['7', '7 gün'], ['30', '30 gün'], ['90', '90 gün'], ['all', 'Tümü']], st.range, (v) => set({ range: v })),
      seg(Object.entries(METRICS), st.metric, (v) => set({ metric: v })),
      st.city ? el('span', { class: 'chip' }, `İl: ${st.city}`, el('button', { type: 'button', 'aria-label': 'İl süzgecini kaldır', text: '×', onclick: () => set({ city: null }) })) : null,
      st.metric === 'rate' ? el('span', { class: 'small muted', text: 'Bin başlıkta: taranan her 1.000 yeni başlıktan kaçı bu konuyla ilgili (kaynak sayısı farkını dengeler).' }) : null));

    host.append(el('div', { class: 'grid tiles' },
      tile('Haber', fmt.int(total), prev != null && prev > 0 ? `önceki ${len} güne göre ${fmt.pct((total - prev) / prev)}` : scope),
      tile('Tekil hikâye', fmt.int(uniq), total ? `kopyalar birleştirilince · ${Math.round((uniq / total) * 100)}%` : null),
      st.city ? tile('İl', st.city, `${fmt.int(cov.sources[st.city]?.usable || 0)} kaynak`) : tile('Haber çıkan il', `${citiesHit} / 81`),
      tile('Bin başlıkta', denom >= MIN_DENOM ? ((total / denom) * 1000).toLocaleString('tr-TR', { maximumFractionDigits: 1 }) + ' ‰' : '—', `${fmt.int(denom)} taranan başlık`)));

    if (!rep.count && !rep.sem_items.length) {
      host.append(el('div', { class: 'card empty', text: 'Bu konu için henüz eşleşen haber yok.' }));
    }

    // Zaman serisi
    const trend = el('div');
    const trendCard = el('div', { class: 'card', style: 'margin-bottom:14px' },
      el('h2', { text: `${METRICS[st.metric]} — günlük` }), el('div', { class: 'sub', text: scope }), trend,
      el('details', { class: 'small', style: 'margin-top:6px' }, el('summary', { text: 'Tablo olarak göster' }),
        el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'Gün' }), el('th', { class: 'num', text: 'Haber' }), el('th', { class: 'num', text: 'Tekil' }), el('th', { class: 'num', text: 'Taranan başlık' }))),
          el('tbody', {}, ...rep.days.slice(i0, i1 + 1).map((d, k) => el('tr', {}, el('td', { text: fmt.dayLong(d) }), el('td', { class: 'num', text: fmt.int(c.n[i0 + k]) }),
            el('td', { class: 'num', text: fmt.int(c.u[i0 + k]) }), el('td', { class: 'num', text: fmt.int(denomAt(i0 + k, st.city)) }))).reverse())))));
    host.append(trendCard);
    const ser = series().slice(i0, i1 + 1);
    lineChart(trend, { days: rep.days.slice(i0, i1 + 1), values: ser, label: METRICS[st.metric] + (st.city ? ` (${st.city})` : ''), unit, valueFmt, marks: st.city ? [] : rep.spikes });

    // Harita + il sıralaması
    const mapHost = el('div'), rankHost = el('div');
    host.append(el('div', { class: 'grid split' },
      el('div', { class: 'card' }, el('h2', { text: 'İllere göre dağılım' }), el('div', { class: 'sub', text: `${METRICS[st.metric]} · ${rangeLabel} · bir ile tıklayarak süzün` }), mapHost),
      el('div', { class: 'card' }, el('h2', { text: 'İl sıralaması' }), el('div', { class: 'sub', text: METRICS[st.metric] + unit }), rankHost)));
    const pick = (name) => set({ city: st.city === name ? null : name });
    choropleth(mapHost, app.map, vals, {
      valueFmt, unit, selected: st.city, onSelect: pick, noData, legendTitle: METRICS[st.metric],
      detail: (name) => [[fmt.int(detail[name].n), 'haber'], [fmt.int(detail[name].u), 'tekil hikâye'], [fmt.int(detail[name].d), 'taranan başlık'], [fmt.int(cov.sources[name]?.usable || 0), 'kaynak']]
        .filter((row, k) => !(st.metric === 'n' && k === 0) && !(st.metric === 'u' && k === 1)),   // ölçünün kendisi zaten ilk satırda
    });
    barList(rankHost, Object.entries(vals).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).map(([name, v]) => ({ key: name, label: name, value: v })),
      { valueFmt, unit, selected: st.city, onSelect: pick, limit: 14, emptyText: 'Bu aralıkta eşleşme yok' });

    // Terimler + kaynaklar
    const items = filteredItems(i0);
    const termHost = el('div'), srcHost = el('div');
    let termRows;
    if (st.city) {
      const tc = {}; for (const it of items) for (const k of it.k) tc[k] = (tc[k] || 0) + 1;
      termRows = Object.entries(tc);
    } else termRows = Object.entries(rep.term_days).map(([k, pairs]) => [k, sum(dense(pairs, 1), i0, i1)]);
    barList(termHost, termRows.filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ key: k, label: k, value: v })),
      { selected: st.term, onSelect: (k) => set({ term: st.term === k ? '' : k }), limit: 10, emptyText: 'Eşleşme yok' });
    const sc = {}; const scity = {};
    for (const it of filteredItems(i0)) { sc[it.s] = (sc[it.s] || 0) + 1; scity[it.s] = it.c; }
    barList(srcHost, Object.entries(sc).sort((a, b) => b[1] - a[1]).map(([s, v]) => ({ key: s, label: s, value: v, title: scity[s] })), { limit: 10, emptyText: 'Eşleşme yok' });
    const truncated = rep.count > rep.items.length && rep.items.length && rep.days[i0] < rep.items[rep.items.length - 1].d;
    host.append(el('div', { class: 'grid two' },
      el('div', { class: 'card' }, el('h2', { text: 'Hangi terimler yakaladı?' }), el('div', { class: 'sub', text: 'Bir haber birden çok terimle eşleşebilir · tıklayınca liste süzülür' }), termHost),
      el('div', { class: 'card' }, el('h2', { text: 'En çok haber çıkan kaynaklar' }), el('div', { class: 'sub', text: scope + (truncated ? ` · son ${rep.items.length} eşleşme üzerinden` : '') }), srcHost)));

    // Haber listesi
    const tbody = el('tbody');
    const more = el('button', { class: 'link-btn', type: 'button' });
    const fill = () => {
      tbody.replaceChildren(...items.slice(0, st.shown).map((it) => el('tr', {},
        el('td', { class: 'nowrap', text: fmt.day(it.d) }), el('td', { class: 'nowrap', text: it.c }),
        el('td', {},
          it.u ? el('a', { href: it.u, target: '_blank', rel: 'noopener noreferrer', text: it.t }) : it.t,
          el('span', { class: 'terms' }, ...it.k.slice(0, 4).map((k) => el('span', { class: 'pill core', text: k })),
            it.gn > 1 ? el('span', { class: 'pill', title: 'Aynı haber birden çok sitede yayımlandı', text: `×${it.gn} site` }) : null,
            it.w === 's' ? el('span', { class: 'pill', title: 'Terim başlıkta değil, haber özetinde geçiyor', text: 'özette' }) : null),
          it.x ? el('div', { class: 'snippet', text: it.x }) : null),
        el('td', { class: 'nowrap', text: it.s }))));
      more.hidden = items.length <= st.shown;
      more.textContent = `Daha fazla göster (${items.length - st.shown} haber daha)`;
    };
    more.onclick = () => { st.shown += 100; fill(); };
    const search = el('input', { type: 'search', class: 'grow', placeholder: 'Başlıkta ya da kaynakta ara…', value: st.q, 'aria-label': 'Haberlerde ara' });
    search.addEventListener('input', () => { st.q = search.value; clearTimeout(search._t); search._t = setTimeout(() => { draw(); const s2 = host.querySelector('input[type=search]'); s2.focus(); s2.setSelectionRange(s2.value.length, s2.value.length); }, 350); });
    host.append(el('div', { class: 'card', style: 'margin-bottom:14px' },
      el('h2', { text: 'Haberler' }), el('div', { class: 'sub', text: `${fmt.int(items.length)} haber · ${scope}` + (st.term ? ` · terim: ${st.term}` : '') }),
      el('div', { class: 'toolbar' }, search,
        el('label', { class: 'small' }, el('input', { type: 'checkbox', checked: st.unique || null, onchange: (e) => set({ unique: e.target.checked }) }), ' Kopyaları gizle'),
        st.term ? el('span', { class: 'chip' }, `Terim: ${st.term}`, el('button', { type: 'button', text: '×', 'aria-label': 'Terim süzgecini kaldır', onclick: () => set({ term: '' }) })) : null,
        el('button', { class: 'btn', type: 'button', text: 'CSV indir', onclick: () => downloadCSV(`${rep.slug}-haberler.csv`, ['Tarih', 'İl', 'Başlık', 'Kaynak', 'Adres', 'Terimler', 'Kopya sayısı'],
          items.map((it) => [it.d, it.c, it.t, it.s, it.u, it.k.join(', '), it.gn || ''])) })),
      el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'Gün' }), el('th', { text: 'İl' }), el('th', { text: 'Başlık' }), el('th', { text: 'Kaynak' }))), tbody)),
      more));
    fill();

    // Anlamsal adaylar
    if (rep.sem_items.length) {
      host.append(el('div', { class: 'card', style: 'margin-bottom:14px' },
        el('details', {}, el('summary', {}, el('strong', { text: `Anlamsal adaylar (${rep.sem_items.length})` }),
          ' — hiçbir terime takılmayan ama anlamca konuya yakın bulunan başlıklar. Sayımlara dahil değildir; gözden geçirmek ve yeni terim keşfetmek içindir.'),
          el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'Gün' }), el('th', { text: 'İl' }), el('th', { text: 'Başlık' }), el('th', { class: 'num', title: 'Arka plandan ayrışma (σ): hem konu tanımına hem bilinen konu haberlerine benzerlik', text: 'Puan (σ)' }))),
            el('tbody', {}, ...rep.sem_items.map((it) => el('tr', {}, el('td', { class: 'nowrap', text: fmt.day(it.d) }), el('td', { class: 'nowrap', text: it.c }),
              el('td', {}, it.u ? el('a', { href: it.u, target: '_blank', rel: 'noopener noreferrer', text: it.t }) : it.t), el('td', { class: 'num', text: (it.sc ?? 0).toFixed(1) })))))))));
    }

    // Konu tanımı
    const chips = (list, cls) => el('div', { class: 'chips' }, ...(list.length ? list.map((t) => el('span', { class: 'pill ' + cls, text: t })) : [el('span', { class: 'muted small', text: '—' })]));
    const on = rep.terms.filter((t) => t.on !== false);
    host.append(el('div', { class: 'card' },
      el('details', {}, el('summary', {}, el('strong', { text: 'Bu konu nasıl tanımlı?' }), ` ${on.length} etkin terim · Türkçe çekim eklerine duyarlı kalıplarla eşleştirilir.`),
        el('div', { class: 'defs small' },
          el('div', {}, el('div', { class: 'muted', text: 'Çekirdek terimler — tek başına yeterli' }), chips(on.filter((t) => t.kind !== 'weak').map((t) => t.t), 'core')),
          el('div', {}, el('div', { class: 'muted', text: 'Bağlam gerektiren terimler — yalnızca bir bağlam sözcüğüyle birlikte geçerse sayılır' }), chips(on.filter((t) => t.kind === 'weak').map((t) => t.t), '')),
          el('div', {}, el('div', { class: 'muted', text: 'Bağlam sözcükleri' }), chips(rep.context, '')),
          el('div', {}, el('div', { class: 'muted', text: 'Dışlanan kalıplar' }), chips(rep.exclude, 'off'))))));
  }
  draw();
  let rt, lastW = host.clientWidth;
  const onResize = () => {
    if (!host.isConnected) { window.removeEventListener('resize', onResize); return; }
    clearTimeout(rt);
    rt = setTimeout(() => { if (host.isConnected && host.clientWidth !== lastW) { lastW = host.clientWidth; draw(); } }, 200);
  };
  window.addEventListener('resize', onResize);
}

async function renderTopicRoute(slug) {
  const rep = await getJSON(`data/topics/${encodeURIComponent(slug)}.json`);
  const actions = [];
  if (app.local) {
    actions.push(el('a', { class: 'btn', href: `#/kesfet/${slug}`, text: 'Tanımı düzenle' }));
    if (app.localActions) actions.push(...app.localActions(slug));
  }
  view.replaceChildren(el('p', { class: 'small', style: 'margin-bottom:8px' }, el('a', { href: '#/', text: '← Tüm konular' })), el('div', { id: 'topicHost' }));
  mountTopic(view.querySelector('#topicHost'), rep, { actions });
}

// ---------------------------------------------------------------------------
// Kapsam: kaç kaynak, hangi illerde, günde kaç başlık
// ---------------------------------------------------------------------------
function renderCoverage() {
  const cov = app.coverage, ix = app.index;
  const usable = Object.fromEntries(Object.entries(cov.sources).map(([c, v]) => [c, v.usable]));
  const n = cov.days.length;
  const last30 = (arr) => sum(arr, Math.max(0, n - 30), n - 1) / Math.max(1, Math.min(30, n));
  const mapHost = el('div'), rankHost = el('div'), trend = el('div');
  view.replaceChildren(
    pageHead('Kapsam', 'Taranan yerel kaynaklar ve günlük başlık hacmi. Kaynak listesi Basın İlan Kurumu\'nun aylık internet haber sitesi listesinden ve elle eklenen sitelerden oluşur; her site için RSS beslemesi ya da ana sayfa başlıkları okunur.'),
    el('div', { class: 'grid tiles' },
      tile('Kullanılabilir kaynak', fmt.int(ix.sources.usable), `${fmt.int(ix.sources.total)} adayın içinden`),
      tile('RSS beslemesi olan', fmt.int(Object.values(cov.sources).reduce((a, v) => a + v.rss, 0)), 'kalanı ana sayfadan okunur'),
      tile('Kapsanan il', `${Object.values(usable).filter((v) => v > 0).length} / 81`),
      tile('Günlük yeni başlık', n ? fmt.int(last30(cov.total)) : '—', n ? `son ${Math.min(30, n)} gün ortalaması` : 'henüz tarama yok')),
    el('div', { class: 'grid split' },
      el('div', { class: 'card' }, el('h2', { text: 'İl başına kaynak sayısı' }), el('div', { class: 'sub', text: 'Kaynak sayısı iller arasında çok değişir; konu sayfalarındaki "Bin başlıkta" ölçüsü bu farkı dengeler.' }), mapHost),
      el('div', { class: 'card' }, el('h2', { text: 'İl sıralaması' }), el('div', { class: 'sub', text: 'Kullanılabilir kaynak' }), rankHost)),
    el('div', { class: 'card' }, el('h2', { text: 'Günlük taranan yeni başlık' }), el('div', { class: 'sub', text: 'Daha önce görülmemiş başlıklar; konu eşleşmelerinin paydası' }), trend));
  choropleth(mapHost, app.map, usable, { valueFmt: fmt.int, legendTitle: 'Kaynak',
    detail: (name) => [[fmt.int(cov.sources[name].rss), 'RSS beslemeli'], [fmt.int(last30(cov.cities[name] || [])), 'yeni başlık / gün']] });
  barList(rankHost, Object.entries(usable).sort((a, b) => b[1] - a[1]).map(([c, v]) => ({ key: c, label: c, value: v })), { valueFmt: fmt.int, limit: 14 });
  if (n) lineChart(trend, { days: cov.days, values: cov.total, label: 'Yeni başlık', valueFmt: fmt.compact });
  else trend.append(el('p', { class: 'muted', text: 'Henüz tarama yapılmadı.' }));
  if (app.local && app.coverageExtras) view.append(app.coverageExtras());
}

// ---------------------------------------------------------------------------
// Yönlendirme
// ---------------------------------------------------------------------------
function drawNav(active) {
  const nav = document.getElementById('nav');
  nav.replaceChildren(...app.nav.map((item) => (item === '|' ? el('span', { class: 'nav-sep' })
    : el('a', { href: `#/${item[0]}`, class: item[0] === active ? 'is-active' : '', text: item[1] }))));
  if (app.local) nav.append(el('span', { class: 'badge-local', style: 'align-self:center;margin-left:6px', title: 'Bu bilgisayarda çalışan sürüm: tarama, model ve düzenleme araçları açık', text: 'yerel' }));
}

export async function route() {
  const [name = '', ...rest] = decodeURIComponent(location.hash.replace(/^#\/?/, '')).split('/');
  const handler = app.routes[name] || app.routes[''];
  drawNav(name === 'konu' ? '' : name);
  window.scrollTo(0, 0);
  try {
    await handler(rest.join('/'));
  } catch (err) {
    view.replaceChildren(el('div', { class: 'alert error' }, el('span', { class: 'ico', text: '!' }), el('div', {}, el('strong', { text: 'Sayfa yüklenemedi' }), el('div', { class: 'small', text: err.message }))));
  }
}

export async function reloadData() {
  [app.index, app.coverage] = await Promise.all([getJSON('data/index.json'), getJSON('data/coverage.json')]);
}

async function boot() {
  document.getElementById('themeBtn').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('radar-theme', root.dataset.theme); } catch (e) { /* özel pencere */ }
  });
  try {
    [app.map] = await Promise.all([getJSON('assets/tr-map.json'), reloadData()]);
  } catch (err) {
    view.replaceChildren(el('div', { class: 'card empty' }, el('p', { text: 'Pano verisi henüz üretilmemiş.' }),
      el('p', { class: 'small muted', text: 'İlk günlük tarama tamamlandığında bu sayfa dolacak. (' + err.message + ')' })));
    return;
  }
  app.local = app.index.mode === 'local';
  document.getElementById('siteTitle').textContent = app.index.site.title;
  document.title = app.index.site.title;
  app.routes = { '': renderOverview, konu: renderTopicRoute, kapsam: renderCoverage };
  app.nav = [['', 'Genel bakış'], ['kapsam', 'Kapsam']];
  if (app.local) (await import('./local.js')).install(app);

  const gh = location.hostname.match(/^(.+)\.github\.io$/);
  const repo = gh ? `https://github.com/${gh[1]}/${location.pathname.split('/')[1]}` : null;
  document.getElementById('footer').replaceChildren(
    `Son güncelleme: ${new Date(app.index.generated).toLocaleString('tr-TR', { dateStyle: 'long', timeStyle: 'short' })} · `,
    'Başlıklar ilgili haber sitelerine aittir; burada yalnızca başlık ve bağlantı gösterilir. ',
    repo ? el('a', { href: repo, target: '_blank', rel: 'noopener', text: 'Kaynak kod ve veri' }) : '');
  window.addEventListener('hashchange', route);
  route();
}

boot();
