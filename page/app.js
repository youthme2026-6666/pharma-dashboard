// 礼来财务看板：页面绘制。页面上的数字都来自附带的数据文件或 calcs.js 里的计算。
// 期间：季度（2026Q3）或年度（2025；当年未满四个季度时为年初至今），整页共用一个所选期间，参数名 q。
// 页面状态：更新页面时旧对象会被保留，所以逐项补齐，不能只在第一次创建时给默认值。
const L = (window.__lly = window.__lly || {});
if (L.hoverQ === undefined) L.hoverQ = null;
if (typeof L.openRev !== 'boolean') L.openRev = false;
if (!L.openArea || typeof L.openArea !== 'object') L.openArea = {};
if (!L.openBs || typeof L.openBs !== 'object') L.openBs = {};

const AREAS = ['Cardiometabolic Health', 'Oncology', 'Immunology', 'Neuroscience', 'Other'];
const AREA_ZH = { 'Cardiometabolic Health': '心血管代谢', Oncology: '肿瘤', Immunology: '免疫', Neuroscience: '神经科学', Other: '其他' };
const REGION_ZH = { 'U.S.': '美国', 'Outside U.S.': '美国以外', Europe: '欧洲', Japan: '日本', China: '中国', ROW: '其他地区', 'Total Revenue': '全球' };
const TYPE_ORDER = ['获批', '并购', '业绩说明', '定价与可及', '临床', '产能'];
const TYPE_SYMBOL = { 获批: d3.symbolCircle, 并购: d3.symbolDiamond, 业绩说明: d3.symbolSquare, 定价与可及: d3.symbolTriangle, 临床: d3.symbolStar, 产能: d3.symbolCross };
const DEFAULTS = { tv: 'area', tm: 'total', tg: 'q', ol: 'margin', cv: 'area', rg: 'Total Revenue', hm: 'rev', cmp: 'yoy', et: '全部', bsv: 'assets' };
const FLOW = { cost_of_sales: ['营业成本', 4], rnd: ['研发', 1], msa: ['销售及管理', 2], acquired_iprd: ['外购在研项目', 3], impairment_restructuring: ['减值与重组', 6], other_expense: ['其他净支出', 7], income_tax: ['所得税', 5], net_income: ['净利润', 0] };
const PRV_KEYS = [['volume', '销量', 0], ['price', '价格', 3], ['rate', '汇率', 6]];
// 热力图只列出最近 HEAT_WINDOW 个季度收入合计不少于 HEAT_MIN（百万美元）的产品。
const HEAT_WINDOW = 4;
const HEAT_MIN = 100;
const YTD_ZH = { 3: '第一季度', 6: '上半年', 9: '前三季度', 12: '全年' };
const STMT = [
  ['revenue', '营收', 'rev'],
  ['cost_of_sales', '营业成本', ''],
  ['gross_margin', '毛利', 'sum', 'ng_gross_margin'],
  ['rnd', '研发费用', ''],
  ['msa', '销售及管理费用', ''],
  ['acquired_iprd', '外购在研项目及里程碑', ''],
  ['impairment_restructuring', '资产减值、重组及其他特殊费用', ''],
  ['operating_income', '营业利润', 'sum', 'ng_operating_income'],
  ['other_net', '其他收支净额', ''],
  ['pretax_income', '税前利润', 'sum'],
  ['income_tax', '所得税', ''],
  ['net_income', '净利润', 'sum', 'ng_net_income'],
  ['eps_diluted', '稀释每股收益（美元）', 'sum', 'ng_eps_diluted'],
];
const GOOD_UP = new Set(['revenue', 'gross_margin', 'operating_income', 'pretax_income', 'net_income', 'eps_diluted']);
// 资产负债表分组：键、中文名、颜色序号（null 为弱化色）、不透明度
const BS_ASSETS = [
  ['cash_and_sti', '现金及短期投资', 0, 1], ['accounts_receivable', '应收账款', 1, 1], ['inventories', '存货', 2, 1], ['other_current_assets', '其他流动资产', null, 1],
  ['investments', '长期投资', 4, 1], ['goodwill_intangibles', '商誉及无形资产', 5, 1], ['ppe', '固定资产', 6, 1], ['other_noncurrent_assets', '其他非流动资产', null, 0.55],
];
const BS_CLAIMS = [
  ['short_term_debt', '短期借款', 3, 0.55], ['accounts_payable', '应付账款', 7, 1], ['sales_rebates', '应付销售返利', 1, 1], ['other_current_liabilities', '其他流动负债', null, 1],
  ['long_term_debt', '长期债务', 3, 1], ['other_noncurrent_liabilities', '其他非流动负债', null, 0.55], ['total_equity', '股东权益', 0, 1],
];
const BS_TREE = [
  { key: 'total_current_assets', label: '流动资产', kids: ['cash_and_sti', 'accounts_receivable', 'inventories', 'other_current_assets'] },
  { key: 'noncurrent_assets', label: '非流动资产', kids: ['investments', 'goodwill_intangibles', 'ppe', 'other_noncurrent_assets'] },
  { key: 'total_assets', label: '资产合计', sum: true },
  { key: 'total_current_liabilities', label: '流动负债', kids: ['short_term_debt', 'accounts_payable', 'sales_rebates', 'other_current_liabilities'] },
  { key: 'noncurrent_liabilities', label: '非流动负债', kids: ['long_term_debt', 'other_noncurrent_liabilities'] },
  { key: 'total_liabilities', label: '负债合计', sum: true },
  { key: 'total_equity', label: '股东权益', sum: true },
  { key: 'total_liabilities_equity', label: '负债和股东权益合计', sum: true },
];
const BS_LABEL = {
  cash_and_sti: '现金及短期投资', accounts_receivable: '应收账款', inventories: '存货', other_current_assets: '其他流动资产（含其他应收款、预付款）',
  investments: '长期投资', goodwill_intangibles: '商誉及其他无形资产', ppe: '固定资产（净额）', other_noncurrent_assets: '其他非流动资产（含递延所得税资产）',
  short_term_debt: '短期借款及一年内到期的长期债务', accounts_payable: '应付账款', sales_rebates: '应付销售返利及折扣', other_current_liabilities: '其他流动负债',
  long_term_debt: '长期债务', other_noncurrent_liabilities: '其他非流动负债（含退休福利、长期应交税费）',
};

// 数字格式：用 ASCII 负号，千分位用逗号。
const LOC = d3.formatLocale({ decimal: '.', thousands: ',', grouping: [3], currency: ['$', ''], minus: '-' });
const fYi = LOC.format(',.1f');
const fNum = LOC.format(',');
const fPct = LOC.format('.1%');
const fPct0 = LOC.format('.0%');
const fSPct = LOC.format('+.1%');
const fSPct0 = LOC.format('+.0%');
const fEps = LOC.format('.2f');
const fRatio = LOC.format('.2f');
const fSRatio = LOC.format('+.2f');
const fInt = LOC.format(',.0f');
// 百分点变化在计算里已经换算成百分点（保留一位小数），这里只加正负号。
const fPP = (v) => LOC.format('+.1f')(v);
const yiText = (m) => fYi(m / 100);
const signYi = (m) => (m >= 0 ? '+' : '') + fYi(m / 100);

const color = (i) => dash.colors[((i % dash.colors.length) + dash.colors.length) % dash.colors.length];
const areaColor = (a) => (AREAS.includes(a) ? color(AREAS.indexOf(a)) : 'var(--cds-chart-muted)');
const seriesColor = (name, rank) => (name === '其他产品' ? 'var(--cds-chart-muted)' : color(Number(rank) || 0));
const bsColor = (ci) => (ci == null ? 'var(--cds-chart-muted)' : color(ci));

const rowsOf = (id) => {
  const d = dash.data(id);
  return d.status === 'ok' && Array.isArray(d.data) ? d.data : null;
};
const param = (k) => {
  const v = dash.params()[k];
  return v == null || v === '' ? DEFAULTS[k] : v;
};

// ---------- 期间 ----------
const kpiRows = () => rowsOf('kpi') || [];
const allQ = () => kpiRows().filter((r) => r.kind === 'q').map((r) => r.period).sort();
const allY = () => kpiRows().filter((r) => r.kind === 'y').map((r) => r.period).sort();
const monthsOf = (y) => {
  const r = kpiRows().find((x) => x.kind === 'y' && x.period === y);
  return r ? Number(r.months) : 12;
};
const isYear = (p) => /^\d{4}$/.test(String(p || ''));
const latestQ = () => {
  const q = allQ();
  return q.length ? q[q.length - 1] : null;
};
const selPer = () => {
  const p = dash.params().q;
  return p && (allQ().includes(p) || allY().includes(p)) ? p : latestQ();
};
const viewPer = () => (L.hoverQ && allQ().includes(L.hoverQ) ? L.hoverQ : selPer());
const pickPer = (p) => dash.setParams({ q: p === latestQ() ? null : p });
const quartersIn = (p) => (isYear(p) ? allQ().filter((q) => q.startsWith(p)) : p ? [p] : []);
const inPer = (q, p) => (isYear(p) ? String(q).startsWith(p) : q === p);
const perName = (p) => {
  if (!p) return '—';
  if (isYear(p)) return `${p} 年${YTD_ZH[monthsOf(p)] || '全年'}`;
  return `${p.slice(0, 4)} 年第 ${p.slice(5)} 季度`;
};
const perTick = (p) => (isYear(p) ? (monthsOf(p) < 12 ? `${p} ${YTD_ZH[monthsOf(p)]}` : `${p} 全年`) : `${p.slice(2, 4)}Q${p.slice(5)}`);
const qTick = (q) => `${q.slice(2, 4)}Q${q.slice(5)}`;
const effCmp = () => (isYear(selPer()) ? 'yoy' : param('cmp'));

const enc = (v) => String(v).replace(/%/g, '%25').replace(/&/g, '%26').replace(/=/g, '%3D');
const whereOf = (o) => Object.entries(o).map(([k, v]) => `${enc(k)}=${enc(v)}`).join('&');
const markEl = (tag, source, field, where, text) => {
  const s = document.createElement(tag);
  s.dataset.source = source;
  s.dataset.field = field;
  s.dataset.where = where;
  s.textContent = text;
  return s;
};
const make = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
// 中英对照：独立网页会带上术语表（window.__GLOSSARY__，来自 agent/glossary.json）；没有术语表时（例如 Claude 看板快照）只显示中文。
const GLOSS = window.__GLOSSARY__ || null;
const enOf = (group, key) => (GLOSS && GLOSS[group] && GLOSS[group][key] && GLOSS[group][key].en) || '';
// 中文后面接一个灰色小号的英文术语（表格行名、图例、按钮这些 HTML 里用）
const withEn = (node, en) => {
  if (en) node.append(' ', make('span', 'ly-en', en));
  return node;
};
// 纯文字的场合（提示框标题和行名）用“中文 English”
const zhEn = (zh, en) => (en ? `${zh} ${en}` : zh);

// 加载与失败状态：图表留空并显示骨架，文字放回“—”，失败时显示一行提示。
const BAD = ['error', 'connect', 'declined', 'missing'];
const guard = (el, ids, mode) => {
  const st = ids.map((id) => dash.data(id).status);
  if (st.every((s) => s === 'ok')) {
    el.classList.remove('dash-skeleton');
    return true;
  }
  const loading = st.some((s) => s === 'loading');
  el.classList.toggle('dash-skeleton', loading && mode !== 'table');
  if (mode === 'chart' && !loading && st.some((s) => BAD.includes(s))) {
    const box = make('div', 'ly-failed');
    box.append(make('span', 'ly-failed-dot'), document.createTextNode('加载失败'));
    el.replaceChildren(box);
  } else if (mode === 'chart' || mode === 'table') el.replaceChildren();
  else el.replaceChildren(document.createTextNode('—'));
  return false;
};

// 图表小工具
const svgIn = (el, w, h) => d3.select(el).append('svg').attr('class', 'ly-svg').attr('width', '100%').attr('height', h).attr('viewBox', `0 0 ${w} ${h}`);
const textWidth = (svg, texts, cls) => {
  const g = svg.append('g').attr('class', cls).style('visibility', 'hidden');
  let m = 0;
  texts.forEach((t) => {
    const n = g.append('text').text(t).node();
    m = Math.max(m, n.getComputedTextLength());
  });
  g.remove();
  return m;
};
const canvasCtx = document.createElement('canvas').getContext('2d');
const textPx = (text, el) => {
  const cs = getComputedStyle(el);
  canvasCtx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  return canvasCtx.measureText(text).width;
};
const makeTip = (host) => {
  const t = make('div', 'ly-tip');
  t.hidden = true;
  host.append(t);
  return t;
};
const showTip = (tip, host, x, y, build) => {
  tip.replaceChildren();
  build(tip);
  tip.hidden = false;
  const hw = host.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = x + 14;
  if (left + tw > hw) left = Math.max(0, x - tw - 14);
  let top = y - th - 10;
  if (top < 0) top = Math.min(y + 16, Math.max(0, host.clientHeight - th));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
};
const tipHead = (tip, text) => tip.append(make('div', 'ly-tip-head', text));
const tipLine = (tip, sw, name, value) => {
  const r = make('div', 'ly-tip-row');
  if (sw) {
    const s = make('span', 'ly-sw');
    s.style.background = sw;
    r.append(s);
  } else r.append(make('span', 'ly-sw ly-sw-none'));
  r.append(make('span', 'ly-tip-name', name), make('span', 'ly-tip-val', value));
  tip.append(r);
};
const tipEvents = (tip, list) => {
  if (!list.length) return;
  tip.append(make('div', 'ly-tip-sub', '当季事件'));
  list.slice(0, 6).forEach((e) => tip.append(make('div', 'ly-tip-ev', `${e.date.slice(5)} · ${e.type} · ${e.title}`)));
  if (list.length > 6) tip.append(make('div', 'ly-tip-ev', `另有 ${list.length - 6} 条，见关键事件`));
};
const yearLabels = (g, periods, x, y) => {
  periods.forEach((p) => {
    if (!p.endsWith('Q1')) return;
    g.append('text').attr('x', x(p)).attr('y', y).attr('text-anchor', 'start').text(p.slice(0, 4));
  });
};
// 在按季度排列的横轴上标出所选期间（季度一格，年度多格）
const markPeriod = (g, x, per, y0, y1) => {
  const qs = quartersIn(per).filter((q) => x(q) != null);
  if (!qs.length) return;
  const off = (x.step() - x.bandwidth()) / 2;
  const x0 = x(qs[0]) - off, x1 = x(qs[qs.length - 1]) + x.bandwidth() + off;
  g.append('rect').attr('x', x0).attr('width', x1 - x0).attr('y', y0).attr('height', y1 - y0).style('fill', 'var(--color-fg)').attr('fill-opacity', 0.07).attr('rx', 3);
};
const refreshHover = () => {
  dash.draw('mixdonut');
  dash.draw('mixlist');
  dash.draw('flow');
  dash.draw('qtext');
};
const setHover = (q) => {
  if (L.hoverQ === q) return;
  L.hoverQ = q;
  refreshHover();
};

// ---------- 控件：只监听和设置按下状态 ----------
document.querySelectorAll('[data-param]').forEach((b) => {
  b.addEventListener('click', () => dash.setParams({ [b.dataset.param]: b.dataset.value }));
});
document.querySelectorAll('[data-param-check]').forEach((c) => {
  c.addEventListener('change', () => dash.setParams({ [c.dataset.paramCheck]: c.checked ? 'margin' : 'none' }));
});
document.querySelectorAll('.ly-latest').forEach((b) => b.addEventListener('click', () => dash.setParams({ q: null })));

const selP = () => {
  const rows = rowsOf('product_p');
  if (!rows) return null;
  const p = dash.params().p;
  if (p && rows.some((r) => r.product === p)) return p;
  const per = selPer();
  const top = rows.filter((r) => r.period === per).sort((a, b) => b.total - a.total)[0];
  return top ? top.product : null;
};

dash.onData(() => {
  const yearSel = isYear(selPer());
  document.querySelectorAll('[data-param]').forEach((b) => {
    const val = b.dataset.param === 'cmp' ? effCmp() : param(b.dataset.param);
    b.setAttribute('aria-pressed', String(val === b.dataset.value));
    if (b.dataset.param === 'cmp' && b.dataset.value === 'qoq') b.disabled = yearSel;
  });
  document.querySelectorAll('[data-param-check]').forEach((c) => {
    c.checked = param(c.dataset.paramCheck) === 'margin';
  });
  const q = dash.params().q;
  document.querySelectorAll('.ly-latest').forEach((b) => {
    b.hidden = !q || q === latestQ();
  });
  const ev = rowsOf('events'), p = selP();
  const has = !!(ev && p && ev.some((e) => e.product && e.product.split('、').includes(p)));
  document.querySelectorAll('.ly-pempty').forEach((n) => {
    n.hidden = !ev || !p || has;
  });
});

// ---------- 期间选择与文字 ----------
dash.draw('ysel', (sel) => {
  const ys = allY(), p = selPer();
  if (!ys.length || !p) {
    sel.replaceChildren();
    return;
  }
  sel.replaceChildren(...ys.slice().reverse().map((y) => new Option(`${y} 年`, y)));
  sel.value = p.slice(0, 4);
  sel.onchange = () => {
    const y = sel.value, cur = selPer();
    if (isYear(cur)) return pickPer(y);
    const qs = allQ().filter((q) => q.startsWith(y)), same = `${y}Q${cur.slice(5)}`;
    pickPer(qs.includes(same) ? same : qs[qs.length - 1]);
  };
});

dash.draw('psel', (sel) => {
  const p = selPer();
  if (!p) {
    sel.replaceChildren();
    return;
  }
  const y = p.slice(0, 4);
  sel.replaceChildren(new Option(YTD_ZH[monthsOf(y)] || '全年', y), ...allQ().filter((q) => q.startsWith(y)).map((q) => new Option(`第 ${q.slice(5)} 季度`, q)));
  sel.value = p;
  sel.onchange = () => pickPer(sel.value);
});

dash.draw('qtext', (el) => {
  if (!guard(el, ['kpi'])) return;
  const k = el.dataset.k, q = allQ();
  el.textContent = k === 'range' ? `${q[0]} 至 ${q[q.length - 1]}` : perName(k === 'view' ? viewPer() : selPer());
});

// ---------- 核心指标 ----------
const KFMT = { revenue_yi: fYi, net_income_yi: fYi, rnd_yi: fYi, acquired_iprd_yi: fYi, gross_margin: fPct, rnd_ratio: fPct, op_margin: fPct, eps: fEps, eps_ng: fEps };
const KGOOD = { revenue_yoy: 1, gross_margin_pp: 1, op_margin_pp: 1, net_income_yoy: 1, eps_yoy: 1 };
const kpiRow = (p) => kpiRows().find((r) => r.period === p);

dash.draw('kpinum', (el) => {
  if (!guard(el, ['kpi'])) return;
  const k = el.dataset.k, p = selPer(), row = kpiRow(p), v = row ? row[k] : null;
  if (v == null) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  el.replaceChildren(markEl('span', 'kpi', k, whereOf({ period: p }), (KFMT[k] || fYi)(Number(v))));
});

dash.draw('kpidelta', (el) => {
  el.classList.remove('ly-up', 'ly-down');
  if (!guard(el, ['kpi'])) return;
  const k = el.dataset.k, p = selPer(), row = kpiRow(p), v = row ? row[k] : null;
  if (v == null) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  const n = Number(v);
  if (KGOOD[k] && n !== 0) el.classList.add(n > 0 ? 'ly-up' : 'ly-down');
  el.replaceChildren(markEl('span', 'kpi', k, whereOf({ period: p }), k.endsWith('_pp') ? fPP(n) : fSPct(n)));
});

dash.draw('mixnum', (el) => {
  if (!guard(el, ['mix_p'])) return;
  const k = el.dataset.k, p = selPer(), row = rowsOf('mix_p').find((r) => r.period === p), v = row ? row[k] : null;
  if (v == null) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  el.replaceChildren(markEl('span', 'mix_p', k, whereOf({ period: p }), k.endsWith('_share') ? fPct(Number(v)) : fYi(Number(v))));
});

let gradSeq = 0;
const sparkline = (el, rows, k, endIndex, colorValue) => {
  const win = rows.slice(Math.max(0, endIndex - 11), endIndex + 1).filter((r) => r[k] != null);
  el.replaceChildren();
  if (win.length < 2) return;
  const w = Math.max(80, el.clientWidth || 160), h = 36, pad = 3;
  const x = d3.scaleLinear([0, win.length - 1], [pad, w - pad]);
  const ext = d3.extent(win, (r) => Number(r[k]));
  const y = d3.scaleLinear(ext[0] === ext[1] ? [ext[0] - 1, ext[1] + 1] : ext, [h - pad, pad]);
  const svg = svgIn(el, w, h).attr('aria-hidden', 'true');
  const gid = `ly-grad-${(gradSeq += 1)}`;
  const grad = svg.append('defs').append('linearGradient').attr('id', gid).attr('x1', 0).attr('x2', 0).attr('y1', 0).attr('y2', 1);
  [[0, 0.26], [0.25, 0.16], [0.5, 0.08], [0.75, 0.03], [1, 0]].forEach(([o, a]) => grad.append('stop').attr('offset', o).attr('style', `stop-color: ${colorValue}; stop-opacity: ${a}`));
  svg.append('path').attr('d', d3.area().x((d, j) => x(j)).y0(h).y1((d) => y(Number(d[k])))(win)).style('fill', `url(#${gid})`);
  svg.append('path').attr('d', d3.line().x((d, j) => x(j)).y((d) => y(Number(d[k])))(win)).style('fill', 'none').style('stroke', colorValue).attr('stroke-width', 1.75).attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round');
  svg.append('circle').attr('cx', x(win.length - 1)).attr('cy', y(Number(win[win.length - 1][k]))).attr('r', 2.75).style('fill', colorValue);
};
dash.draw('spark', (el) => {
  if (!guard(el, ['kpi'], 'chart')) return;
  const p = selPer(), kind = isYear(p) ? 'y' : 'q';
  const rows = kpiRows().filter((r) => r.kind === kind).sort((a, b) => (a.period < b.period ? -1 : 1));
  sparkline(el, rows, el.dataset.k, rows.findIndex((r) => r.period === p), color(0));
});

// ---------- 收入趋势 ----------
const trendSetup = () => {
  const byArea = param('tv') === 'area', yearly = param('tg') === 'y';
  return { byArea, yearly, src: byArea ? 'area_p' : 'series_p', sk: byArea ? 'area' : 'series', kind: yearly ? 'y' : 'q' };
};
const trendKeys = (raw, byArea) => {
  if (byArea) return AREAS.filter((a) => raw.some((r) => r.area === a));
  const rank = new Map(raw.map((r) => [r.series, Number(r.rank)]));
  return [...rank.entries()].sort((a, b) => a[1] - b[1]).map((e) => e[0]);
};

dash.draw('trendlegend', (el) => {
  const { byArea, yearly, src, kind } = trendSetup();
  if (!guard(el, [src], 'chart')) return;
  const raw = rowsOf(src).filter((r) => r.kind === kind), keys = trendKeys(raw, byArea);
  const rank = new Map(raw.map((r) => [r.series, r.rank]));
  const items = keys.map((k) => {
    const it = make('span', 'ly-leg');
    const sw = make('span', 'ly-sw');
    sw.style.background = byArea ? areaColor(k) : seriesColor(k, rank.get(k));
    it.append(sw, document.createTextNode(byArea ? AREA_ZH[k] || k : k));
    if (byArea) withEn(it, enOf('areas', k));
    return it;
  });
  if (!yearly && param('ol') === 'margin') {
    const it = make('span', 'ly-leg');
    it.append(make('span', 'ly-sw ly-sw-line'), document.createTextNode('营业利润率（右轴）'));
    items.push(it);
  }
  if (yearly && raw.some((r) => Number(r.quarters) < 4)) items.push(make('span', 'ly-leg ly-leg-note', '* 当年未满四个季度'));
  el.replaceChildren(...items);
});

dash.draw('trend', (el) => {
  const { byArea, yearly, src, sk, kind } = trendSetup();
  const tm = param('tm'), withLine = !yearly && param('ol') === 'margin';
  if (!guard(el, [src, 'kpi', 'events'], 'chart')) return;
  const raw = rowsOf(src).filter((r) => r.kind === kind), kpi = kpiRows(), events = rowsOf('events');
  const vk = tm === 'delta' ? 'delta' : 'total';
  const keys = trendKeys(raw, byArea);
  const rank = new Map(raw.map((r) => [r.series, r.rank]));
  const colorOf = (k) => (byArea ? areaColor(k) : seriesColor(k, rank.get(k)));
  const nameOf = (k) => (byArea ? AREA_ZH[k] || k : k);
  const periods = [...new Set(raw.map((r) => r.period))].sort();
  let table = periods.map((p) => {
    const o = { period: p, n: 1 };
    keys.forEach((k) => {
      o[k] = null;
    });
    raw.filter((r) => r.period === p).forEach((r) => {
      o[r[sk]] = r[vk] == null ? null : Number(r[vk]);
      o.n = Number(r.quarters);
    });
    return o;
  });
  if (tm === 'delta') table = table.filter((o) => keys.some((k) => o[k] != null));
  el.replaceChildren();
  if (!table.length) return;

  const W = Math.max(320, el.clientWidth || 640), H = 300, bottom = H - 26, topY = 28;
  const layers = d3.stack().keys(keys).value((d, k) => (d[k] == null ? 0 : d[k] / 100)).offset(tm === 'delta' ? d3.stackOffsetDiverging : d3.stackOffsetNone)(table);
  let lo = d3.min(layers, (s) => d3.min(s, (d) => Math.min(d[0], d[1])));
  let hi = d3.max(layers, (s) => d3.max(s, (d) => Math.max(d[0], d[1])));
  lo = Math.min(0, lo || 0);
  hi = Math.max(0, hi || 0);
  if (hi === lo) hi = lo + 1;
  const svg = svgIn(el, W, H);
  const y = d3.scaleLinear([lo, hi], [bottom, topY]).nice(5);
  const ticks = y.ticks(5);
  const leftW = Math.ceil(textWidth(svg, ticks.map((t) => fInt(t)), 'ly-tick')) + 12;
  const margin = new Map(kpi.filter((r) => r.kind === 'q').map((r) => [r.period, r.op_margin == null ? null : Number(r.op_margin)]));
  const mVals = table.map((o) => margin.get(o.period)).filter((v) => v != null);
  const yM = d3.scaleLinear([Math.min(0, d3.min(mVals) ?? 0), Math.max(0.1, d3.max(mVals) ?? 0.1)], [bottom, topY]).nice(4);
  const rightW = withLine ? Math.ceil(textWidth(svg, yM.ticks(4).map((t) => fPct0(t)), 'ly-tick')) + 12 : 8;
  const x = d3.scaleBand(table.map((o) => o.period), [leftW, W - rightW]).paddingInner(0.22).paddingOuter(0.12);
  const step = x.step(), off = (step - x.bandwidth()) / 2;

  const g = svg.append('g');
  g.append('g').selectAll('line').data(ticks).join('line').attr('x1', leftW).attr('x2', W - rightW).attr('y1', (t) => y(t)).attr('y2', (t) => y(t)).style('stroke', 'var(--cds-chart-grid)').attr('stroke-width', 1);
  g.append('g').attr('class', 'ly-tick').selectAll('text').data(ticks).join('text').attr('x', leftW - 8).attr('y', (t) => y(t)).attr('dy', '0.32em').attr('text-anchor', 'end').text((t) => fInt(t));
  g.append('text').attr('class', 'ly-tick').attr('x', 0).attr('y', 10).attr('text-anchor', 'start').text('亿美元');

  const per = selPer();
  if (yearly) {
    const yp = per.slice(0, 4);
    if (x(yp) != null) g.append('rect').attr('x', x(yp) - off).attr('width', step).attr('y', topY - 6).attr('height', bottom - topY + 6).style('fill', 'var(--color-fg)').attr('fill-opacity', 0.07).attr('rx', 3);
  } else markPeriod(g, x, per, topY - 6, bottom);
  const hoverBand = g.append('rect').attr('y', topY - 6).attr('height', bottom - topY + 6).attr('width', step).style('fill', 'var(--color-fg)').attr('fill-opacity', 0).attr('rx', 3).attr('pointer-events', 'none');

  layers.forEach((layer) => {
    g.append('g').style('fill', colorOf(layer.key)).selectAll('rect').data(layer).join('rect')
      .attr('x', (d) => x(d.data.period)).attr('width', x.bandwidth())
      .attr('y', (d) => y(Math.max(d[0], d[1]))).attr('height', (d) => Math.abs(y(d[0]) - y(d[1])));
  });
  if (tm === 'delta') {
    g.append('line').attr('x1', leftW).attr('x2', W - rightW).attr('y1', y(0)).attr('y2', y(0)).style('stroke', 'var(--cds-chart-axis)').attr('stroke-width', 1);
    g.append('g').selectAll('line').data(table).join('line')
      .attr('x1', (o) => x(o.period) - 2).attr('x2', (o) => x(o.period) + x.bandwidth() + 2)
      .attr('y1', (o) => y(d3.sum(keys, (k) => o[k] || 0) / 100)).attr('y2', (o) => y(d3.sum(keys, (k) => o[k] || 0) / 100))
      .style('stroke', 'var(--color-fg)').attr('stroke-width', 2).attr('stroke-linecap', 'round');
  }
  if (withLine && mVals.length) {
    const pts = table.map((o) => ({ p: o.period, v: margin.get(o.period) })).filter((d) => d.v != null);
    const cx = (d) => x(d.p) + x.bandwidth() / 2;
    g.append('path').attr('d', d3.line().x(cx).y((d) => yM(d.v))(pts)).style('fill', 'none').style('stroke', 'var(--color-fg)').attr('stroke-opacity', 0.75).attr('stroke-width', 2).attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round');
    g.append('g').selectAll('circle').data(pts).join('circle').attr('cx', cx).attr('cy', (d) => yM(d.v)).attr('r', 2.4).style('fill', 'var(--color-fg)').attr('fill-opacity', 0.8);
    g.append('g').attr('class', 'ly-tick').selectAll('text').data(yM.ticks(4)).join('text').attr('x', W - rightW + 8).attr('y', (t) => yM(t)).attr('dy', '0.32em').text((t) => fPct0(t));
  }

  // 横轴：放得下就逐季标注，否则只在每年第一季度标年份
  const xl = g.append('g').attr('class', 'ly-tick');
  if (yearly) table.forEach((o) => xl.append('text').attr('x', x(o.period) + x.bandwidth() / 2).attr('y', H - 8).attr('text-anchor', 'middle').text(o.n < 4 ? `${o.period}*` : o.period));
  else {
    const lw = textWidth(svg, table.map((o) => qTick(o.period)), 'ly-tick');
    if (lw + 6 <= step) table.forEach((o) => xl.append('text').attr('x', x(o.period) + x.bandwidth() / 2).attr('y', H - 8).attr('text-anchor', 'middle').text(qTick(o.period)));
    else yearLabels(xl, table.map((o) => o.period), x, H - 8);
  }

  // 事件标记：每季最多三种类型
  const evBy = d3.group(events.filter((e) => !yearly && x(e.quarter) != null), (e) => e.quarter);
  const lane = g.append('g').style('fill', 'var(--color-fg)').attr('fill-opacity', 0.72);
  for (const [q, list] of evBy) {
    const types = [...new Set(list.map((e) => e.type))].sort((a, b) => TYPE_ORDER.indexOf(a) - TYPE_ORDER.indexOf(b)).slice(0, 3);
    types.forEach((t, i) => lane.append('path').attr('d', d3.symbol(TYPE_SYMBOL[t] || d3.symbolCircle, 26)()).attr('transform', `translate(${x(q) + x.bandwidth() / 2},${topY - 12 - i * 7})`));
  }

  const tip = makeTip(el);
  g.append('g').selectAll('rect').data(table).join('rect')
    .attr('x', (o) => x(o.period) - off).attr('width', step).attr('y', 0).attr('height', bottom)
    .attr('fill', 'transparent').style('cursor', 'pointer')
    .on('pointerenter pointermove', (event, o) => {
      hoverBand.attr('x', x(o.period) - off).attr('fill-opacity', 0.05);
      const [px, py] = d3.pointer(event, el);
      showTip(tip, el, px, py, (t) => {
        tipHead(t, yearly ? `${o.period} 年${o.n < 4 ? `（前 ${o.n} 个季度）` : ''}` : perName(o.period));
        keys.slice().reverse().forEach((k) => {
          if (o[k] == null || o[k] === 0) return;
          tipLine(t, colorOf(k), nameOf(k), tm === 'delta' ? `${signYi(o[k])} 亿` : `${yiText(o[k])} 亿`);
        });
        const sum = d3.sum(keys, (k) => o[k] || 0);
        tipLine(t, null, tm === 'delta' ? '合计变化' : '合计', tm === 'delta' ? `${signYi(sum)} 亿美元` : `${yiText(sum)} 亿美元`);
        if (withLine && margin.get(o.period) != null) tipLine(t, null, '营业利润率', fPct(margin.get(o.period)));
        if (!yearly) tipEvents(t, events.filter((e) => e.quarter === o.period));
      });
      if (!yearly) setHover(o.period);
    })
    .on('click', (event, o) => pickPer(o.period));
  svg.on('pointerleave', () => {
    tip.hidden = true;
    hoverBand.attr('fill-opacity', 0);
    setHover(null);
  });
});

// ---------- 收入构成 ----------
const mixSource = () => (param('cv') === 'region' ? 'region_p' : param('cv') === 'product' ? 'series_p' : 'area_p');
const mixItems = () => {
  const cv = param('cv'), rows = rowsOf(mixSource());
  if (!rows) return null;
  const here = rows.filter((r) => r.period === viewPer());
  if (cv === 'area') return AREAS.map((a) => here.find((r) => r.area === a)).filter(Boolean).map((r) => ({ key: r.area, name: AREA_ZH[r.area] || r.area, en: enOf('areas', r.area), value: Number(r.total), share: Number(r.share), change: r.yoy == null ? null : Number(r.yoy), color: areaColor(r.area) }));
  if (cv === 'product') return here.slice().sort((a, b) => a.rank - b.rank).filter((r) => Number(r.total) !== 0).map((r) => ({ key: r.series, name: r.series, value: Number(r.total), share: Number(r.share), change: r.yoy == null ? null : Number(r.yoy), color: seriesColor(r.series, r.rank), product: r.series !== '其他产品' }));
  return here.map((r, i) => ({ key: r.region, name: REGION_ZH[r.region] || r.region, en: enOf('regions', r.region), value: Number(r.revenue), share: Number(r.share), change: r.total_change == null ? null : Number(r.total_change), color: color(i) }));
};
const highlightMix = (key) => {
  document.querySelectorAll('.ly-donut path[data-key]').forEach((p) => p.classList.toggle('ly-dim', key != null && p.dataset.key !== key));
  document.querySelectorAll('.ly-mixrow[data-key]').forEach((r) => r.classList.toggle('ly-dim', key != null && r.dataset.key !== key));
};

dash.draw('mixdonut', (el) => {
  if (!guard(el, [mixSource(), 'kpi'], 'chart')) return;
  const items = mixItems(), p = viewPer(), k = kpiRow(p);
  el.replaceChildren();
  if (!items || !items.length) return;
  const size = 200, R = size / 2;
  const svg = svgIn(el, size, size).attr('height', null).attr('aria-label', `${perName(p)}收入构成`);
  const arc = d3.arc().innerRadius(R * 0.64).outerRadius(R - 2).padAngle(0.012).cornerRadius(2);
  const pie = d3.pie().value((d) => Math.max(0, d.value)).sort(null);
  const gd = svg.append('g').attr('transform', `translate(${R},${R})`);
  gd.selectAll('path').data(pie(items)).join('path').attr('d', arc).attr('data-key', (d) => d.data.key).style('fill', (d) => d.data.color)
    .on('pointerenter', (event, d) => highlightMix(d.data.key)).on('pointerleave', () => highlightMix(null));
  gd.append('text').attr('class', 'ly-donut-num').attr('text-anchor', 'middle').attr('y', -4).text(k ? fYi(Number(k.revenue_yi)) : '—');
  gd.append('text').attr('class', 'ly-donut-cap').attr('text-anchor', 'middle').attr('y', 16).text('亿美元');
  gd.append('text').attr('class', 'ly-donut-cap').attr('text-anchor', 'middle').attr('y', 32).text(perTick(p));
});

dash.draw('mixlist', (el) => {
  if (!guard(el, [mixSource()], 'chart')) return;
  const items = mixItems();
  el.replaceChildren();
  if (!items) return;
  items.forEach((it) => {
    const row = make(it.product ? 'button' : 'div', 'ly-mixrow');
    row.dataset.key = it.key;
    if (it.product) {
      row.type = 'button';
      row.addEventListener('click', () => dash.setParams({ p: it.key }));
    }
    const sw = make('span', 'ly-sw');
    sw.style.background = it.color;
    const chg = make('span', `ly-num${it.change == null ? '' : it.change >= 0 ? ' ly-up' : ' ly-down'}`, it.change == null ? '—' : fSPct(it.change));
    row.append(sw, withEn(make('span', 'ly-mixname', it.name), it.en), make('span', 'ly-num', yiText(it.value)), make('span', 'ly-num', fPct(it.share)), chg);
    row.addEventListener('pointerenter', () => highlightMix(it.key));
    row.addEventListener('pointerleave', () => highlightMix(null));
    el.append(row);
  });
});

// ---------- 营收去向 ----------
dash.draw('flow', (el) => {
  if (!guard(el, ['cost_flow'], 'chart')) return;
  const p = viewPer();
  const rows = rowsOf('cost_flow').filter((r) => r.period === p).sort((a, b) => a.order - b.order);
  el.replaceChildren();
  const rev = rows.find((r) => r.item === 'revenue');
  if (!rev) return;
  const revenue = Number(rev.amount);
  const outs = rows.filter((r) => FLOW[r.item] && Number(r.amount) > 0);
  const inflow = rows.find((r) => r.item === 'other_income' && Number(r.amount) > 0);
  const net = rows.find((r) => r.item === 'net_income');
  const totalOut = d3.sum(outs, (r) => Number(r.amount));
  const scale = Math.max(revenue, totalOut);
  const bar = make('div', 'ly-flowbar');
  el.append(bar);
  const barW = bar.clientWidth || el.clientWidth || 600;
  const tip = makeTip(el);
  const segs = new Map(), legRows = new Map();
  const highlightFlow = (item) => {
    segs.forEach((s, k) => s.classList.toggle('ly-dim', item != null && k !== item));
    legRows.forEach((s, k) => s.classList.toggle('ly-dim', item != null && k !== item));
  };
  outs.forEach((r) => {
    const seg = make('div', 'ly-flowseg');
    const frac = Number(r.amount) / scale, label = FLOW[r.item][0];
    seg.style.width = `${frac * 100}%`;
    seg.style.background = color(FLOW[r.item][1]);
    if (barW * frac >= textPx(label, bar) + 12) seg.textContent = label;
    seg.addEventListener('pointermove', (event) => {
      const [px, py] = d3.pointer(event, el);
      showTip(tip, el, px, py, (t) => {
        tipHead(t, `${label} · ${perName(p)}`);
        tipLine(t, null, '金额', `${yiText(Number(r.amount))} 亿美元`);
        tipLine(t, null, '占营收', fPct(Number(r.share)));
      });
      highlightFlow(r.item);
    });
    seg.addEventListener('pointerleave', () => {
      tip.hidden = true;
      highlightFlow(null);
    });
    segs.set(r.item, seg);
    bar.append(seg);
  });
  if (totalOut > revenue + 0.5) {
    const over = make('div', 'ly-flowover');
    over.style.left = `${(revenue / scale) * 100}%`;
    over.style.width = `${((totalOut - revenue) / scale) * 100}%`;
    const tick = make('div', 'ly-flowtick');
    tick.style.left = `${(revenue / scale) * 100}%`;
    bar.append(over, tick);
  }
  const legend = make('div', 'ly-flowlegend');
  outs.forEach((r) => {
    const row = make('div', 'ly-flowrow');
    const sw = make('span', 'ly-sw');
    sw.style.background = color(FLOW[r.item][1]);
    row.append(sw, withEn(make('span', 'ly-flowname', FLOW[r.item][0]), enOf('terms', r.item)), make('span', 'ly-num', `${yiText(Number(r.amount))} 亿`), make('span', 'ly-num ly-flowper', fPct(Number(r.share))));
    row.addEventListener('pointerenter', () => highlightFlow(r.item));
    row.addEventListener('pointerleave', () => highlightFlow(null));
    legRows.set(r.item, row);
    legend.append(row);
  });
  el.append(legend);
  const notes = [];
  if (net && Number(net.amount) < 0) notes.push(`当期净亏损 ${yiText(-Number(net.amount))} 亿美元：各项支出合计超过营收，竖线右侧的斜纹部分即超出的金额。`);
  if (inflow) notes.push(`当期其他收支为净收益 ${yiText(Number(inflow.amount))} 亿美元，所以各项去向合计略高于营收（竖线右侧的斜纹部分）。`);
  if (notes.length) el.append(make('p', 'ly-flownote', notes.join(' ')));
});

// ---------- 资产负债表 ----------
const BS_KFMT = { total_assets_yi: fYi, cash_yi: fYi, debt_yi: fYi, net_debt_yi: fYi, equity_yi: fYi, liab_ratio: fPct, current_ratio: fRatio };
const bsKpiRow = (p) => (rowsOf('bs_kpi') || []).find((r) => r.period === p);
dash.draw('bsnum', (el) => {
  if (!guard(el, ['bs_kpi'])) return;
  const k = el.dataset.k, p = selPer(), row = bsKpiRow(p), v = row ? row[k] : null;
  if (v == null) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  el.replaceChildren(markEl('span', 'bs_kpi', k, whereOf({ period: p }), (BS_KFMT[k] || fYi)(Number(v))));
});
dash.draw('bsdelta', (el) => {
  if (!guard(el, ['bs_kpi'])) return;
  const k = el.dataset.k, p = selPer(), row = bsKpiRow(p), v = row ? row[k] : null;
  if (v == null) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  const n = Number(v);
  el.replaceChildren(markEl('span', 'bs_kpi', k, whereOf({ period: p }), k.endsWith('_pp') ? fPP(n) : k.endsWith('_chg') ? fSRatio(n) : fSPct(n)));
});
dash.draw('bsasof', (el) => {
  if (!guard(el, ['bs_kpi'])) return;
  const p = selPer(), row = bsKpiRow(p);
  el.replaceChildren(row ? markEl('span', 'bs_kpi', 'as_of', whereOf({ period: p }), row.as_of) : document.createTextNode('—'));
});

const bsValues = (p) => new Map((rowsOf('bs_lines') || []).filter((r) => r.period === p).map((r) => [r.line, r]));
dash.draw('bsbars', (el) => {
  if (!guard(el, ['bs_lines'], 'chart')) return;
  const p = selPer(), vals = bsValues(p);
  el.replaceChildren();
  const ta = vals.get('total_assets');
  if (!ta) return;
  const total = Number(ta.value);
  const tip = makeTip(el);
  const segMap = new Map(), legMap = new Map();
  const hl = (key) => {
    segMap.forEach((s, k) => s.classList.toggle('ly-dim', key != null && k !== key));
    legMap.forEach((s, k) => s.classList.toggle('ly-dim', key != null && k !== key));
  };
  const drawBar = (title, defs) => {
    const row = make('div', 'ly-bsrow');
    row.append(make('span', 'ly-bslabel', title));
    const bar = make('div', 'ly-flowbar ly-bsbar');
    row.append(bar);
    el.append(row);
    const barW = bar.clientWidth || el.clientWidth || 600;
    defs.forEach(([key, label, ci, alpha]) => {
      const r = vals.get(key);
      if (!r || Number(r.value) <= 0) return;
      const frac = Number(r.value) / total;
      const seg = make('div', 'ly-flowseg');
      seg.style.width = `${frac * 100}%`;
      seg.style.background = bsColor(ci);
      seg.style.opacity = String(alpha);
      if (barW * frac >= textPx(label, bar) + 12) seg.textContent = label;
      seg.addEventListener('pointermove', (event) => {
        const [px, py] = d3.pointer(event, el);
        showTip(tip, el, px, py, (t) => {
          tipHead(t, `${zhEn(BS_LABEL[key] || label, enOf('terms', key))} · ${r.as_of}`);
          tipLine(t, null, '金额', `${yiText(Number(r.value))} 亿美元`);
          tipLine(t, null, '占总资产', fPct(Number(r.pct_assets)));
          tipLine(t, null, '比去年同期', r.yoy == null ? '—' : fSPct(Number(r.yoy)));
        });
        hl(key);
      });
      seg.addEventListener('pointerleave', () => {
        tip.hidden = true;
        hl(null);
      });
      segMap.set(key, seg);
      bar.append(seg);
    });
  };
  drawBar('资产', BS_ASSETS);
  drawBar('负债与权益', BS_CLAIMS);
  const legend = make('div', 'ly-bslegend');
  [['资产', BS_ASSETS], ['负债与权益', BS_CLAIMS]].forEach(([title, defs]) => {
    const col = make('div', 'ly-bscol');
    col.append(withEn(make('p', 'ly-bscol-title', title), title === '资产' ? 'Assets' : 'Liabilities and equity'));
    defs.forEach(([key, label, ci, alpha]) => {
      const r = vals.get(key);
      if (!r) return;
      const line = make('div', 'ly-flowrow');
      const sw = make('span', 'ly-sw');
      sw.style.background = bsColor(ci);
      sw.style.opacity = String(alpha);
      line.append(sw, withEn(make('span', 'ly-flowname', label), enOf('terms', key)), make('span', 'ly-num', `${yiText(Number(r.value))} 亿`), make('span', 'ly-num ly-flowper', fPct(Number(r.pct_assets))));
      line.addEventListener('pointerenter', () => hl(key));
      line.addEventListener('pointerleave', () => hl(null));
      legMap.set(key, line);
      col.append(line);
    });
    legend.append(col);
  });
  el.append(legend);
});

dash.draw('bstrend', (el) => {
  if (!guard(el, ['bs_lines', 'bs_kpi'], 'chart')) return;
  const defs = param('bsv') === 'claims' ? BS_CLAIMS : BS_ASSETS;
  const rows = rowsOf('bs_lines').filter((r) => r.kind === 'q');
  const quarters = [...new Set(rows.map((r) => r.period))].sort();
  const by = new Map(rows.map((r) => [`${r.period}|${r.line}`, Number(r.value)]));
  const ratio = new Map(rowsOf('bs_kpi').filter((r) => r.kind === 'q').map((r) => [r.period, Number(r.liab_ratio)]));
  el.replaceChildren();
  if (!quarters.length) return;
  const table = quarters.map((q) => {
    const o = { period: q };
    defs.forEach(([key]) => {
      o[key] = Math.max(0, by.get(`${q}|${key}`) || 0) / 100;
    });
    return o;
  });
  const W = Math.max(320, el.clientWidth || 600), H = 260, bottom = H - 24, topY = 14;
  const layers = d3.stack().keys(defs.map((d) => d[0]))(table);
  const svg = svgIn(el, W, H);
  const y = d3.scaleLinear([0, d3.max(layers, (s) => d3.max(s, (d) => d[1])) || 1], [bottom, topY]).nice(5);
  const ticks = y.ticks(5);
  const leftW = Math.ceil(textWidth(svg, ticks.map((t) => fInt(t)), 'ly-tick')) + 12;
  const yR = d3.scaleLinear([0, 1], [bottom, topY]);
  const rightW = Math.ceil(textWidth(svg, ['100%'], 'ly-tick')) + 12;
  const x = d3.scaleBand(quarters, [leftW, W - rightW]).paddingInner(0.22).paddingOuter(0.1);
  const step = x.step(), off = (step - x.bandwidth()) / 2;
  const g = svg.append('g');
  g.append('g').selectAll('line').data(ticks).join('line').attr('x1', leftW).attr('x2', W - rightW).attr('y1', (t) => y(t)).attr('y2', (t) => y(t)).style('stroke', 'var(--cds-chart-grid)');
  g.append('g').attr('class', 'ly-tick').selectAll('text').data(ticks).join('text').attr('x', leftW - 8).attr('y', (t) => y(t)).attr('dy', '0.32em').attr('text-anchor', 'end').text((t) => fInt(t));
  markPeriod(g, x, selPer(), topY - 4, bottom);
  layers.forEach((layer) => {
    const def = defs.find((d) => d[0] === layer.key);
    g.append('g').style('fill', bsColor(def[2])).attr('fill-opacity', def[3]).selectAll('rect').data(layer).join('rect')
      .attr('x', (d) => x(d.data.period)).attr('width', x.bandwidth()).attr('y', (d) => y(d[1])).attr('height', (d) => Math.max(0, y(d[0]) - y(d[1])));
  });
  const pts = quarters.map((q) => ({ q, v: ratio.get(q) })).filter((d) => d.v != null);
  const cx = (d) => x(d.q) + x.bandwidth() / 2;
  g.append('path').attr('d', d3.line().x(cx).y((d) => yR(d.v))(pts)).style('fill', 'none').style('stroke', 'var(--color-fg)').attr('stroke-opacity', 0.75).attr('stroke-width', 2).attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round');
  g.append('g').attr('class', 'ly-tick').selectAll('text').data([0, 0.5, 1]).join('text').attr('x', W - rightW + 8).attr('y', (t) => yR(t)).attr('dy', '0.32em').text((t) => fPct0(t));
  yearLabels(g.append('g').attr('class', 'ly-tick'), quarters, x, H - 6);
  const tip = makeTip(el);
  const hoverBand = g.append('rect').attr('y', topY - 4).attr('height', bottom - topY + 4).attr('width', step).style('fill', 'var(--color-fg)').attr('fill-opacity', 0).attr('rx', 3).attr('pointer-events', 'none');
  g.append('g').selectAll('rect').data(table).join('rect').attr('x', (o) => x(o.period) - off).attr('width', step).attr('y', 0).attr('height', bottom).attr('fill', 'transparent').style('cursor', 'pointer')
    .on('pointerenter pointermove', (event, o) => {
      hoverBand.attr('x', x(o.period) - off).attr('fill-opacity', 0.05);
      const [px, py] = d3.pointer(event, el);
      showTip(tip, el, px, py, (t) => {
        tipHead(t, `${perName(o.period)}末`);
        defs.slice().reverse().forEach(([key, label, ci]) => {
          if (!o[key]) return;
          tipLine(t, bsColor(ci), label, `${fYi(o[key])} 亿`);
        });
        if (ratio.get(o.period) != null) tipLine(t, null, '资产负债率', fPct(ratio.get(o.period)));
      });
    })
    .on('click', (event, o) => pickPer(o.period));
  svg.on('pointerleave', () => {
    tip.hidden = true;
    hoverBand.attr('fill-opacity', 0);
  });
});

dash.draw('bslegend', (el) => {
  if (!guard(el, ['bs_lines'], 'chart')) return;
  const defs = param('bsv') === 'claims' ? BS_CLAIMS : BS_ASSETS;
  const items = defs.map(([, label, ci, alpha]) => {
    const it = make('span', 'ly-leg');
    const sw = make('span', 'ly-sw');
    sw.style.background = bsColor(ci);
    sw.style.opacity = String(alpha);
    it.append(sw, document.createTextNode(label));
    return it;
  });
  const line = make('span', 'ly-leg');
  line.append(make('span', 'ly-sw ly-sw-line'), document.createTextNode('资产负债率（右轴）'));
  el.replaceChildren(...items, line);
});

dash.draw('bsstmt', (el) => {
  if (!guard(el, ['bs_lines'], 'chart')) return;
  const p = selPer(), cmp = effCmp(), vals = bsValues(p);
  const box = make('div', 'ly-srows');
  const cell = (key) => {
    const r = vals.get(key), w = whereOf({ period: p, line: key });
    const amt = markEl('span', 'bs_lines', 'value', w, fNum(Number(r.value)));
    amt.className = 'ly-num';
    const pct = markEl('span', 'bs_lines', 'pct_assets', w, fPct(Number(r.pct_assets)));
    pct.className = 'ly-num';
    const v = r[cmp];
    const ch = v == null ? make('span', 'ly-num ly-muted', '—') : markEl('span', 'bs_lines', cmp, w, fSPct(Number(v)));
    if (v != null) ch.className = 'ly-num';
    return [amt, pct, ch];
  };
  BS_TREE.forEach((node) => {
    if (!vals.get(node.key)) return;
    const row = make('div', `ly-srow${node.sum || node.kids ? ' ly-srow-sum' : ''}`);
    const lab = make('span', 'ly-slabel');
    if (node.kids) {
      const open = !!L.openBs[node.key];
      const b = make('button', 'ly-stoggle');
      b.type = 'button';
      b.setAttribute('aria-expanded', String(open));
      b.append(make('span', 'ly-caret', open ? '▾' : '▸'), document.createTextNode(node.label));
      b.addEventListener('click', () => {
        L.openBs[node.key] = !L.openBs[node.key];
        dash.draw('bsstmt');
      });
      lab.append(b);
    } else lab.append(document.createTextNode(node.label));
    withEn(lab, enOf('terms', node.key));
    row.append(lab, ...cell(node.key));
    box.append(row);
    if (node.kids && L.openBs[node.key]) node.kids.forEach((kid) => {
      if (!vals.get(kid)) return;
      const krow = make('div', 'ly-srow ly-srow-l1');
      krow.append(withEn(make('span', 'ly-slabel', BS_LABEL[kid] || kid), enOf('terms', kid)), ...cell(kid));
      box.append(krow);
    });
  });
  el.replaceChildren(box);
});

// ---------- 增长拆解 ----------
const pvRegions = () => {
  const rows = rowsOf('prv_p') || [];
  const regs = [...new Set(rows.map((r) => r.region))];
  return [...regs.filter((r) => !/total/i.test(r)), ...regs.filter((r) => /total/i.test(r))];
};
const selRegion = () => {
  const regs = pvRegions(), rg = param('rg');
  return regs.includes(rg) ? rg : regs.find((r) => /total/i.test(r)) || regs[0];
};
dash.draw('rgsel', (el) => {
  if (!guard(el, ['prv_p'], 'chart')) return;
  const cur = selRegion(), regs = pvRegions();
  const ordered = [...regs.filter((r) => /total/i.test(r)), ...regs.filter((r) => !/total/i.test(r))];
  el.replaceChildren(...ordered.map((r) => {
    const b = withEn(make('button', null, REGION_ZH[r] || r), enOf('regions', r));
    b.type = 'button';
    b.setAttribute('aria-pressed', String(r === cur));
    b.addEventListener('click', () => dash.setParams({ rg: r }));
    return b;
  }));
});
dash.draw('prv', (el) => {
  if (!guard(el, ['prv_p'], 'chart')) return;
  const rg = selRegion();
  const data = rowsOf('prv_p').filter((r) => r.kind === 'q' && r.region === rg).sort((a, b) => (a.period < b.period ? -1 : 1))
    .map((r) => ({ quarter: r.period, volume: r.volume, price: r.price, rate: r.rate, total: r.total_change }));
  el.replaceChildren();
  if (!data.length) return;
  const W = Math.max(300, el.clientWidth || 560), H = 240, bottom = H - 24, topY = 12;
  const layers = d3.stack().keys(PRV_KEYS.map((k) => k[0])).value((d, k) => d[k] || 0).offset(d3.stackOffsetDiverging)(data);
  const lo = Math.min(0, d3.min(layers, (s) => d3.min(s, (d) => d[0])), d3.min(data, (d) => (d.total == null ? 0 : d.total)));
  const hi = Math.max(0, d3.max(layers, (s) => d3.max(s, (d) => d[1])), d3.max(data, (d) => (d.total == null ? 0 : d.total)));
  const svg = svgIn(el, W, H);
  const y = d3.scaleLinear([lo, hi === lo ? lo + 0.1 : hi], [bottom, topY]).nice(5);
  const ticks = y.ticks(5);
  const leftW = Math.ceil(textWidth(svg, ticks.map((t) => fPct0(t)), 'ly-tick')) + 12;
  const x = d3.scaleBand(data.map((d) => d.quarter), [leftW, W - 8]).paddingInner(0.25).paddingOuter(0.1);
  const step = x.step(), off = (step - x.bandwidth()) / 2;
  const g = svg.append('g');
  g.append('g').selectAll('line').data(ticks).join('line').attr('x1', leftW).attr('x2', W - 8).attr('y1', (t) => y(t)).attr('y2', (t) => y(t)).style('stroke', 'var(--cds-chart-grid)');
  g.append('g').attr('class', 'ly-tick').selectAll('text').data(ticks).join('text').attr('x', leftW - 8).attr('y', (t) => y(t)).attr('dy', '0.32em').attr('text-anchor', 'end').text((t) => fPct0(t));
  markPeriod(g, x, selPer(), topY, bottom);
  layers.forEach((layer) => {
    const def = PRV_KEYS.find((k) => k[0] === layer.key);
    g.append('g').style('fill', color(def[2])).selectAll('rect').data(layer).join('rect')
      .attr('x', (d) => x(d.data.quarter)).attr('width', x.bandwidth()).attr('y', (d) => y(Math.max(d[0], d[1]))).attr('height', (d) => Math.abs(y(d[0]) - y(d[1])));
  });
  g.append('line').attr('x1', leftW).attr('x2', W - 8).attr('y1', y(0)).attr('y2', y(0)).style('stroke', 'var(--cds-chart-axis)');
  const cx = (d) => x(d.quarter) + x.bandwidth() / 2;
  const pts = data.filter((d) => d.total != null);
  g.append('path').attr('d', d3.line().x(cx).y((d) => y(d.total))(pts)).style('fill', 'none').style('stroke', 'var(--color-fg)').attr('stroke-width', 2).attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round');
  g.append('g').selectAll('circle').data(pts).join('circle').attr('cx', cx).attr('cy', (d) => y(d.total)).attr('r', 2.4).style('fill', 'var(--color-fg)');
  yearLabels(g.append('g').attr('class', 'ly-tick'), data.map((d) => d.quarter), x, H - 6);
  const tip = makeTip(el);
  const hoverBand = g.append('rect').attr('y', topY).attr('height', bottom - topY).attr('width', step).style('fill', 'var(--color-fg)').attr('fill-opacity', 0).attr('rx', 3).attr('pointer-events', 'none');
  g.append('g').selectAll('rect').data(data).join('rect').attr('x', (d) => x(d.quarter) - off).attr('width', step).attr('y', 0).attr('height', bottom).attr('fill', 'transparent').style('cursor', 'pointer')
    .on('pointerenter pointermove', (event, d) => {
      hoverBand.attr('x', x(d.quarter) - off).attr('fill-opacity', 0.05);
      const [px, py] = d3.pointer(event, el);
      showTip(tip, el, px, py, (t) => {
        tipHead(t, `${perName(d.quarter)} · ${zhEn(REGION_ZH[rg] || rg, enOf('regions', rg))}`);
        PRV_KEYS.forEach(([k, name, ci]) => tipLine(t, color(ci), zhEn(name, enOf('terms', k)), d[k] == null ? 'NM' : fSPct0(d[k])));
        tipLine(t, null, '合计增速', d.total == null ? 'NM' : fSPct0(d.total));
      });
    })
    .on('click', (event, d) => pickPer(d.quarter));
  svg.on('pointerleave', () => {
    tip.hidden = true;
    hoverBand.attr('fill-opacity', 0);
  });
});

dash.draw('prvreg', (el) => {
  if (!guard(el, ['prv_p'], 'chart')) return;
  const p = selPer(), regs = pvRegions();
  const here = rowsOf('prv_p').filter((r) => r.period === p);
  const data = regs.map((g) => here.find((r) => r.region === g)).filter(Boolean)
    .map((r) => ({ region: r.region, revenue: r.revenue, volume: r.volume, price: r.price, rate: r.rate, total: r.total_change }));
  el.replaceChildren();
  if (!data.length) return;
  const W = Math.max(300, el.clientWidth || 560), rowH = 26, H = data.length * rowH + 8;
  const svg = svgIn(el, W, H);
  const labW = Math.ceil(textWidth(svg, data.map((d) => REGION_ZH[d.region] || d.region), 'ly-tick')) + 10;
  const revW = Math.ceil(textWidth(svg, data.map((d) => `${yiText(d.revenue)} 亿`), 'ly-tick')) + 14;
  const totW = Math.ceil(textWidth(svg, data.map((d) => (d.total == null ? 'NM' : fSPct0(d.total))), 'ly-tick')) + 12;
  const ext = d3.max(data, (d) => Math.max(Math.abs(d.total || 0), PRV_KEYS.reduce((s, [k]) => s + Math.max(0, d[k] || 0), 0), PRV_KEYS.reduce((s, [k]) => s + Math.max(0, -(d[k] || 0)), 0))) || 0.1;
  const x = d3.scaleLinear([-ext, ext], [labW + revW, W - totW]).nice();
  const g = svg.append('g');
  g.append('line').attr('x1', x(0)).attr('x2', x(0)).attr('y1', 2).attr('y2', H - 4).style('stroke', 'var(--cds-chart-axis)');
  data.forEach((d, i) => {
    const mid = 4 + i * rowH + rowH / 2 - 2;
    const lab = g.append('text').attr('class', 'ly-tick').attr('x', 0).attr('y', mid).attr('dy', '0.32em').text(REGION_ZH[d.region] || d.region);
    if (/total/i.test(d.region)) lab.style('font-weight', 600);
    g.append('text').attr('class', 'ly-tick').attr('x', labW + revW - 10).attr('y', mid).attr('dy', '0.32em').attr('text-anchor', 'end').text(`${yiText(d.revenue)} 亿`);
    let pos = 0, neg = 0;
    PRV_KEYS.forEach(([k, , ci]) => {
      const v = d[k];
      if (v == null || v === 0) return;
      const a = v > 0 ? pos : neg, b = a + v;
      if (v > 0) pos = b;
      else neg = b;
      g.append('rect').attr('x', x(Math.min(a, b))).attr('width', Math.abs(x(b) - x(a))).attr('y', mid - 7).attr('height', 14).style('fill', color(ci));
    });
    if (d.total != null) g.append('line').attr('x1', x(d.total)).attr('x2', x(d.total)).attr('y1', mid - 10).attr('y2', mid + 10).style('stroke', 'var(--color-fg)').attr('stroke-width', 2);
    g.append('text').attr('class', 'ly-tick').attr('x', W - 2).attr('y', mid).attr('dy', '0.32em').attr('text-anchor', 'end').text(d.total == null ? 'NM' : fSPct0(d.total));
  });
});

// ---------- 图例色块与阈值文字 ----------
dash.draw('swatch', (el) => {
  el.style.background = color(Number(el.dataset.k) || 0);
});
dash.draw('heatwin', (el) => {
  el.textContent = String(HEAT_WINDOW);
});
dash.draw('heatmin', (el) => {
  el.textContent = String(HEAT_MIN / 100);
});

// ---------- 产品热力图 ----------
dash.draw('heat', (el) => {
  if (!guard(el, ['product_p'], 'chart')) return;
  const hm = param('hm');
  const rows = rowsOf('product_p').filter((r) => r.kind === 'q');
  const quarters = [...new Set(rows.map((r) => r.period))].sort();
  const recent = new Set(quarters.slice(-HEAT_WINDOW));
  const ttm = d3.rollup(rows.filter((r) => recent.has(r.period)), (v) => d3.sum(v, (r) => Number(r.total)), (r) => r.product);
  const areaOf = new Map(rows.map((r) => [r.product, r.area]));
  const prods = [...ttm.entries()].filter(([p, v]) => v >= HEAT_MIN && !p.startsWith('Other')).sort((a, b) => AREAS.indexOf(areaOf.get(a[0])) - AREAS.indexOf(areaOf.get(b[0])) || b[1] - a[1]).map((e) => e[0]);
  el.replaceChildren();
  if (!prods.length) return;
  const cell = new Map(rows.map((r) => [`${r.period}|${r.product}`, r]));
  const rowMax = new Map(prods.map((p) => [p, d3.max(quarters, (q) => Math.max(0, Number((cell.get(`${q}|${p}`) || {}).total || 0))) || 1]));
  const W = Math.max(320, el.clientWidth || 720), rowH = 20, top = 20, H = top + prods.length * rowH + 4;
  const svg = svgIn(el, W, H);
  const labW = Math.ceil(textWidth(svg, prods, 'ly-tick')) + 20;
  const x = d3.scaleBand(quarters, [labW, W - 2]).paddingInner(0.08);
  const y = d3.scaleBand(prods, [top, top + prods.length * rowH]).paddingInner(0.14);
  const g = svg.append('g');
  yearLabels(g.append('g').attr('class', 'ly-tick'), quarters, x, 12);
  const p0 = selP(), qs0 = quartersIn(selPer()).filter((q) => x(q) != null);
  prods.forEach((p) => {
    g.append('circle').attr('cx', 5).attr('cy', y(p) + y.bandwidth() / 2).attr('r', 3.5).style('fill', areaColor(areaOf.get(p)));
    const t = g.append('text').attr('class', 'ly-tick ly-heat-label').attr('x', 14).attr('y', y(p) + y.bandwidth() / 2).attr('dy', '0.32em').text(p).style('cursor', 'pointer').on('click', () => dash.setParams({ p }));
    if (p === p0) t.style('fill', 'var(--color-fg)').style('font-weight', 600);
  });
  if (qs0.length) {
    const x0 = x(qs0[0]) - 1, x1 = x(qs0[qs0.length - 1]) + x.bandwidth() + 1;
    g.append('rect').attr('x', x0).attr('width', x1 - x0).attr('y', top - 2).attr('height', prods.length * rowH + 2).style('fill', 'none').style('stroke', 'var(--color-fg)').attr('stroke-opacity', 0.5).attr('rx', 2);
  }
  const cells = [];
  prods.forEach((p) => quarters.forEach((q) => {
    const c = cell.get(`${q}|${p}`);
    if (c) cells.push(c);
  }));
  const tip = makeTip(el);
  g.append('g').selectAll('rect').data(cells).join('rect')
    .attr('x', (c) => x(c.period)).attr('width', x.bandwidth()).attr('y', (c) => y(c.product)).attr('height', y.bandwidth()).attr('rx', 2)
    .style('fill', (c) => {
      if (hm === 'yoy') return c.yoy == null ? 'none' : Number(c.yoy) >= 0 ? 'var(--color-ok)' : 'var(--color-bad)';
      return Number(c.total) > 0 ? areaColor(c.area) : 'none';
    })
    .attr('fill-opacity', (c) => {
      if (hm === 'yoy') {
        const v = c.yoy == null ? 0 : Number(c.yoy);
        return 0.08 + 0.8 * Math.min(1, Math.abs(v) / (v >= 0 ? 1 : 0.5));
      }
      return 0.1 + 0.9 * Math.pow(Math.max(0, Number(c.total)) / rowMax.get(c.product), 0.75);
    })
    .style('stroke', (c) => (c.product === p0 ? 'var(--color-fg)' : 'none')).attr('stroke-opacity', 0.35)
    .style('cursor', 'pointer')
    .on('pointerenter pointermove', (event, c) => {
      const [px, py] = d3.pointer(event, el);
      showTip(tip, el, px, py, (t) => {
        tipHead(t, `${c.product} · ${perName(c.period)}`);
        tipLine(t, areaColor(c.area), '收入', `${fNum(Number(c.total))} 百万美元`);
        tipLine(t, null, '同比', c.yoy == null ? '—' : fSPct(Number(c.yoy)));
        tipLine(t, null, '占总营收', fPct(Number(c.share)));
      });
    })
    .on('pointerleave', () => {
      tip.hidden = true;
    })
    .on('click', (event, c) => dash.setParams({ q: c.period === latestQ() ? null : c.period, p: c.product }));
});

// ---------- 产品版图 ----------
dash.draw('tree', (el) => {
  if (!guard(el, ['product_p'], 'chart')) return;
  const per = selPer(), p0 = selP();
  const rows = rowsOf('product_p').filter((r) => r.period === per && Number(r.total) > 0);
  el.replaceChildren();
  if (!rows.length) return;
  const data = { children: AREAS.map((a) => ({ area: a, children: rows.filter((r) => r.area === a).map((r) => ({ name: r.product, area: a, value: Number(r.total), row: r })) })).filter((d) => d.children.length) };
  const W = Math.max(300, el.clientWidth || 520), H = 340;
  const root = d3.hierarchy(data).sum((d) => d.value || 0).sort((a, b) => b.value - a.value);
  d3.treemap().size([W, H]).paddingOuter(3).paddingTop(20).paddingInner(2).round(true)(root);
  const svg = svgIn(el, W, H);
  const g = svg.append('g');
  (root.children || []).forEach((grp) => {
    g.append('rect').attr('x', grp.x0).attr('y', grp.y0).attr('width', grp.x1 - grp.x0).attr('height', grp.y1 - grp.y0).style('fill', 'none').style('stroke', 'var(--color-border-line)').attr('rx', 3);
    const t = g.append('text').attr('class', 'ly-tick').attr('x', grp.x0 + 5).attr('y', grp.y0 + 14).text(`${AREA_ZH[grp.data.area] || grp.data.area} ${yiText(grp.value)} 亿`);
    if (t.node().getComputedTextLength() > grp.x1 - grp.x0 - 8) t.text(AREA_ZH[grp.data.area] || grp.data.area);
    if (t.node().getComputedTextLength() > grp.x1 - grp.x0 - 8) t.remove();
  });
  const tip = makeTip(el);
  const leaves = g.append('g').selectAll('g').data(root.leaves()).join('g').attr('transform', (d) => `translate(${d.x0},${d.y0})`).style('cursor', 'pointer');
  leaves.append('rect').attr('width', (d) => Math.max(0, d.x1 - d.x0)).attr('height', (d) => Math.max(0, d.y1 - d.y0)).attr('rx', 2).style('fill', (d) => areaColor(d.data.area)).attr('fill-opacity', 0.88)
    .style('stroke', (d) => (d.data.name === p0 ? 'var(--color-fg)' : 'none')).attr('stroke-width', 2);
  leaves.each(function (d) {
    const w = d.x1 - d.x0, h = d.y1 - d.y0;
    if (w < 40 || h < 18) return;
    const node = d3.select(this);
    const t1 = node.append('text').attr('class', 'ly-tree-label').attr('x', 5).attr('y', 14).text(d.data.name);
    if (t1.node().getComputedTextLength() > w - 8) {
      t1.remove();
      return;
    }
    if (h >= 34) {
      const t2 = node.append('text').attr('class', 'ly-tree-value').attr('x', 5).attr('y', 29).text(`${yiText(d.value)} 亿 · ${fPct(Number(d.data.row.share))}`);
      if (t2.node().getComputedTextLength() > w - 8) t2.remove();
    }
  });
  leaves.on('pointerenter pointermove', (event, d) => {
    const [px, py] = d3.pointer(event, el);
    showTip(tip, el, px, py, (t) => {
      tipHead(t, `${d.data.name} · ${perName(per)}`);
      tipLine(t, areaColor(d.data.area), zhEn(AREA_ZH[d.data.area] || d.data.area, enOf('areas', d.data.area)), `${fNum(d.value)} 百万美元`);
      tipLine(t, null, '占总营收', fPct(Number(d.data.row.share)));
      tipLine(t, null, '同比', d.data.row.yoy == null ? '—' : fSPct(Number(d.data.row.yoy)));
    });
  }).on('pointerleave', () => {
    tip.hidden = true;
  }).on('click', (event, d) => dash.setParams({ p: d.data.name }));
});

// ---------- 产品详情 ----------
const productRow = () => {
  const rows = rowsOf('product_p'), p = selP(), per = selPer();
  return rows && p ? rows.find((r) => r.product === p && r.period === per) : null;
};
dash.draw('pname', (el) => {
  if (!guard(el, ['product_p'])) return;
  const p = selP(), row = productRow();
  if (!p) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  el.replaceChildren(row ? markEl('span', 'product_p', 'product', whereOf({ period: row.period, product: p }), p) : document.createTextNode(p));
});
dash.draw('parea', (el) => {
  if (!guard(el, ['product_p'])) return;
  const p = selP(), row = (rowsOf('product_p') || []).find((r) => r.product === p);
  el.style.setProperty('--ly-pill', row ? areaColor(row.area) : 'var(--cds-chart-muted)');
  el.textContent = row ? AREA_ZH[row.area] || row.area : '—';
});
const PFMT = { total_yi: fYi, yoy: fSPct, share: fPct, us: fNum, intl: fNum };
dash.draw('pnum', (el) => {
  el.classList.remove('ly-up', 'ly-down');
  if (!guard(el, ['product_p'])) return;
  const k = el.dataset.k, row = productRow(), v = row ? row[k] : null;
  if (v == null) {
    el.replaceChildren(document.createTextNode('—'));
    return;
  }
  if (k === 'yoy' && Number(v) !== 0) el.classList.add(Number(v) > 0 ? 'ly-up' : 'ly-down');
  el.replaceChildren(markEl('span', 'product_p', k, whereOf({ period: row.period, product: row.product }), PFMT[k](Number(v))));
});
dash.draw('pchart', (el) => {
  if (!guard(el, ['product_p', 'events'], 'chart')) return;
  const p = selP(), all = rowsOf('product_p').filter((r) => r.kind === 'q'), events = rowsOf('events');
  const quarters = [...new Set(all.map((r) => r.period))].sort();
  const by = new Map(all.filter((r) => r.product === p).map((r) => [r.period, r]));
  el.replaceChildren();
  if (!p || !by.size) return;
  // 只披露合计、没有美国/国际拆分的季度，画一根中性色的整柱。
  const data = quarters.map((q) => {
    const r = by.get(q), split = !!r && r.us != null && r.intl != null;
    return { quarter: q, us: split ? Number(r.us) : 0, intl: split ? Number(r.intl) : 0, whole: r && !split ? Number(r.total) : 0, total: r ? Number(r.total) : null, split };
  });
  const W = Math.max(300, el.clientWidth || 520), H = 196, bottom = H - 22, topY = 30;
  const layers = d3.stack().keys(['us', 'intl', 'whole']).offset(d3.stackOffsetDiverging)(data);
  const lo = Math.min(0, d3.min(layers, (s) => d3.min(s, (d) => d[0])));
  const hi = Math.max(1, d3.max(layers, (s) => d3.max(s, (d) => d[1])));
  const svg = svgIn(el, W, H);
  const y = d3.scaleLinear([lo / 100, hi / 100], [bottom, topY]).nice(4);
  const ticks = y.ticks(4);
  const leftW = Math.ceil(textWidth(svg, ticks.map((t) => fYi(t)), 'ly-tick')) + 12;
  const x = d3.scaleBand(quarters, [leftW, W - 4]).paddingInner(0.2).paddingOuter(0.1);
  const g = svg.append('g');
  g.append('g').selectAll('line').data(ticks).join('line').attr('x1', leftW).attr('x2', W - 4).attr('y1', (t) => y(t)).attr('y2', (t) => y(t)).style('stroke', 'var(--cds-chart-grid)');
  g.append('g').attr('class', 'ly-tick').selectAll('text').data(ticks).join('text').attr('x', leftW - 8).attr('y', (t) => y(t)).attr('dy', '0.32em').attr('text-anchor', 'end').text((t) => fYi(t));
  g.append('text').attr('class', 'ly-tick').attr('x', 0).attr('y', 10).attr('text-anchor', 'start').text('亿美元');
  const step = x.step(), off = (step - x.bandwidth()) / 2;
  markPeriod(g, x, selPer(), topY - 4, bottom);
  const cols = { us: color(0), intl: color(2), whole: 'var(--cds-chart-muted)' };
  layers.forEach((layer) => {
    g.append('g').style('fill', cols[layer.key]).selectAll('rect').data(layer).join('rect')
      .attr('x', (d) => x(d.data.quarter)).attr('width', x.bandwidth()).attr('y', (d) => y(Math.max(d[0], d[1]) / 100)).attr('height', (d) => Math.abs(y(d[0] / 100) - y(d[1] / 100)));
  });
  const mine = events.filter((e) => e.product && e.product.split('、').includes(p) && x(e.quarter) != null);
  const lane = g.append('g').style('fill', 'var(--color-fg)').attr('fill-opacity', 0.72);
  d3.group(mine, (e) => e.quarter).forEach((list, q) => {
    [...new Set(list.map((e) => e.type))].slice(0, 2).forEach((t, i) => lane.append('path').attr('d', d3.symbol(TYPE_SYMBOL[t] || d3.symbolCircle, 26)()).attr('transform', `translate(${x(q) + x.bandwidth() / 2},${topY - 10 - i * 7})`));
  });
  yearLabels(g.append('g').attr('class', 'ly-tick'), quarters, x, H - 6);
  const tip = makeTip(el);
  g.append('g').selectAll('rect').data(data).join('rect').attr('x', (d) => x(d.quarter) - off).attr('width', step).attr('y', 0).attr('height', bottom).attr('fill', 'transparent').style('cursor', 'pointer')
    .on('pointerenter pointermove', (event, d) => {
      const [px, py] = d3.pointer(event, el);
      showTip(tip, el, px, py, (t) => {
        tipHead(t, `${p} · ${perName(d.quarter)}`);
        if (d.total == null) tipLine(t, null, '收入', '当季无数据');
        else if (!d.split) {
          tipLine(t, cols.whole, '合计', `${fNum(d.total)} 百万美元`);
          tipLine(t, null, '美国 / 国际', '未单独披露');
        } else {
          tipLine(t, cols.us, '美国', `${fNum(d.us)} 百万美元`);
          tipLine(t, cols.intl, '国际', `${fNum(d.intl)} 百万美元`);
          tipLine(t, null, '合计', `${fNum(d.total)} 百万美元`);
        }
        tipEvents(t, mine.filter((e) => e.quarter === d.quarter));
      });
    })
    .on('pointerleave', () => {
      tip.hidden = true;
    })
    .on('click', (event, d) => pickPer(d.quarter));
});
dash.draw('secfrom', (el) => {
  // 数据说明里的“从哪个季度起改用 SEC 口径”：第一个出现“(combined)”产品的季度；还没有时写“下一个新季度”。
  const rows = rowsOf('product_p');
  if (!rows) {
    el.textContent = '—';
    return;
  }
  const qs = rows.filter((r) => r.kind === 'q' && /\(combined\)$/.test(r.product)).map((r) => r.period).sort();
  el.textContent = qs.length ? perName(qs[0]) : '下一个新季度';
});
dash.draw('pwhole', (el) => {
  // 图例里的“只有合计”：所选产品有只披露合计的季度时才显示。
  const rows = rowsOf('product_p'), p = selP();
  const has = !!rows && rows.some((r) => r.kind === 'q' && r.product === p && (r.us == null || r.intl == null));
  el.hidden = !has;
  if (!has) {
    el.replaceChildren();
    return;
  }
  const sw = make('span', 'ly-sw');
  sw.style.background = 'var(--cds-chart-muted)';
  el.replaceChildren(sw, document.createTextNode('只有合计（未拆分美国/国际）'));
});
dash.draw('pevents', (el) => {
  if (!guard(el, ['events', 'product_p'], 'table')) return;
  const p = selP();
  const list = rowsOf('events').filter((e) => p && e.product && e.product.split('、').includes(p)).sort((a, b) => (a.date < b.date ? 1 : -1));
  el.replaceChildren(...list.map((e) => {
    const li = make('li', 'ly-pev');
    const date = markEl('span', 'events', 'date', whereOf({ id: e.id }), e.date);
    date.className = 'ly-pev-date';
    const a = make('a', 'ly-pev-link');
    a.href = e.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.append(markEl('span', 'events', 'title', whereOf({ id: e.id }), e.title));
    li.append(date, a);
    return li;
  }));
});

// ---------- 利润表浏览 ----------
dash.draw('stmt', (el) => {
  if (!guard(el, ['is_lines', 'area_p', 'product_p'], 'chart')) return;
  const per = selPer(), cmp = effCmp();
  const lines = new Map(rowsOf('is_lines').filter((r) => r.period === per).map((r) => [r.line, r]));
  const areas = rowsOf('area_p').filter((r) => r.period === per);
  const prods = rowsOf('product_p').filter((r) => r.period === per);
  const box = make('div', 'ly-srows');
  const changeCell = (src, row, where, good) => {
    const v = row[cmp];
    if (v == null) return make('span', 'ly-num ly-muted', '—');
    const n = Number(v);
    const s = markEl('span', src, cmp, where, fSPct(n));
    s.className = `ly-num${good && n !== 0 ? (n > 0 ? ' ly-up' : ' ly-down') : ''}`;
    return s;
  };
  const toggle = (label, open, onClick) => {
    const b = make('button', 'ly-stoggle');
    b.type = 'button';
    b.setAttribute('aria-expanded', String(open));
    b.append(make('span', 'ly-caret', open ? '▾' : '▸'), document.createTextNode(label));
    b.addEventListener('click', onClick);
    return b;
  };
  STMT.forEach(([line, label, kind, ng]) => {
    const r = lines.get(line);
    if (!r) return;
    const where = whereOf({ period: per, line });
    const row = make('div', `ly-srow${kind === 'sum' || kind === 'rev' ? ' ly-srow-sum' : ''}`);
    const lab = make('span', 'ly-slabel');
    if (kind === 'rev') lab.append(toggle(label, L.openRev, () => {
      L.openRev = !L.openRev;
      dash.draw('stmt');
    }));
    else lab.append(document.createTextNode(label));
    withEn(lab, enOf('terms', line));
    const ngRow = ng ? lines.get(ng) : null;
    if (ngRow) {
      const n = make('span', 'ly-sng');
      n.append(document.createTextNode('非 GAAP '), markEl('span', 'is_lines', 'value', whereOf({ period: per, line: ng }), line.includes('eps') ? fEps(Number(ngRow.value)) : fNum(Number(ngRow.value))));
      lab.append(n);
    }
    const amt = markEl('span', 'is_lines', 'value', where, line.includes('eps') ? fEps(Number(r.value)) : fNum(Number(r.value)));
    amt.className = 'ly-num';
    const pct = r.pct_rev == null ? make('span', 'ly-num ly-muted', '') : markEl('span', 'is_lines', 'pct_rev', where, fPct(Number(r.pct_rev)));
    if (r.pct_rev != null) pct.className = 'ly-num';
    row.append(lab, amt, pct, changeCell('is_lines', r, where, GOOD_UP.has(line)));
    box.append(row);
    if (kind === 'rev' && L.openRev) {
      AREAS.forEach((a) => {
        const ar = areas.find((x) => x.area === a);
        if (!ar) return;
        const aw = whereOf({ period: per, area: a });
        const open = !!L.openArea[a];
        const arow = make('div', 'ly-srow ly-srow-l1');
        const alab = make('span', 'ly-slabel');
        alab.append(toggle(AREA_ZH[a] || a, open, () => {
          L.openArea[a] = !L.openArea[a];
          dash.draw('stmt');
        }));
        withEn(alab, enOf('areas', a));
        const aAmt = markEl('span', 'area_p', 'total', aw, fNum(Number(ar.total)));
        aAmt.className = 'ly-num';
        const aPct = markEl('span', 'area_p', 'share', aw, fPct(Number(ar.share)));
        aPct.className = 'ly-num';
        arow.append(alab, aAmt, aPct, changeCell('area_p', ar, aw, true));
        box.append(arow);
        if (!open) return;
        prods.filter((x) => x.area === a).sort((m, n) => n.total - m.total).forEach((pr) => {
          const pw = whereOf({ period: per, product: pr.product });
          const prow = make('div', 'ly-srow ly-srow-l2');
          const plab = make('button', 'ly-slabel ly-slink', pr.product);
          plab.type = 'button';
          plab.addEventListener('click', () => dash.setParams({ p: pr.product }));
          const pAmt = markEl('span', 'product_p', 'total', pw, fNum(Number(pr.total)));
          pAmt.className = 'ly-num';
          const pPct = markEl('span', 'product_p', 'share', pw, fPct(Number(pr.share)));
          pPct.className = 'ly-num';
          prow.append(plab, pAmt, pPct, changeCell('product_p', pr, pw, true));
          box.append(prow);
        });
      });
    }
  });
  el.replaceChildren(box);
});

// ---------- 关键事件 ----------
dash.draw('ecount', (el) => {
  if (!guard(el, ['event_counts'])) return;
  const k = el.dataset.k, r = rowsOf('event_counts').find((x) => x.type === k);
  if (!r) {
    el.textContent = '0';
    return;
  }
  el.replaceChildren(markEl('span', 'event_counts', 'n', whereOf({ type: k }), String(r.n)));
});
dash.draw('glyph', (el) => {
  el.replaceChildren();
  const svg = d3.select(el).append('svg').attr('width', 10).attr('height', 10).attr('viewBox', '-5 -5 10 10').attr('aria-hidden', 'true');
  svg.append('path').attr('d', d3.symbol(TYPE_SYMBOL[el.dataset.k] || d3.symbolCircle, 26)()).style('fill', 'currentColor');
});
dash.draw('erows', (tb) => {
  if (!guard(tb, ['events'], 'table')) return;
  const et = param('et'), per = selPer(), qs = allQ();
  const rows = rowsOf('events').filter((e) => et === '全部' || e.type === et).sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
  tb.replaceChildren(...rows.map((e) => {
    const tr = document.createElement('tr');
    tr.dataset.key = e.id;
    if (inPer(e.quarter, per)) tr.className = 'ly-hl';
    const td = (text) => make('td', null, text);
    const tdTitle = document.createElement('td');
    const a = make('a', null, e.title);
    a.href = e.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    tdTitle.append(a);
    tr.append(td(e.date), td(e.quarter), td(e.type), td(e.product), tdTitle);
    tr.addEventListener('click', (ev) => {
      if (ev.target.closest('a')) return;
      if (qs.includes(e.quarter)) pickPer(e.quarter);
    });
    return tr;
  }));
});
