// Grafik bileşenleri — dış kütüphane yok; düz SVG/HTML.
// Kurallar: tek eksen, ince işaretler, kılcal ızgara, renk yalnızca işarette (metin her zaman mürekkep tonunda),
// her grafikte üzerine gelince ipucu + tablo karşılığı.

const NS = 'http://www.w3.org/2000/svg';
const MONTHS = ['Oca', 'Şub', 'Mar', 'Nis', 'May', 'Haz', 'Tem', 'Ağu', 'Eyl', 'Eki', 'Kas', 'Ara'];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c.nodeType ? c : document.createTextNode(c));
  return node;
}

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

export const fmt = {
  int: (n) => Math.round(n).toLocaleString('tr-TR'),
  num: (n) => (n >= 100 ? Math.round(n).toLocaleString('tr-TR') : n.toLocaleString('tr-TR', { maximumFractionDigits: 1 })),
  compact: (n) => (n >= 10000 ? (n / 1000).toLocaleString('tr-TR', { maximumFractionDigits: 1 }) + ' B' : Math.round(n).toLocaleString('tr-TR')),
  day: (iso) => `${+iso.slice(8, 10)} ${MONTHS[+iso.slice(5, 7) - 1]}`,
  dayLong: (iso) => new Date(iso + 'T12:00:00').toLocaleDateString('tr-TR', { day: 'numeric', month: 'long', year: 'numeric', weekday: 'short' }),
  pct: (x) => (x > 0 ? '+' : '') + Math.round(x * 100) + '%',
};

// ---------------------------------------------------------------------------
// İpucu (tooltip): tek öğe, tüm grafikler paylaşır
// ---------------------------------------------------------------------------
let tipEl;
export function showTip(evt, build) {
  tipEl = tipEl || document.getElementById('tooltip');
  tipEl.replaceChildren();
  build(tipEl);
  tipEl.hidden = false;
  const pad = 14, r = tipEl.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
  tipEl.style.left = Math.max(8, x) + 'px';
  tipEl.style.top = Math.max(8, y) + 'px';
}
export function hideTip() { if (tipEl) tipEl.hidden = true; }

function tipRow(parent, value, label, color) {
  parent.append(el('div', { class: 'tip-row' },
    color ? el('span', { class: 'tip-key', style: `background:${color}` }) : null,
    el('strong', { text: value }), el('span', { text: label })));
}

// Eksen: temiz adımlar (1-2-5 × 10ⁿ); dönen değer [adım, adım sayısı]
function niceScale(v, target = 4) {
  const raw = Math.max(v, 1e-9) / target;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw);
  return [step, Math.max(1, Math.ceil(v / step - 1e-9))];
}

// ---------------------------------------------------------------------------
// Zaman serisi: tek seri, çizgi + hafif alan, imleç çizgisi ve ipucu
// ---------------------------------------------------------------------------
export function lineChart(host, { days, values, label, color = 'var(--series-1)', unit = '', marks = [], height = 220, valueFmt = fmt.num }) {
  host.replaceChildren();
  host.classList.add('chart');
  const W = Math.max(320, host.clientWidth || 640), H = height;
  const m = { l: 44, r: 44, t: 14, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const n = days.length;
  const [step0, steps] = niceScale(Math.max(...values.map((v) => v ?? 0), 0) || 4);
  const max = step0 * steps;
  const x = (i) => m.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => m.t + ih - (v / max) * ih;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H, role: 'img', 'aria-label': `${label}: zaman serisi` });

  for (let k = 0; k <= steps; k++) {                  // ızgara + y ekseni
    const v = step0 * k, yy = y(v);
    root.append(svg('line', { x1: m.l, x2: W - m.r, y1: yy, y2: yy, class: k === 0 ? 'axisline' : 'gridline' }));
    root.append(svg('text', { x: m.l - 8, y: yy + 4, class: 'tick', 'text-anchor': 'end' }, valueFmt(v)));
  }
  const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 72))));      // x ekseni: seyrek tarih etiketleri
  for (let i = n - 1; i >= 0; i -= step) {
    root.append(svg('text', { x: x(i), y: H - 7, class: 'tick', 'text-anchor': n === 1 ? 'middle' : i === n - 1 ? 'end' : 'middle' }, fmt.day(days[i])));
  }

  const pts = values.map((v, i) => (v == null ? null : [x(i), y(v)]));
  const segs = [];                                    // boş günlerde çizgiyi kes
  let cur = [];
  pts.forEach((p) => { if (p) cur.push(p); else if (cur.length) { segs.push(cur); cur = []; } });
  if (cur.length) segs.push(cur);
  for (const s of segs) {
    if (s.length > 1) {
      const line = s.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('');
      root.append(svg('path', { d: `${line}L${s[s.length - 1][0].toFixed(1)},${y(0)}L${s[0][0].toFixed(1)},${y(0)}Z`, fill: color, opacity: 0.1 }));
      root.append(svg('path', { d: line, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    }
  }
  if (n <= 31) pts.forEach((p) => p && root.append(svg('circle', { cx: p[0], cy: p[1], r: 3, fill: color, class: 'dot' })));

  const markSet = new Map(marks.map((mk) => [mk.d, mk]));     // sıçrama günleri
  days.forEach((d, i) => {
    if (markSet.has(d) && pts[i]) root.append(svg('circle', { cx: pts[i][0], cy: pts[i][1], r: 6, class: 'spike-ring' }));
  });

  const lastI = values.reduce((acc, v, i) => (v == null ? acc : i), -1);       // uç nokta + doğrudan etiket
  if (lastI >= 0) {
    const [lx, ly] = pts[lastI];
    root.append(svg('circle', { cx: lx, cy: ly, r: 4.5, fill: color, class: 'dot' }));
    root.append(svg('text', { x: Math.min(lx + 9, W - 4), y: ly + 4, class: 'end-label' }, valueFmt(values[lastI])));
  }

  const cross = svg('line', { y1: m.t, y2: m.t + ih, class: 'crosshair', visibility: 'hidden' });
  const focus = svg('circle', { r: 5, fill: color, class: 'dot', visibility: 'hidden' });
  const hit = svg('rect', { x: m.l - 10, y: m.t, width: iw + 20, height: ih + m.b, fill: 'transparent', tabindex: 0, 'aria-label': `${label} — ok tuşlarıyla günler arasında gezinin` });
  root.append(cross, focus, hit);
  let active = -1;
  const show = (i, evt) => {
    active = i;
    const p = pts[i];
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
    if (p) { focus.setAttribute('cx', p[0]); focus.setAttribute('cy', p[1]); focus.setAttribute('visibility', 'visible'); }
    else focus.setAttribute('visibility', 'hidden');
    const box = root.getBoundingClientRect();
    const at = evt || { clientX: box.left + (x(i) / W) * box.width, clientY: box.top + 30 };
    showTip(at, (t) => {
      t.append(el('div', { class: 'tip-title', text: fmt.dayLong(days[i]) }));
      tipRow(t, values[i] == null ? '—' : valueFmt(values[i]) + unit, label, color);
      const mk = markSet.get(days[i]);
      if (mk) t.append(el('div', { class: 'tip-note', text: `▲ Olağandışı artış (beklenen ≈ ${fmt.num(mk.expected)})` }));
    });
  };
  const leave = () => { cross.setAttribute('visibility', 'hidden'); focus.setAttribute('visibility', 'hidden'); hideTip(); };
  hit.addEventListener('pointermove', (evt) => {
    const box = root.getBoundingClientRect();
    const px = ((evt.clientX - box.left) / box.width) * W;
    show(n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / iw) * (n - 1)))), evt);
  });
  hit.addEventListener('pointerleave', leave);
  hit.addEventListener('blur', leave);
  hit.addEventListener('keydown', (evt) => {
    if (evt.key === 'ArrowLeft' || evt.key === 'ArrowRight') {
      evt.preventDefault();
      show(Math.max(0, Math.min(n - 1, (active < 0 ? n - 1 : active) + (evt.key === 'ArrowRight' ? 1 : -1))));
    }
  });
  host.append(root);
}

// ---------------------------------------------------------------------------
// Mini eğilim çizgisi (kartlarda)
// ---------------------------------------------------------------------------
export function sparkline(values, { w = 132, h = 34 } = {}) {
  const root = svg('svg', { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: 'spark', 'aria-hidden': 'true' });
  if (!values.length) return root;
  const max = Math.max(1, ...values), n = values.length;
  const px = (i) => (n === 1 ? w - 4 : 3 + (i / (n - 1)) * (w - 7));
  const py = (v) => h - 4 - (v / max) * (h - 9);
  if (n > 1) root.append(svg('path', { d: values.map((v, i) => `${i ? 'L' : 'M'}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(''), fill: 'none', class: 'spark-line' }));
  root.append(svg('circle', { cx: px(n - 1), cy: py(values[n - 1]), r: 3, class: 'spark-dot' }));
  return root;
}

// ---------------------------------------------------------------------------
// Sıralı yatay çubuk listesi (aynı zamanda tablo görünümü): tek seri → tek renk
// ---------------------------------------------------------------------------
export function barList(host, rows, { valueFmt = fmt.num, onSelect, selected, limit = 12, emptyText = 'Veri yok', unit = '' } = {}) {
  host.replaceChildren();
  if (!rows.length) { host.append(el('p', { class: 'muted', text: emptyText })); return; }
  const max = Math.max(...rows.map((r) => r.value), 1e-9);
  let expanded = false;
  const list = el('div', { class: 'barlist', role: 'table' });
  const draw = () => {
    list.replaceChildren();
    for (const r of expanded ? rows : rows.slice(0, limit)) {
      const row = el(onSelect ? 'button' : 'div', {
        class: 'bar-row' + (selected === r.key ? ' is-selected' : ''), role: 'row', type: onSelect ? 'button' : null,
        title: r.title || null, onclick: onSelect ? () => onSelect(r.key) : null,
      },
        el('span', { class: 'bar-label', role: 'cell', text: r.label }),
        el('span', { class: 'bar-track', role: 'presentation' }, el('span', { class: 'bar-fill', style: `width:${Math.max(0.5, (r.value / max) * 100)}%` })),
        el('span', { class: 'bar-value', role: 'cell', text: valueFmt(r.value) + unit }),
        r.sub ? el('span', { class: 'bar-sub', role: 'cell', text: r.sub }) : null);
      list.append(row);
    }
  };
  draw();
  host.append(list);
  if (rows.length > limit) {
    const more = el('button', { class: 'link-btn', type: 'button', text: `Tümünü göster (${rows.length})`, onclick: () => {
      expanded = !expanded; more.textContent = expanded ? 'Daha az göster' : `Tümünü göster (${rows.length})`; draw();
    } });
    host.append(more);
  }
}

// ---------------------------------------------------------------------------
// İl haritası: tek renk tonlaması (açık → koyu = az → çok), ölçek göstergesi, ipucu, seçim
// ---------------------------------------------------------------------------
const CLASSES = 5;

export function breaks(values) {
  const v = values.filter((x) => x > 0).sort((a, b) => a - b);
  if (!v.length) return [];
  const uniq = [...new Set(v)];
  if (uniq.length <= CLASSES) return uniq.slice(1);
  const out = [];
  for (let k = 1; k < CLASSES; k++) {
    const q = v[Math.min(v.length - 1, Math.floor((k / CLASSES) * v.length))];
    if (q > v[0] && (!out.length || q > out[out.length - 1])) out.push(q);   // en küçük değerle aynı eşik boş sınıf üretir
  }
  return out;
}

export function choropleth(host, map, values, { valueFmt = fmt.num, unit = '', onSelect, selected, detail, noData = new Set(), legendTitle = '' } = {}) {
  host.replaceChildren();
  const br = breaks(Object.values(values));
  const cls = (v) => { let c = 0; while (c < br.length && v >= br[c]) c++; return c; };
  const root = svg('svg', { viewBox: `0 0 ${map.width} ${map.height}`, width: '100%', class: 'map', role: 'img', 'aria-label': `${legendTitle} — il haritası` });
  let hovered = null;
  for (const p of map.provinces) {
    const v = values[p.name] || 0;
    const missing = noData.has(p.name);
    const path = svg('path', {
      d: p.d, class: 'prov ' + (missing ? 'prov-na' : v > 0 ? `seq-${cls(v) + 1}` : 'prov-zero') + (selected === p.name ? ' is-selected' : ''),
      tabindex: 0, role: 'button', 'aria-label': `${p.name}: ${missing ? 'veri yok' : valueFmt(v) + unit}`,
    });
    const tip = (evt) => showTip(evt.clientX != null ? evt : { clientX: path.getBoundingClientRect().left, clientY: path.getBoundingClientRect().top }, (t) => {
      t.append(el('div', { class: 'tip-title', text: p.name }));
      tipRow(t, missing ? 'veri yok' : valueFmt(v) + unit, legendTitle);
      for (const [val, lab] of (detail ? detail(p.name) : [])) tipRow(t, val, lab);
    });
    path.addEventListener('pointermove', (evt) => { if (hovered !== path) { hovered = path; root.append(path); } tip(evt); });
    path.addEventListener('pointerleave', () => { hovered = null; hideTip(); });
    path.addEventListener('focus', tip);
    path.addEventListener('blur', hideTip);
    if (onSelect) {
      path.addEventListener('click', () => onSelect(p.name));
      path.addEventListener('keydown', (evt) => { if (evt.key === 'Enter' || evt.key === ' ') { evt.preventDefault(); onSelect(p.name); } });
    }
    root.append(path);
  }
  const sel = root.querySelector('.is-selected');
  if (sel) root.append(sel);
  host.append(root);

  const legend = el('div', { class: 'map-legend' }, el('span', { class: 'muted', text: legendTitle }));
  if (br.length || Object.values(values).some((x) => x > 0)) {
    const positive = Object.values(values).filter((x) => x > 0);
    const lo = Math.min(...positive);
    const whole = positive.every(Number.isInteger);         // sayım mı, oran mı?
    const edges = [lo, ...br];
    edges.forEach((e, i) => {
      const hi = edges[i + 1];
      let text;
      if (hi == null) text = `${valueFmt(e)}+`;
      else if (whole) text = hi - 1 > e ? `${valueFmt(e)}–${valueFmt(hi - 1)}` : valueFmt(e);
      else text = `${valueFmt(e)}–${valueFmt(hi)}`;
      legend.append(el('span', { class: 'legend-item' }, el('i', { class: `swatch seq-${i + 1}` }), text));
    });
  }
  legend.append(el('span', { class: 'legend-item' }, el('i', { class: 'swatch prov-zero' }), 'yok'));
  if (noData.size) legend.append(el('span', { class: 'legend-item' }, el('i', { class: 'swatch prov-na' }), 'yetersiz veri'));
  host.append(legend);
}

// ---------------------------------------------------------------------------
// CSV dışa aktarma
// ---------------------------------------------------------------------------
export function downloadCSV(filename, header, rows) {
  const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const text = '﻿' + [header, ...rows].map((r) => r.map(esc).join(';')).join('\r\n');
  const a = el('a', { href: URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' })), download: filename });
  document.body.append(a); a.click(); a.remove();
}
