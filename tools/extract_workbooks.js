// 礼来 Financial Workbook 抽取脚本
// 用法：在浏览器打开 https://investor.lilly.com/financial-information/quarterly-results ，
// 把整段粘到开发者工具的控制台运行（或让 Claude 用内置浏览器运行）。
// 结果：window.__LLY.csv.{is, pr, prv} 三份 CSV 文本，以及每份的行数、数字总和、字符数（写入文件后用来核对）。
// 规则与看板现有数据一致：
//   - 利润表：每季度 Workbook 的 Income Statement 页，当季“三个月”的 GAAP 与非 GAAP 列；2021 年取 2022 年 Workbook 的上年同期列。
//   - 分产品收入：每年最后一份 Workbook 的 “<年> Revenue” 页（年中时用最新一季的 Workbook）；Diabetes 统一改称 Cardiometabolic Health。
//   - PRV：同一批 Workbook 的 PRV 页。
//   - 2023 年的分产品收入原表以美元计，自动换算为百万美元。
(async () => {
  const X = (window.__LLY = {});
  const unzip = async (buf) => {
    const dv = new DataView(buf), td = new TextDecoder();
    let eocd = -1;
    for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 65557); i--) if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    if (eocd < 0) throw new Error('not a zip');
    const n = dv.getUint16(eocd + 10, true);
    let p = dv.getUint32(eocd + 16, true);
    const files = {};
    for (let k = 0; k < n; k++) {
      const method = dv.getUint16(p + 10, true), comp = dv.getUint32(p + 20, true);
      const nl = dv.getUint16(p + 28, true), el = dv.getUint16(p + 30, true), cl = dv.getUint16(p + 32, true), off = dv.getUint32(p + 42, true);
      files[td.decode(new Uint8Array(buf, p + 46, nl))] = { method, comp, off };
      p += 46 + nl + el + cl;
    }
    return async (name) => {
      const f = files[name];
      if (!f) return null;
      const start = f.off + 30 + dv.getUint16(f.off + 26, true) + dv.getUint16(f.off + 28, true);
      const data = new Uint8Array(buf, start, f.comp);
      if (f.method === 0) return td.decode(data);
      return new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).text();
    };
  };
  const load = async (url) => {
    const read = await unzip(await (await fetch(url)).arrayBuffer()), P = new DOMParser();
    const wb = P.parseFromString(await read('xl/workbook.xml'), 'application/xml');
    const rels = P.parseFromString(await read('xl/_rels/workbook.xml.rels'), 'application/xml');
    const rel = {};
    for (const r of rels.getElementsByTagName('Relationship')) rel[r.getAttribute('Id')] = r.getAttribute('Target');
    const ss = [], ssx = await read('xl/sharedStrings.xml');
    if (ssx) for (const si of P.parseFromString(ssx, 'application/xml').getElementsByTagName('si')) ss.push(Array.from(si.getElementsByTagName('t')).map((t) => t.textContent).join(''));
    const sheets = [];
    for (const s of wb.getElementsByTagName('sheet')) {
      let tgt = rel[s.getAttribute('r:id')];
      if (!tgt) continue;
      tgt = tgt.replace(/^\//, '');
      const sd = P.parseFromString(await read(tgt.startsWith('xl/') ? tgt : 'xl/' + tgt), 'application/xml');
      const rows = [];
      for (const row of sd.getElementsByTagName('row')) {
        const cells = {};
        for (const c of row.getElementsByTagName('c')) {
          const col = c.getAttribute('r').replace(/\d+/g, ''), t = c.getAttribute('t'), v = c.getElementsByTagName('v')[0];
          let val = null;
          if (t === 's' && v) val = ss[+v.textContent];
          else if (t === 'inlineStr') val = Array.from(c.getElementsByTagName('t')).map((x) => x.textContent).join('');
          else if (v) val = t === 'str' || t === 'e' || t === 'b' ? v.textContent : Number(v.textContent);
          if (val !== null && String(val).trim() !== '') cells[col] = typeof val === 'string' ? val.replace(/\s+/g, ' ').trim() : val;
        }
        if (Object.keys(cells).length) rows.push({ r: +row.getAttribute('r'), cells });
      }
      sheets.push({ name: s.getAttribute('name'), rows });
    }
    return sheets;
  };
  const num = (v) => {
    if (v == null) return null;
    if (typeof v === 'number') return v;
    const s = String(v).trim();
    if (/^(NM|N\/A)$/i.test(s)) return null;
    if (/^[-—–]$/.test(s)) return 0;
    const neg = /^\(.*\)%?$/.test(s), n = Number(s.replace(/[(),%$\s]/g, ''));
    if (!Number.isFinite(n)) return null;
    const val = (s.includes('%') ? n / 100 : n) * (neg ? -1 : 1);
    return Object.is(val, -0) ? 0 : val;
  };
  const colIdx = (c) => c.split('').reduce((a, ch) => a * 26 + ch.charCodeAt(0) - 64, 0);
  const label = (r) => (typeof r.cells.A === 'string' ? r.cells.A : typeof r.cells.B === 'string' ? r.cells.B : '');

  // 1. 找到页面上所有季度的 Workbook 链接（例如 2026Q3、2021Q4R=修订版）
  const links = {};
  document.querySelectorAll('main a[href]').forEach((a) => {
    const m = a.textContent.replace(/\s+/g, ' ').trim().match(/^Q([1-4]) (\d{4}) ?(Revised )?Financial[ _]?Workbook$/i);
    if (m) links[`${m[2]}Q${m[1]}${m[3] ? 'R' : ''}`] = a.href;
  });
  const quarters = Object.keys(links).filter((k) => !k.endsWith('R') && k >= '2022Q1').sort();
  const latest = quarters[quarters.length - 1];
  const years = [...new Set(quarters.map((q) => q.slice(0, 4)))];
  const revWB = ['2021Q4R', ...years.map((y) => (y === latest.slice(0, 4) ? latest : `${y}Q4`))].filter((k) => links[k]);
  const wb = {};
  await Promise.all([...new Set([...quarters, ...revWB])].map(async (k) => { wb[k] = await load(links[k]); }));

  // 2. 利润表
  const IS_MAP = [['revenue', /^revenue$/i], ['cost_of_sales', /^cost of sales$/i], ['gross_margin', /^gross margin$/i], ['rnd', /^research and development$/i], ['msa', /^marketing, selling,? and administrative$/i], ['acquired_iprd', /^acquired (in-process research|ipr&d)/i], ['impairment_restructuring', /^asset impairment, restructuring/i], ['operating_income', /^operating income/i], ['other_net', /^other\s*[-–]\s*net/i], ['pretax_income', /^income \(loss\) before income taxes|^income before income taxes/i], ['income_tax', /^income tax expense/i], ['net_income', /^net income( \(loss\))?$/i], ['eps_diluted', /^earnings( \(loss\))? per share/i], ['diluted_shares_k', /^diluted shares outstanding/i]];
  const parseIS = (key, pick) => {
    const sh = wb[key].find((s) => /income statement/i.test(s.name));
    const hdr = sh.rows.find((r) => Object.values(r.cells).some((v) => v === 'GAAP Reported'));
    const cols = (re) => Object.entries(hdr.cells).filter(([, v]) => re.test(String(v))).map(([c]) => c).sort((a, b) => colIdx(a) - colIdx(b));
    const g = cols(/^GAAP Reported$/), n = cols(/^Non-GAAP Adjusted$/), out = {};
    for (const [k, re] of IS_MAP) {
      const row = sh.rows.find((r) => r.r > hdr.r && re.test(label(r)));
      if (row && !(k in out)) { out[k] = num(row.cells[g[pick]]); out['ng_' + k] = num(row.cells[n[pick]]); }
    }
    return out;
  };
  const IS = [], notes = [];
  for (const k of quarters) {
    IS.push({ quarter: k, ...parseIS(k, 0) });
    if (k.startsWith('2022')) IS.push({ quarter: '2021Q' + k.slice(-1), ...parseIS(k, 1) });
  }
  IS.sort((a, b) => a.quarter.localeCompare(b.quarter));
  for (const i of IS) {
    for (const k of ['acquired_iprd', 'impairment_restructuring']) if (i[k] == null) {
      const other = k === 'acquired_iprd' ? 'impairment_restructuring' : 'acquired_iprd';
      const implied = i.gross_margin - i.rnd - i.msa - (i[other] || 0) - i.operating_income;
      if (Math.abs(implied) < 0.6) i[k] = 0; else notes.push(`${i.quarter} ${k} 缺失，按等式应为 ${implied.toFixed(1)}`);
    }
    if (Math.abs(i.gross_margin - i.rnd - i.msa - i.acquired_iprd - i.impairment_restructuring - i.operating_income) > 0.6) notes.push(`${i.quarter} 营业利润等式不平`);
    if (Math.abs(i.pretax_income - i.income_tax - i.net_income) > 0.6) notes.push(`${i.quarter} 净利润等式不平`);
  }

  // 3. 分产品收入与 PRV
  const PR = [], areaTotals = {}, PRV = [];
  const normArea = (a) => (/^diabetes$/i.test(a) ? 'Cardiometabolic Health' : a);
  const normProd = (p) => { const s = p.replace(/\(\d+\)/g, '').replace(/\*+/g, '').replace(/\s+/g, ' ').trim(); return /^Other Diabetes$/i.test(s) ? 'Other Cardiometabolic Health' : s; };
  for (const key of revWB) {
    const year = key.slice(0, 4), sh = wb[key].find((s) => /^\d{4} Revenue$/.test(s.name));
    const hdr = sh.rows.find((r) => Object.values(r.cells).includes('Intl')), qrow = sh.rows.filter((r) => r.r < hdr.r).slice(-1)[0];
    const groups = [];
    for (const [c, v] of Object.entries(hdr.cells).sort((a, b) => colIdx(a[0]) - colIdx(b[0]))) {
      if (v === 'US') groups.push({ q: String(qrow.cells[c] || ''), us: c });
      else if (v === 'Intl') groups[groups.length - 1].intl = c;
      else if (v === 'Total') groups[groups.length - 1].total = c;
    }
    const qGroups = groups.filter((gp) => /^Q[1-4]$/.test(gp.q));
    const tot = sh.rows.find((r) => /^total revenue$/i.test(String(r.cells.A || '')));
    const scale = qGroups.some((gp) => num(tot.cells[gp.total]) > 1e6) ? 1e6 : 1;
    let pending = [];
    for (const r of sh.rows.filter((x) => x.r > hdr.r)) {
      if (!groups.some((gp) => typeof r.cells[gp.us] === 'number' || typeof r.cells[gp.total] === 'number')) continue;
      if (typeof r.cells.B === 'string') { pending.push(r); continue; }
      if (typeof r.cells.A !== 'string') continue;
      const area = normArea(r.cells.A);
      for (const gp of qGroups) {
        if (r.cells[gp.total] == null) continue;
        const qk = year + gp.q;
        areaTotals[qk + '|' + area] = num(r.cells[gp.total]) / scale;
        if (/^total revenue$/i.test(area)) continue;
        for (const pr of pending) {
          const us = num(pr.cells[gp.us]), intl = num(pr.cells[gp.intl]), total = num(pr.cells[gp.total]);
          if (us == null && intl == null && total == null) continue;
          PR.push({ quarter: qk, area, product: normProd(pr.cells.B), us: us == null ? null : us / scale, intl: intl == null ? null : intl / scale, total: total == null ? null : total / scale });
        }
      }
      pending = [];
    }
    const ps = wb[key].find((s) => s.name === 'PRV'), ph = ps.rows.find((r) => Object.values(r.cells).includes('Price'));
    const pq = ps.rows.filter((r) => r.r < ph.r).slice(-1)[0], pg = [];
    for (const [c, v] of Object.entries(ph.cells).sort((a, b) => colIdx(a[0]) - colIdx(b[0]))) {
      if (v === '$') pg.push({ q: String(pq.cells[c] || ''), d: c });
      else if (pg.length) pg[pg.length - 1][String(v).toLowerCase()] = c;
    }
    for (const r of ps.rows.filter((x) => x.r > ph.r && pg.some((g) => typeof x.cells[g.d] === 'number'))) {
      const region = String(r.cells.B || r.cells.A).trim();
      for (const g of pg) {
        const m = g.q.match(/^Q([1-4])\b/), d = num(r.cells[g.d]);
        if (!m || d == null) continue;
        PRV.push({ quarter: `${year}Q${m[1]}`, region, revenue: d, price: num(r.cells[g.price]), rate: num(r.cells[g.rate]), volume: num(r.cells[g.volume]), total_change: num(r.cells[g.total]) });
      }
    }
  }
  const prQ = [...new Set(PR.map((p) => p.quarter))].sort();
  for (const q of prQ) {
    const sum = PR.filter((p) => p.quarter === q).reduce((a, p) => a + (p.total || 0), 0), isr = (IS.find((i) => i.quarter === q) || {}).revenue;
    if (isr != null && Math.abs(sum - isr) > 6) notes.push(`${q} 产品合计 ${sum.toFixed(1)} 与利润表营收 ${isr} 不一致`);
  }

  // 4. 输出 CSV
  const qEnd = (q) => `${q.slice(0, 4)}-${['03-31', '06-30', '09-30', '12-31'][+q.slice(-1) - 1]}`;
  const r1 = (v, d = 1) => (v == null ? '' : String(Math.round(v * 10 ** d) / 10 ** d));
  const isCols = ['quarter', 'period_end', 'revenue', 'cost_of_sales', 'gross_margin', 'rnd', 'msa', 'acquired_iprd', 'impairment_restructuring', 'operating_income', 'other_net', 'pretax_income', 'income_tax', 'net_income', 'eps_diluted', 'diluted_shares_k', 'ng_gross_margin', 'ng_operating_income', 'ng_net_income', 'ng_eps_diluted'];
  const prCols = ['quarter', 'area', 'product', 'us', 'intl', 'total'];
  const prvCols = ['quarter', 'period_end', 'region', 'revenue', 'price', 'rate', 'volume', 'total_change'];
  const csv = {
    is: [isCols.join(',')].concat(IS.map((i) => isCols.map((c) => (c === 'quarter' ? i.quarter : c === 'period_end' ? qEnd(i.quarter) : r1(i[c], c.includes('eps') ? 2 : 1))).join(','))).join('\n'),
    pr: [prCols.join(',')].concat(PR.sort((a, b) => a.quarter.localeCompare(b.quarter)).map((p) => prCols.map((c) => (['quarter', 'area', 'product'].includes(c) ? p[c] : r1(p[c]))).join(','))).join('\n'),
    prv: [prvCols.join(',')].concat(PRV.sort((a, b) => a.quarter.localeCompare(b.quarter)).map((p) => prvCols.map((c) => (c === 'period_end' ? qEnd(p.quarter) : c === 'quarter' || c === 'region' ? p[c] : r1(p[c], c === 'revenue' ? 1 : 3))).join(','))).join('\n'),
  };
  const checksum = (t) => {
    const lines = t.split('\n');
    let s = 0;
    lines.slice(1).forEach((l) => l.split(',').forEach((x) => { if (x !== '' && !/^\d{4}-\d{2}-\d{2}$/.test(x) && !isNaN(Number(x))) s += Number(x); }));
    return { rows: lines.length - 1, sum: Math.round(s * 1000) / 1000, chars: t.length };
  };
  X.csv = csv;
  X.check = { is: checksum(csv.is), pr: checksum(csv.pr), prv: checksum(csv.prv), latest, notes };
  console.log('礼来 Workbook 抽取完成', X.check);
})();
