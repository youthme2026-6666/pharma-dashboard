// 礼来财务看板：页面上所有派生数字的计算。每个计算只读附带的数据文件（或另一个计算），读者可以在“数据来源”里看到公式。
// 期间（period）有两种：季度（kind q，如 2026Q3）和年度（kind y，如 2025；当年未满四个季度时为年初至今）。

dash.calc('kpi', {
  title: '核心指标',
  description: '由利润表算出的营收（亿美元）、同比增速、毛利率、研发费用率、营业利润率、净利润和每股收益；带 _pp 的是比率与去年同期相比变化的百分点。季度与去年同季比较；年度用礼来公布的全年或年初至今数字，与上年相同月数比较。',
  inputs: ['income_statement', 'income_annual'],
  fn: (income_statement, income_annual) => {
    const num = (v) => (v === '' || v == null ? null : Number(v));
    const pick = (r) => (r ? { revenue: num(r.revenue), gross: num(r.gross_margin), rnd: num(r.rnd), op: num(r.operating_income), ni: num(r.net_income), eps: num(r.eps_diluted), epsNg: num(r.ng_eps_diluted), iprd: num(r.acquired_iprd) } : null);
    const r4 = (x) => (x == null || !Number.isFinite(x) ? null : Math.round(x * 10000) / 10000);
    const yi = (m) => (m == null ? null : Math.round(m / 10) / 10);
    const ratio = (a, b) => (a == null || !b ? null : a / b);
    const growth = (a, b) => (a == null || b == null || b <= 0 ? null : r4(a / b - 1));
    const diff = (a, b) => (a == null || b == null ? null : Math.round((a - b) * 1000) / 10);
    const build = (period, kind, months, c, p) => {
      const gm = ratio(c.gross, c.revenue), rd = ratio(c.rnd, c.revenue), opm = ratio(c.op, c.revenue);
      return {
        period, kind, months,
        revenue_yi: yi(c.revenue),
        revenue_yoy: p ? growth(c.revenue, p.revenue) : null,
        gross_margin: r4(gm),
        gross_margin_pp: p ? diff(gm, ratio(p.gross, p.revenue)) : null,
        rnd_yi: yi(c.rnd),
        rnd_ratio: r4(rd),
        rnd_ratio_pp: p ? diff(rd, ratio(p.rnd, p.revenue)) : null,
        op_margin: r4(opm),
        op_margin_pp: p ? diff(opm, ratio(p.op, p.revenue)) : null,
        net_income_yi: yi(c.ni),
        net_income_yoy: p ? growth(c.ni, p.ni) : null,
        eps: c.eps,
        eps_yoy: p ? growth(c.eps, p.eps) : null,
        eps_ng: c.epsNg,
        acquired_iprd_yi: yi(c.iprd),
      };
    };
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const quarters = [...income_statement].sort((a, b) => (a.quarter < b.quarter ? -1 : 1));
    const qBy = new Map(quarters.map((r) => [r.quarter, r]));
    const out = quarters.map((r) => build(r.quarter, 'q', 3, pick(r), pick(qBy.get(lastYear(r.quarter)))));
    const aBy = new Map(income_annual.map((r) => [`${String(r.year)}|${Number(r.months)}`, r]));
    for (const y of [...new Set(income_annual.map((r) => String(r.year)))].sort()) {
      const months = Math.max(...income_annual.filter((r) => String(r.year) === y).map((r) => Number(r.months)));
      const cur = pick(aBy.get(`${y}|${months}`));
      if (cur) out.push(build(y, 'y', months, cur, pick(aBy.get(`${Number(y) - 1}|${months}`))));
    }
    return out;
  },
});

dash.calc('area_p', {
  title: '治疗领域收入',
  description: '把分产品收入按治疗领域加总（百万美元）：季度值，以及按自然年加总的年度值；附美国与国际收入（领域里有产品只披露合计时留空）、占比，以及与去年同期（年度只比较相同季度）和上一季度的变化。',
  inputs: ['product_revenue'],
  fn: (product_revenue) => {
    const sums = new Map(), totals = new Map();
    for (const r of product_revenue) {
      const key = `${r.quarter}|${r.area}`;
      const s = sums.get(key) || { total: 0, us: 0, intl: 0, split: true };
      s.total += Number(r.total);
      if (r.us == null || r.us === '' || r.intl == null || r.intl === '') s.split = false;
      else { s.us += Number(r.us); s.intl += Number(r.intl); }
      sums.set(key, s);
      totals.set(r.quarter, (totals.get(r.quarter) || 0) + Number(r.total));
    }
    const quarters = [...totals.keys()].sort(), areas = [...new Set(product_revenue.map((r) => r.area))];
    const r1 = (x) => Math.round(x * 10) / 10, r4 = (x) => Math.round(x * 10000) / 10000;
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const prevQ = (q) => (q.endsWith('Q1') ? Number(q.slice(0, 4)) - 1 + 'Q4' : q.slice(0, 5) + (Number(q.slice(5)) - 1));
    const rate = (t, b) => (b != null && b > 0 && t >= 0 ? r4(t / b - 1) : null);
    const out = [];
    for (const q of quarters) for (const area of areas) {
      const s = sums.get(`${q}|${area}`);
      if (!s) continue;
      const py = sums.get(`${lastYear(q)}|${area}`), pq = sums.get(`${prevQ(q)}|${area}`);
      out.push({ period: q, kind: 'q', quarters: 1, area, total: r1(s.total), us: s.split ? r1(s.us) : null, intl: s.split ? r1(s.intl) : null, share: r4(s.total / totals.get(q)), delta: py ? r1(s.total - py.total) : null, yoy: rate(s.total, py ? py.total : null), qoq: rate(s.total, pq ? pq.total : null) });
    }
    for (const y of [...new Set(quarters.map((q) => q.slice(0, 4)))]) {
      const qs = quarters.filter((q) => q.startsWith(y)), base = qs.map(lastYear), hasBase = base.every((q) => totals.has(q));
      const yearTotal = qs.reduce((a, q) => a + totals.get(q), 0);
      const sum = (list, area) => list.reduce((a, q) => {
        const s = sums.get(`${q}|${area}`);
        return s ? { total: a.total + s.total, us: a.us + s.us, intl: a.intl + s.intl, n: a.n + 1, split: a.split && s.split } : a;
      }, { total: 0, us: 0, intl: 0, n: 0, split: true });
      for (const area of areas) {
        const c = sum(qs, area);
        if (!c.n) continue;
        const b = hasBase ? sum(base, area) : null;
        out.push({ period: y, kind: 'y', quarters: qs.length, area, total: r1(c.total), us: c.split ? r1(c.us) : null, intl: c.split ? r1(c.intl) : null, share: r4(c.total / yearTotal), delta: b ? r1(c.total - b.total) : null, yoy: b ? rate(c.total, b.total) : null, qoq: null });
      }
    }
    return out;
  },
});

dash.calc('series_p', {
  title: '主要产品收入',
  description: '最近四个季度收入最高的 8 个产品各自一组（只在最新季度仍单独披露的产品里选），其余产品（含各领域的“其他”）合并为“其他产品”：季度值和按自然年加总的年度值（百万美元），附占比与同比变化（年度只比较相同季度）。',
  inputs: ['product_revenue'],
  fn: (product_revenue) => {
    const quarters = [...new Set(product_revenue.map((r) => r.quarter))].sort();
    const recent = new Set(quarters.slice(-4)), ttm = new Map();
    const live = new Set(product_revenue.filter((r) => r.quarter === quarters[quarters.length - 1]).map((r) => r.product));
    for (const r of product_revenue) if (recent.has(r.quarter) && live.has(r.product) && !r.product.startsWith('Other')) ttm.set(r.product, (ttm.get(r.product) || 0) + Number(r.total));
    const top = [...ttm.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map((e) => e[0]);
    const names = [...top, '其他产品'];
    const sums = new Map(), totals = new Map();
    for (const r of product_revenue) {
      const key = `${r.quarter}|${top.includes(r.product) ? r.product : '其他产品'}`;
      sums.set(key, (sums.get(key) || 0) + Number(r.total));
      totals.set(r.quarter, (totals.get(r.quarter) || 0) + Number(r.total));
    }
    const r1 = (x) => Math.round(x * 10) / 10, r4 = (x) => Math.round(x * 10000) / 10000;
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const out = [];
    for (const q of quarters) {
      const py = lastYear(q), hasPy = totals.has(py);
      names.forEach((series, rank) => {
        if (!sums.has(`${q}|${series}`) && !(hasPy && sums.has(`${py}|${series}`))) return;
        const t = sums.get(`${q}|${series}`) || 0, p = sums.get(`${py}|${series}`) || 0;
        out.push({ period: q, kind: 'q', quarters: 1, series, rank, total: r1(t), share: r4(t / totals.get(q)), delta: hasPy ? r1(t - p) : null, yoy: hasPy && p > 0 && t >= 0 ? r4(t / p - 1) : null });
      });
    }
    for (const y of [...new Set(quarters.map((q) => q.slice(0, 4)))]) {
      const qs = quarters.filter((q) => q.startsWith(y)), base = qs.map(lastYear), hasBase = base.every((q) => totals.has(q));
      const yearTotal = qs.reduce((a, q) => a + totals.get(q), 0);
      names.forEach((series, rank) => {
        const sum = (list) => list.reduce((a, q) => a + (sums.get(`${q}|${series}`) || 0), 0);
        if (!qs.some((q) => sums.has(`${q}|${series}`)) && !(hasBase && base.some((q) => sums.has(`${q}|${series}`)))) return;
        const t = sum(qs), p = hasBase ? sum(base) : null;
        out.push({ period: y, kind: 'y', quarters: qs.length, series, rank, total: r1(t), share: r4(t / yearTotal), delta: p == null ? null : r1(t - p), yoy: p != null && p > 0 && t >= 0 ? r4(t / p - 1) : null });
      });
    }
    return out;
  },
});

dash.calc('product_p', {
  title: '产品收入',
  description: '每个产品的季度收入和按自然年加总的年度收入（百万美元）、美国与国际收入（只披露合计的季度留空）、占总收入的比例，以及同比（年度只比较相同季度）和环比增速。',
  inputs: ['product_revenue'],
  fn: (product_revenue) => {
    const totals = new Map(), byKey = new Map(), areaOf = new Map();
    const num = (v) => (v === '' || v == null ? null : Number(v));
    for (const r of product_revenue) {
      totals.set(r.quarter, (totals.get(r.quarter) || 0) + Number(r.total));
      byKey.set(`${r.quarter}|${r.product}`, { total: Number(r.total), us: num(r.us), intl: num(r.intl) });
      areaOf.set(r.product, r.area);
    }
    const quarters = [...totals.keys()].sort();
    const r1 = (x) => Math.round(x * 10) / 10, r4 = (x) => Math.round(x * 10000) / 10000;
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const prevQ = (q) => (q.endsWith('Q1') ? Number(q.slice(0, 4)) - 1 + 'Q4' : q.slice(0, 5) + (Number(q.slice(5)) - 1));
    const rate = (t, p) => (p != null && p > 0 && t >= 0 ? r4(t / p - 1) : null);
    const out = product_revenue.map((r) => {
      const t = Number(r.total), py = byKey.get(`${lastYear(r.quarter)}|${r.product}`), pq = byKey.get(`${prevQ(r.quarter)}|${r.product}`);
      return { period: r.quarter, kind: 'q', area: r.area, product: r.product, total: t, us: num(r.us), intl: num(r.intl), total_yi: Math.round(t / 10) / 10, share: r4(t / totals.get(r.quarter)), yoy: rate(t, py ? py.total : null), qoq: rate(t, pq ? pq.total : null) };
    });
    for (const y of [...new Set(quarters.map((q) => q.slice(0, 4)))]) {
      const qs = quarters.filter((q) => q.startsWith(y)), base = qs.map(lastYear), hasBase = base.every((q) => totals.has(q));
      const yearTotal = qs.reduce((a, q) => a + totals.get(q), 0);
      for (const product of areaOf.keys()) {
        const got = qs.map((q) => byKey.get(`${q}|${product}`)).filter(Boolean);
        if (!got.length) continue;
        const t = r1(got.reduce((a, v) => a + v.total, 0));
        const prior = hasBase ? base.map((q) => byKey.get(`${q}|${product}`)).filter(Boolean) : [];
        const p = prior.length ? prior.reduce((a, v) => a + v.total, 0) : null;
        const split = got.every((v) => v.us != null && v.intl != null);
        out.push({ period: y, kind: 'y', area: areaOf.get(product), product, total: t, us: split ? r1(got.reduce((a, v) => a + v.us, 0)) : null, intl: split ? r1(got.reduce((a, v) => a + v.intl, 0)) : null, total_yi: Math.round(t / 10) / 10, share: r4(t / yearTotal), yoy: rate(t, p), qoq: null });
      }
    }
    return out;
  },
});

dash.calc('mix_p', {
  title: '收入结构',
  description: '每个季度和年度：美国收入占总收入的比例，以及替尔泊肽（Mounjaro 与 Zepbound）的合计收入（亿美元）和占比。美国收入按产品加总；某个期间有产品只披露合计时，改用分地区收入里的美国收入。',
  inputs: ['product_revenue', 'price_volume'],
  fn: (product_revenue, price_volume) => {
    const usRev = new Map();
    for (const r of price_volume) if (r.region === 'U.S.' && r.revenue != null && r.revenue !== '') usRev.set(r.quarter, Number(r.revenue));
    const m = new Map();
    const add = (period, kind, r) => {
      const s = m.get(period) || { period, kind, total: 0, us: 0, tirz: 0, split: true, qs: new Set() };
      const t = Number(r.total);
      s.total += t; s.qs.add(r.quarter);
      if (r.us == null || r.us === '') s.split = false;
      else s.us += Number(r.us);
      if (r.product === 'Mounjaro' || r.product === 'Zepbound') s.tirz += t;
      m.set(period, s);
    };
    for (const r of product_revenue) {
      add(r.quarter, 'q', r);
      add(r.quarter.slice(0, 4), 'y', r);
    }
    const r4 = (x) => Math.round(x * 10000) / 10000;
    return [...m.values()].map((s) => {
      const qs = [...s.qs];
      const us = s.split ? s.us : qs.every((q) => usRev.has(q)) ? qs.reduce((a, q) => a + usRev.get(q), 0) : null;
      return { period: s.period, kind: s.kind, us_share: us == null ? null : r4(us / s.total), tirz_yi: Math.round(s.tirz / 10) / 10, tirz_share: r4(s.tirz / s.total) };
    });
  },
});

dash.calc('prv_p', {
  title: '价格、汇率与销量拆分',
  description: '把季度拆分表和年度（年初至今）拆分表合在一起，按期间索引；数值照礼来公布的原样（小数，0.1 即 10%），礼来标为 NM 的留空。',
  inputs: ['price_volume', 'price_volume_annual'],
  fn: (price_volume, price_volume_annual) => {
    const n = (v) => (v === '' || v == null ? null : Number(v));
    const row = (period, kind, r) => ({ period, kind, region: r.region, revenue: Number(r.revenue), price: n(r.price), rate: n(r.rate), volume: n(r.volume), total_change: n(r.total_change) });
    return [...price_volume.map((r) => row(String(r.quarter), 'q', r)), ...price_volume_annual.map((r) => row(String(r.year), 'y', r))];
  },
});

dash.calc('region_p', {
  title: '地区收入',
  description: '各地区每个季度和年度的收入（百万美元）、占总收入的比例，以及礼来公布的收入同比增速。',
  inputs: ['prv_p'],
  fn: (prv_p) => {
    const total = new Map(prv_p.filter((r) => /total/i.test(r.region)).map((r) => [r.period, r.revenue]));
    return prv_p.filter((r) => !/total/i.test(r.region)).map((r) => ({ period: r.period, kind: r.kind, region: r.region, revenue: r.revenue, share: Math.round((r.revenue / total.get(r.period)) * 10000) / 10000, total_change: r.total_change }));
  },
});

dash.calc('cost_flow', {
  title: '营收去向',
  description: '每个季度和年度的营收在营业成本、各项费用、所得税和净利润之间的分配（百万美元）及各项占营收的比例；其他收支为净收益时单列为流入。',
  inputs: ['income_statement', 'income_annual'],
  fn: (income_statement, income_annual) => {
    const out = [];
    const add = (period, kind, r) => {
      const v = (k) => Number(r[k] || 0), revenue = v('revenue'), other = v('other_net');
      const items = [['revenue', revenue], ['cost_of_sales', v('cost_of_sales')], ['rnd', v('rnd')], ['msa', v('msa')], ['acquired_iprd', v('acquired_iprd')], ['impairment_restructuring', v('impairment_restructuring')], ['other_expense', other < 0 ? -other : 0], ['income_tax', v('income_tax')], ['net_income', v('net_income')], ['other_income', other > 0 ? other : 0]];
      items.forEach(([item, amount], order) => out.push({ period, kind, item, order, amount: Math.round(amount * 10) / 10, share: Math.round((amount / revenue) * 10000) / 10000 }));
    };
    income_statement.forEach((r) => add(r.quarter, 'q', r));
    const months = new Map();
    income_annual.forEach((r) => months.set(String(r.year), Math.max(months.get(String(r.year)) || 0, Number(r.months))));
    income_annual.filter((r) => Number(r.months) === months.get(String(r.year))).forEach((r) => add(String(r.year), 'y', r));
    return out;
  },
});

dash.calc('is_lines', {
  title: '利润表逐项对比',
  description: '利润表每一行在每个季度和年度的金额（百万美元，每股收益为美元）、占营收的比例，以及与去年同期（年度为上年相同月数）、上一季度相比的变化率；基数为负时不计算变化率。',
  inputs: ['income_statement', 'income_annual'],
  fn: (income_statement, income_annual) => {
    const lines = ['revenue', 'cost_of_sales', 'gross_margin', 'rnd', 'msa', 'acquired_iprd', 'impairment_restructuring', 'operating_income', 'other_net', 'pretax_income', 'income_tax', 'net_income', 'eps_diluted', 'ng_gross_margin', 'ng_operating_income', 'ng_net_income', 'ng_eps_diluted'];
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const prevQ = (q) => (q.endsWith('Q1') ? Number(q.slice(0, 4)) - 1 + 'Q4' : q.slice(0, 5) + (Number(q.slice(5)) - 1));
    const r4 = (x) => Math.round(x * 10000) / 10000;
    const rate = (v, base) => {
      const b = base == null || base === '' ? null : Number(base);
      return b != null && b > 0 && v >= 0 ? r4(v / b - 1) : null;
    };
    const out = [];
    const emit = (period, kind, r, py, pq) => {
      for (const line of lines) {
        if (r[line] === '' || r[line] == null) continue;
        const v = Number(r[line]);
        out.push({ period, kind, line, value: v, pct_rev: line.includes('eps') ? null : r4(v / Number(r.revenue)), yoy: py ? rate(v, py[line]) : null, qoq: pq ? rate(v, pq[line]) : null });
      }
    };
    const qBy = new Map(income_statement.map((r) => [r.quarter, r]));
    for (const r of [...income_statement].sort((a, b) => (a.quarter < b.quarter ? -1 : 1))) emit(r.quarter, 'q', r, qBy.get(lastYear(r.quarter)), qBy.get(prevQ(r.quarter)));
    const aBy = new Map(income_annual.map((r) => [`${String(r.year)}|${Number(r.months)}`, r]));
    for (const y of [...new Set(income_annual.map((r) => String(r.year)))].sort()) {
      const months = Math.max(...income_annual.filter((r) => String(r.year) === y).map((r) => Number(r.months)));
      const cur = aBy.get(`${y}|${months}`);
      if (cur) emit(y, 'y', cur, aBy.get(`${Number(y) - 1}|${months}`), null);
    }
    return out;
  },
});

dash.calc('bs_kpi', {
  title: '资产负债表核心指标',
  description: '每个季度末（年度取年末，当年未满四个季度时取最近一个季度末）的总资产、现金及短期投资、有息负债（短期借款加长期债务）、净负债、股东权益（亿美元），资产负债率、流动比率，以及与去年同期相比的变化；liab_ratio_pp 是资产负债率变化的百分点。',
  inputs: ['balance_sheet'],
  fn: (balance_sheet) => {
    const rows = [...balance_sheet].sort((a, b) => (a.quarter < b.quarter ? -1 : 1));
    const byQ = new Map(rows.map((r) => [r.quarter, r]));
    const v = (r, k) => Number(r[k]);
    const r4 = (x) => (x == null || !Number.isFinite(x) ? null : Math.round(x * 10000) / 10000);
    const r2 = (x) => Math.round(x * 100) / 100;
    const yi = (m) => Math.round(m / 10) / 10;
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const metrics = (r) => {
      const debt = v(r, 'short_term_debt') + v(r, 'long_term_debt');
      return { assets: v(r, 'total_assets'), cash: v(r, 'cash_and_sti'), debt, netDebt: debt - v(r, 'cash_and_sti'), equity: v(r, 'total_equity'), liab: v(r, 'total_liabilities') / v(r, 'total_assets'), current: v(r, 'total_current_assets') / v(r, 'total_current_liabilities') };
    };
    const growth = (a, b) => (b > 0 ? r4(a / b - 1) : null);
    const build = (period, kind, q) => {
      const c = metrics(byQ.get(q)), prior = byQ.get(lastYear(q)), p = prior ? metrics(prior) : null;
      return {
        period, kind, as_of: byQ.get(q).period_end,
        total_assets_yi: yi(c.assets), total_assets_yoy: p ? growth(c.assets, p.assets) : null,
        cash_yi: yi(c.cash), cash_yoy: p ? growth(c.cash, p.cash) : null,
        debt_yi: yi(c.debt), debt_yoy: p ? growth(c.debt, p.debt) : null,
        net_debt_yi: yi(c.netDebt),
        equity_yi: yi(c.equity), equity_yoy: p ? growth(c.equity, p.equity) : null,
        liab_ratio: r4(c.liab), liab_ratio_pp: p ? Math.round((c.liab - p.liab) * 1000) / 10 : null,
        current_ratio: r2(c.current), current_ratio_chg: p ? r2(c.current - p.current) : null,
      };
    };
    const out = rows.map((r) => build(r.quarter, 'q', r.quarter));
    for (const y of [...new Set(rows.map((r) => r.quarter.slice(0, 4)))]) {
      const qs = rows.filter((r) => r.quarter.startsWith(y)).map((r) => r.quarter);
      out.push(build(y, 'y', qs[qs.length - 1]));
    }
    return out;
  },
});

dash.calc('bs_lines', {
  title: '资产负债表逐项对比',
  description: '资产负债表各项在每个季度末（年度取年末，当年未满四个季度时取最近一个季度末）的金额（百万美元）、占总资产的比例，以及与去年同期、上一季度末相比的变化率；非流动资产、非流动负债由合计数相减得出。',
  inputs: ['balance_sheet'],
  fn: (balance_sheet) => {
    const lines = ['cash_and_sti', 'accounts_receivable', 'inventories', 'other_current_assets', 'total_current_assets', 'investments', 'goodwill_intangibles', 'ppe', 'other_noncurrent_assets', 'noncurrent_assets', 'total_assets', 'short_term_debt', 'accounts_payable', 'sales_rebates', 'other_current_liabilities', 'total_current_liabilities', 'long_term_debt', 'other_noncurrent_liabilities', 'noncurrent_liabilities', 'total_liabilities', 'total_equity', 'total_liabilities_equity'];
    const val = (r, line) => (line === 'noncurrent_assets' ? Number(r.total_assets) - Number(r.total_current_assets) : line === 'noncurrent_liabilities' ? Number(r.total_liabilities) - Number(r.total_current_liabilities) : Number(r[line]));
    const rows = [...balance_sheet].sort((a, b) => (a.quarter < b.quarter ? -1 : 1));
    const byQ = new Map(rows.map((r) => [r.quarter, r]));
    const lastYear = (q) => Number(q.slice(0, 4)) - 1 + q.slice(4);
    const prevQ = (q) => (q.endsWith('Q1') ? Number(q.slice(0, 4)) - 1 + 'Q4' : q.slice(0, 5) + (Number(q.slice(5)) - 1));
    const r1 = (x) => Math.round(x * 10) / 10, r4 = (x) => Math.round(x * 10000) / 10000;
    const rate = (v, b) => (b != null && b > 0 && v >= 0 ? r4(v / b - 1) : null);
    const out = [];
    const emit = (period, kind, q, withQoq) => {
      const r = byQ.get(q), py = byQ.get(lastYear(q)), pq = withQoq ? byQ.get(prevQ(q)) : null, ta = Number(r.total_assets);
      for (const line of lines) {
        const v = r1(val(r, line));
        out.push({ period, kind, as_of: r.period_end, line, value: v, pct_assets: r4(v / ta), yoy: py ? rate(v, val(py, line)) : null, qoq: pq ? rate(v, val(pq, line)) : null });
      }
    };
    rows.forEach((r) => emit(r.quarter, 'q', r.quarter, true));
    for (const y of [...new Set(rows.map((r) => r.quarter.slice(0, 4)))]) {
      const qs = rows.filter((r) => r.quarter.startsWith(y)).map((r) => r.quarter);
      emit(y, 'y', qs[qs.length - 1], false);
    }
    return out;
  },
});

dash.calc('event_counts', {
  title: '各类事件数量',
  description: '关键事件列表中每种类型的条数，以及全部事件的条数。',
  inputs: ['events'],
  fn: (events) => {
    const m = new Map();
    for (const e of events) m.set(e.type, (m.get(e.type) || 0) + 1);
    return [{ type: '全部', n: events.length }, ...[...m.entries()].map(([type, n]) => ({ type, n }))];
  },
});
