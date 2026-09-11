(function () {
  'use strict';

  function getCookie(name) {
    var escaped = name.replace(/([.$?*|{}()[\]\\\/+^])/g, '\\$1');
    var match = document.cookie.match(new RegExp('(?:^|; )' + escaped + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function csrftoken() {
    return getCookie('csrftoken') || getCookie('csrf') || '';
  }

  async function request(url, options) {
    var opts = Object.assign({
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
    }, options || {});
    var resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      throw new Error('Network error. Please try again.');
    }
    var data = {};
    try { data = await resp.json(); } catch (e) { /* non-JSON response */ }
    if (!resp.ok) {
      var err = new Error((data && data.error) || 'Request failed (' + resp.status + ').');
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function pad(n) { return String(n).padStart(2, '0'); }

  function fmtDate(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function money(v, cur) {
    var num = Number(v);
    var amount = isNaN(num) ? '0.00' : num.toFixed(2);
    return amount + (cur ? ' ' + cur : '');
  }

  function stateBadgeClass(state) {
    if (['sale', 'confirmed', 'done'].indexOf(state) >= 0) return 'badge-success';
    if (['cancel', 'canceled'].indexOf(state) >= 0) return 'badge-danger';
    if (state === 'sent') return 'badge-warning';
    return 'badge-info';
  }

  function stateBadgeLabel(state) {
    return state || 'unknown';
  }

  function initUserDropdown() {
    var root = document.querySelector('[data-user-dropdown]');
    if (!root) return;
    var toggle = root.querySelector('[data-dropdown-toggle]');
    var menu = root.querySelector('.nav-dropdown-menu');
    if (!toggle || !menu) return;
    function setOpen(open) {
      root.classList.toggle('open', open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        var first = menu.querySelector('a, button');
        if (first) first.focus();
      }
    }
    toggle.addEventListener('click', function (event) {
      event.stopPropagation();
      setOpen(!root.classList.contains('open'));
    });
    document.addEventListener('click', function (event) {
      if (!root.contains(event.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') setOpen(false);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUserDropdown);
  } else {
    initUserDropdown();
  }

  window.HYPAX = {
    api: {
      get: function (url) { return request(url, { method: 'GET' }); },
      post: function (url, payload) {
        return request(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken() },
          body: JSON.stringify(payload || {}),
        });
      },
    },
    csrftoken: csrftoken,
    fmtDate: fmtDate,
    money: money,
    stateBadgeClass: stateBadgeClass,
    stateBadgeLabel: stateBadgeLabel,
    setLoading: function (vm, key, label) {
      vm['loading_' + key] = true;
      if (label !== null && typeof label !== 'undefined') vm['loadingLabel_' + key] = label;
    },
  };
})();