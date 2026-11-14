/* 授权服务后台 · 管理台公共脚本
 * 约定：
 *   1. 所有请求统一走 api()（自动带 Cookie、自动带 X-CSRF-Token、401 跳登录）；
 *   2. 绝不使用 innerHTML 插入用户数据（统一 textContent / DOM 构造），防存储型 XSS；
 *   3. 时间字段统一为带 +08:00 的 ISO 字符串，前端只负责格式化展示。
 */
(function (global) {
  'use strict';

  var CSRF_KEY = 'ls_csrf_token';
  var USER_KEY = 'ls_user';

  // ---------------------------------------------------------------------
  // 会话状态
  // ---------------------------------------------------------------------
  function getCsrf() {
    return sessionStorage.getItem(CSRF_KEY) || '';
  }

  function setCsrf(token) {
    if (token) {
      sessionStorage.setItem(CSRF_KEY, token);
    }
  }

  function getUser() {
    try {
      return JSON.parse(sessionStorage.getItem(USER_KEY) || 'null');
    } catch (e) {
      return null;
    }
  }

  function clearAuth() {
    sessionStorage.removeItem(CSRF_KEY);
    sessionStorage.removeItem(USER_KEY);
  }

  function redirectToLogin() {
    var here = location.pathname.split('/').pop() || 'index.html';
    if (here === 'login.html') { return; }
    location.href = 'login.html?next=' + encodeURIComponent(here + location.search);
  }

  // ---------------------------------------------------------------------
  // 错误解析
  // ---------------------------------------------------------------------
  function errorMessage(data, status) {
    if (data && data.error && data.error.message) {
      return data.error.message;
    }
    if (data && data.detail) {
      if (typeof data.detail === 'string') { return data.detail; }
      if (data.detail.error && data.detail.error.message) {
        return data.detail.error.message;
      }
    }
    if (status === 404) { return '接口不存在（服务版本可能不匹配）'; }
    if (status === 429) { return '请求过于频繁，请稍后再试'; }
    return '请求失败（HTTP ' + status + '）';
  }

  function makeError(message, code, status) {
    var err = new Error(message);
    err.code = code || '';
    err.status = status || 0;
    return err;
  }

  // ---------------------------------------------------------------------
  // 请求封装
  // ---------------------------------------------------------------------
  async function api(method, path, body) {
    var opt = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined && body !== null) {
      opt.headers['Content-Type'] = 'application/json; charset=utf-8';
      opt.body = JSON.stringify(body);
    }
    var csrf = getCsrf();
    if (csrf) { opt.headers['X-CSRF-Token'] = csrf; }

    var resp;
    try {
      resp = await fetch(path, opt);
    } catch (e) {
      throw makeError('网络异常，请确认授权服务是否正常运行', 'network', 0);
    }
    var text = await resp.text();
    var data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    if (resp.status === 401) {
      clearAuth();
      redirectToLogin();
      throw makeError('登录状态已失效，正在跳转登录页', 'unauthorized', 401);
    }
    var errCode = (data && data.error && data.error.code) || '';
    if (errCode === 'must_change_password') {
      location.href = 'account.html';
      throw makeError('请先修改初始密码', errCode, 403);
    }
    if (!resp.ok || (data && data.ok === false)) {
      throw makeError(errorMessage(data, resp.status), errCode, resp.status);
    }
    return data || {};
  }

  function apiGet(path) { return api('GET', path); }
  function apiPost(path, body) { return api('POST', path, body === undefined ? {} : body); }

  // ---------------------------------------------------------------------
  // DOM 构造（禁止 innerHTML）
  // ---------------------------------------------------------------------
  function h(tag, attrs) {
    var el = document.createElement(tag);
    var a = attrs || {};
    Object.keys(a).forEach(function (key) {
      var value = a[key];
      if (value === null || value === undefined || value === false) { return; }
      if (key === 'class') { el.className = value; }
      else if (key === 'text') { el.textContent = String(value); }
      else if (key === 'html') { /* 显式禁止：忽略 */ }
      else if (key.slice(0, 2) === 'on' && typeof value === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) { el.setAttribute(key, ''); }
      else { el.setAttribute(key, String(value)); }
    });
    for (var i = 2; i < arguments.length; i++) {
      append(el, arguments[i]);
    }
    return el;
  }

  function append(parent, kid) {
    if (kid === null || kid === undefined || kid === false) { return; }
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(parent, k); }); return; }
    parent.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }

  function esc(value) {
    return String(value === null || value === undefined ? '' : value);
  }

  // ---------------------------------------------------------------------
  // 时间格式化
  // ---------------------------------------------------------------------
  function fmtTime(value) {
    if (!value) { return '-'; }
    var d = new Date(value);
    if (isNaN(d.getTime())) { return String(value); }
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function fmtDay(value) {
    if (!value) { return '-'; }
    var d = new Date(value);
    if (isNaN(d.getTime())) { return String(value); }
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  function daysLeft(value) {
    if (!value) { return null; }
    var d = new Date(value);
    if (isNaN(d.getTime())) { return null; }
    return Math.floor((d.getTime() - Date.now()) / 86400000);
  }

  // ---------------------------------------------------------------------
  // 提示
  // ---------------------------------------------------------------------
  function toast(message, isError) {
    var box = document.getElementById('toast');
    if (!box) {
      box = h('div', { id: 'toast', class: 'toast' });
      document.body.append(box);
    }
    box.className = 'toast show' + (isError ? ' error' : '');
    box.textContent = String(message || '');
    clearTimeout(box._timer);
    box._timer = setTimeout(function () { box.className = 'toast'; }, 3600);
  }

  function showError(err) {
    toast(err && err.message ? err.message : String(err), true);
  }

  // ---------------------------------------------------------------------
  // 表格 / 分页
  // ---------------------------------------------------------------------
  function table(headers, rows) {
    var thead = h('thead', null, h('tr', null, headers.map(function (t) {
      return h('th', { text: t });
    })));
    var tbody = h('tbody');
    if (!rows || !rows.length) {
      tbody.append(h('tr', null, h('td', {
        class: 'empty', colspan: String(headers.length), text: '暂无数据'
      })));
    } else {
      rows.forEach(function (cells) {
        var tr = h('tr');
        cells.forEach(function (cell) {
          if (cell instanceof Node) { tr.append(cell); }
          else {
            var value = (cell === null || cell === undefined || cell === '') ? '-' : cell;
            tr.append(h('td', { text: String(value) }));
          }
        });
        tbody.append(tr);
      });
    }
    return h('table', { class: 'grid' }, thead, tbody);
  }

  function pager(container, state, onGo) {
    container.replaceChildren();
    var total = state.total || 0;
    var size = state.size || 20;
    var page = state.page || 1;
    var pages = Math.max(1, Math.ceil(total / size));
    container.append(h('span', { class: 'hint', text: '共 ' + total + ' 条 / ' + pages + ' 页' }));
    var prev = h('button', {
      class: 'btn ghost', text: '上一页',
      onclick: function () { if (page > 1) { onGo(page - 1); } }
    });
    var next = h('button', {
      class: 'btn ghost', text: '下一页',
      onclick: function () { if (page < pages) { onGo(page + 1); } }
    });
    if (page <= 1) { prev.disabled = true; }
    if (page >= pages) { next.disabled = true; }
    container.append(prev);
    container.append(h('span', { class: 'hint', text: '第 ' + page + ' 页' }));
    container.append(next);
  }

  function badge(text, kind) {
    return h('span', { class: 'badge ' + (kind || ''), text: text });
  }

  var ROLE_LABEL = {
    active: ['当前生效', 'ok'],
    expired: ['已过期', 'warn'],
    revoked: ['已吊销', 'danger'],
    superseded: ['已被取代', 'muted']
  };

  function roleBadge(role) {
    var meta = ROLE_LABEL[role] || [role || '-', 'muted'];
    return badge(meta[0], meta[1]);
  }

  // ---------------------------------------------------------------------
  // 导航
  // ---------------------------------------------------------------------
  var NAV_ITEMS = [
    { id: 'index', href: 'index.html', text: '仪表盘' },
    { id: 'codes', href: 'codes.html', text: '激活码' },
    { id: 'licenses', href: 'licenses.html', text: '许可证' },
    { id: 'customers', href: 'customers.html', text: '客户档案' },
    { id: 'audit', href: 'audit.html', text: '审计日志' },
    { id: 'account', href: 'account.html', text: '账号' }
  ];

  function renderNav(active, user) {
    var nav = document.getElementById('nav');
    if (!nav) { return; }
    nav.replaceChildren();
    nav.append(h('div', { class: 'brand', text: '授权服务后台' }));
    var links = h('div', { class: 'links' });
    NAV_ITEMS.forEach(function (item) {
      links.append(h('a', {
        href: item.href,
        class: 'nav-link' + (item.id === active ? ' active' : ''),
        text: item.text
      }));
    });
    nav.append(links);
    var right = h('div', { class: 'nav-right' });
    right.append(h('span', {
      class: 'hint',
      text: user ? (user.display_name || user.username) : ''
    }));
    right.append(h('button', {
      class: 'btn ghost', text: '退出',
      onclick: async function () {
        try { await apiPost('/admin/api/logout'); } catch (e) { /* 忽略 */ }
        clearAuth();
        location.href = 'login.html';
      }
    }));
    nav.append(right);
  }

  // ---------------------------------------------------------------------
  // 页面启动：校验会话 → 渲染导航 → 回调
  // ---------------------------------------------------------------------
  async function bootPage(active) {
    renderNav(active, getUser());
    var me = null;
    try {
      me = await api('GET', '/admin/api/me');
    } catch (e) {
      return null;      // 401 时 api() 已跳转登录页
    }
    setCsrf(me.csrf_token);
    sessionStorage.setItem(USER_KEY, JSON.stringify(me.user));
    renderNav(active, me.user);
    if (me.user.must_change_password && active !== 'account') {
      location.href = 'account.html';
      return null;
    }
    return me.user;
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) { el.textContent = value === null || value === undefined ? '-' : String(value); }
  }

  function val(id) {
    var el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
  }

  global.LS = {
    api: api,
    apiGet: apiGet,
    apiPost: apiPost,
    h: h,
    esc: esc,
    table: table,
    pager: pager,
    badge: badge,
    roleBadge: roleBadge,
    fmtTime: fmtTime,
    fmtDay: fmtDay,
    daysLeft: daysLeft,
    toast: toast,
    showError: showError,
    bootPage: bootPage,
    getCsrf: getCsrf,
    setCsrf: setCsrf,
    getUser: getUser,
    clearAuth: clearAuth,
    redirectToLogin: redirectToLogin,
    renderNav: renderNav,
    setText: setText,
    val: val
  };
}(window));
