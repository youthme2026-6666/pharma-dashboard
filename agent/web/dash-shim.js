/* 独立网页版的 dash 运行时：实现看板页面用到的 dash.data / calc / draw / onData / params / setParams / colors。
 * 行为与 Claude 看板一致：CSV 单元格是字符串、空格子是 null；计算按需求值并缓存；参数变化时重画全部图形。
 * 参数保存在网址 # 后面，链接可以直接分享。 */
(function () {
  'use strict';
  // 数据由 data 脚本写入 window.__DASH_DATA__；每次读取时再取，脚本顺序不影响。
  function D() { return window.__DASH_DATA__ || {}; }
  function has(id) { return Object.prototype.hasOwnProperty.call(D(), id); }
  var calcs = new Map(), cache = new Map(), draws = new Map(), dataCbs = [];
  var started = false, pending = false;
  var params = readHash();

  function readHash() {
    var out = {};
    try { new URLSearchParams(location.hash.slice(1)).forEach(function (v, k) { out[k] = v; }); } catch (e) { /* 忽略 */ }
    return out;
  }
  function writeHash() {
    var s = new URLSearchParams(params).toString();
    try { history.replaceState(null, '', s ? '#' + s : location.pathname + location.search); } catch (e) { /* 忽略 */ }
  }
  function copyRows(id) { return D()[id].map(function (r) { return Object.assign({}, r); }); }
  function report(where, err) {
    console.error('[dash] ' + where + ':', err);
    var box = document.getElementById('dash-errors');
    if (box) { box.hidden = false; box.textContent = '页面代码出错（' + where + '）：' + (err && err.message ? err.message : err); }
  }
  function evalCalc(id, stack) {
    if (cache.has(id)) return cache.get(id);
    var def = calcs.get(id), out;
    if (stack.indexOf(id) >= 0) return { status: 'error', message: '计算循环引用：' + stack.concat(id).join(' → ') };
    try {
      var args = (def.inputs || []).map(function (i) {
        var d = has(i) ? { status: 'ok', value: copyRows(i) } : calcs.has(i) ? evalCalc(i, stack.concat(id)) : { status: 'missing' };
        if (d.status !== 'ok') throw new Error('输入 ' + i + ' 不可用');
        return d.value;
      });
      out = { status: 'ok', value: def.fn.apply(null, args) };
    } catch (e) {
      report('dash.calc("' + id + '")', e);
      out = { status: 'error', message: String(e && e.message || e) };
    }
    cache.set(id, out);
    return out;
  }
  function runDraw(name) {
    var fn = draws.get(name);
    document.querySelectorAll('[data-draw="' + name + '"]').forEach(function (el) {
      try { fn(el); } catch (e) { report('dash.draw("' + name + '")', e); }
    });
  }
  function redraw() {
    if (!started) return;
    dataCbs.forEach(function (fn) { try { fn(); } catch (e) { report('dash.onData', e); } });
    draws.forEach(function (_, name) { runDraw(name); });
  }
  function schedule() {
    if (pending) return;
    pending = true;
    setTimeout(function () { pending = false; redraw(); }, 0);
  }

  window.dash = {
    data: function (id) {
      if (has(id)) {
        var r = copyRows(id);
        return { status: 'ok', data: r, rows: r, columns: Object.keys(D()[id][0] || {}), message: '', refreshing: false, meta: [] };
      }
      if (calcs.has(id)) {
        var c = evalCalc(id, []);
        return { status: c.status, data: c.value, rows: [], columns: [], message: c.message || '', refreshing: false, meta: [], value: c.value };
      }
      return { status: 'missing', data: [], rows: [], columns: [], message: '没有数据 “' + id + '”', refreshing: false, meta: [] };
    },
    calc: function (name, def) { calcs.set(name, def || {}); cache.clear(); if (started) schedule(); },
    loader: function () { /* 独立网页版不需要 */ },
    draw: function (name, fn) {
      if (typeof fn === 'object' && fn && typeof fn.update === 'function') fn = fn.update;
      if (typeof fn !== 'function') return;
      draws.set(name, fn);
      if (started) runDraw(name);
    },
    onData: function (fn) { if (typeof fn === 'function') { dataCbs.push(fn); if (started) fn(); } },
    params: function () { return Object.assign({}, params); },
    setParams: function (p) {
      if (!p) { params = {}; } else {
        Object.keys(p).forEach(function (k) {
          var v = p[k];
          if (v == null || v === '') delete params[k]; else params[k] = String(v);
        });
      }
      writeHash();
      schedule();
    },
    resetParams: function () { params = {}; writeHash(); schedule(); },
    refresh: function () {},
    setLink: function () {},
    colors: [1, 2, 3, 4, 5, 6, 7, 8].map(function (i) { return 'var(--cds-chart-categorical-' + i + ')'; }),
    __start: function () { started = true; redraw(); }
  };

  window.addEventListener('hashchange', function () { params = readHash(); schedule(); });
  var lastW = window.innerWidth, t = null;
  window.addEventListener('resize', function () {
    if (window.innerWidth === lastW) return;
    lastW = window.innerWidth;
    clearTimeout(t);
    t = setTimeout(redraw, 150);
  });
})();
