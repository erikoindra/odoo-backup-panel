(function () {
  // ---- Instance list: filter rows, and count them by health -------------------------------------
  var query = document.querySelector('[data-search]');
  var count = document.getElementById('result-count');
  var healthCount = document.getElementById('health-count');
  var LEVELS = [
    ['fresh', 'fresh'], ['stale', 'stale'], ['empty', 'no backups'],
    ['stopped', 'stopped'], ['unreadable', 'unreadable'], ['loading', 'loading']
  ];

  function applyFilter() {
    if (!query) { return; }
    var value = query.value.trim().toLowerCase();
    var rows = 0;
    var servers = 0;
    document.querySelectorAll('[data-group]').forEach(function (group) {
      var groupMatch = value === '' || group.getAttribute('data-group').toLowerCase().indexOf(value) !== -1;
      var visible = 0;
      group.querySelectorAll('[data-row]').forEach(function (row) {
        row.hidden = !groupMatch && row.textContent.toLowerCase().indexOf(value) === -1;
        if (!row.hidden) { visible++; }
      });
      group.hidden = !groupMatch && visible === 0;
      if (!group.hidden) { servers++; rows += visible; }
    });
    if (count) { count.textContent = rows + (rows === 1 ? ' instance on ' : ' instances on ') + servers + (servers === 1 ? ' server' : ' servers'); }
    tally();
  }

  function tally() {
    if (!healthCount) { return; }
    var parts = [];
    LEVELS.forEach(function (level) {
      var n = document.querySelectorAll('[data-row][data-level="' + level[0] + '"]:not([hidden])').length;
      if (n) { parts.push(n + ' ' + level[1]); }
    });
    healthCount.textContent = parts.length ? '· ' + parts.join(' · ') : '';
  }

  if (query) { query.addEventListener('input', applyFilter); }
  document.body.addEventListener('htmx:afterSettle', function (event) {
    if (event.target && event.target.hasAttribute && event.target.hasAttribute('data-row')) { applyFilter(); }
  });
  applyFilter();

  // ---- A fragment that could not be loaded at all (network, server error): say so ---------------
  function failed(event) {
    var target = event.detail && event.detail.target;
    if (!target) { return; }
    var banner = '<div class="alert alert--bad" role="status"><div class="alert__body">' +
      '<p>Could not load this. <a href="">Reload the page</a></p></div></div>';
    if (target.tagName === 'TR') {
      target.innerHTML = '<td colspan="6"><span class="status status--bad">Unreadable</span>' +
        '<span class="reason">Could not load the status. <a href="">Reload the page</a></span></td>';
      target.setAttribute('data-level', 'unreadable');
      tally();
    } else {
      target.innerHTML = banner;
    }
  }
  document.body.addEventListener('htmx:responseError', failed);
  document.body.addEventListener('htmx:sendError', failed);

  // ---- Elapsed time of a running job -----------------------------------------------------------
  function duration(seconds) {
    if (seconds < 60) { return seconds + ' s'; }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) { return minutes + ' min' + (seconds % 60 ? ' ' + (seconds % 60) + ' s' : ''); }
    return Math.floor(minutes / 60) + ' h ' + (minutes % 60) + ' min';
  }
  setInterval(function () {
    document.querySelectorAll('[data-since]').forEach(function (el) {
      var started = Date.parse(el.getAttribute('data-since'));
      if (!isNaN(started)) { el.textContent = duration(Math.max(0, Math.round((Date.now() - started) / 1000))); }
    });
  }, 1000);

  // ---- Dismiss buttons and the toast -----------------------------------------------------------
  var toast = document.getElementById('toast');
  var toastTimer = null;
  function showToast(title, detail) {
    if (!toast) { return; }
    toast.textContent = '';
    var strong = document.createElement('strong');
    strong.textContent = title;
    toast.appendChild(strong);
    if (detail) {
      var small = document.createElement('span');
      small.className = 'mono';
      small.textContent = detail;
      toast.appendChild(small);
    }
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 5000);
  }

  document.addEventListener('click', function (event) {
    var dismiss = event.target.closest('[data-dismiss]');
    if (dismiss) {
      var target = document.querySelector(dismiss.getAttribute('data-dismiss'));
      if (target) { target.remove(); }
      return;
    }
    var download = event.target.closest('[data-download]');
    if (download) { showToast('Download started', download.getAttribute('data-download')); }
  });

  // ---- Restore wizard: progressive enhancement over a form that works without script ------------
  var wizard = document.getElementById('wizard');
  if (!wizard) { return; }
  wizard.setAttribute('data-js', '');

  var steps = [].slice.call(wizard.querySelectorAll('.step'));
  var stepper = [].slice.call(document.querySelectorAll('.stepper li'));
  var newDb = wizard.elements.new_db;
  var confirmDb = wizard.elements.confirm;
  var neutralize = wizard.elements.neutralize;
  var radios = [].slice.call(wizard.querySelectorAll('input[name="filename"]'));
  var needsChecks = wizard.querySelector('[data-needs-checks]');
  var checks = document.getElementById('checks');
  var current = 1;
  var dirty = false;
  var recheckTimer = null;

  function selected() { return radios.filter(function (r) { return r.checked; })[0]; }
  function nameOk() { return /^[a-z0-9_]{1,63}$/.test(newDb.value.trim()); }
  function setError(id, field, message) {
    document.getElementById(id).textContent = message;
    if (message) { field.setAttribute('aria-invalid', 'true'); } else { field.removeAttribute('aria-invalid'); }
  }

  function fillSummary() {
    var chosen = selected();
    var values = {
      filename: chosen ? chosen.value : '',
      new_db: newDb.value.trim(),
      neutralize: neutralize.checked ? 'yes' : 'no'
    };
    wizard.querySelectorAll('[data-sum]').forEach(function (el) { el.textContent = values[el.getAttribute('data-sum')]; });
    confirmDb.placeholder = values.new_db;
  }

  function runChecks() {
    needsChecks.disabled = true;
    if (!nameOk()) {
      setError('new-db-error', newDb, 'Only lowercase letters, digits and underscores (max 63).');
      checks.innerHTML = '<p class="checks--wait">Enter a valid database name, then the checks run.</p>';
      return;
    }
    setError('new-db-error', newDb, '');
    checks.innerHTML = '<p class="checks--wait">Checking…</p>';
    htmx.trigger(document.getElementById('run-checks'), 'recheck');
  }

  function show(n, initial) {
    current = n;
    steps.forEach(function (step) { step.setAttribute('data-open', String(+step.getAttribute('data-step') === n)); });
    stepper.forEach(function (item, i) {
      item.setAttribute('data-active', String(i + 1 === n));
      item.setAttribute('data-done', String(i + 1 < n));
      if (i + 1 === n) { item.setAttribute('aria-current', 'step'); } else { item.removeAttribute('aria-current'); }
    });
    if (n === 2) { runChecks(); }
    if (n === 3) { fillSummary(); if (!initial) { confirmDb.focus(); } } else if (!initial) {
      var heading = steps[n - 1].querySelector('h2');
      heading.setAttribute('tabindex', '-1');
      heading.focus();
    }
    if (!initial) { window.scrollTo(0, 0); }
  }

  wizard.addEventListener('click', function (event) {
    var go = event.target.closest('[data-go]');
    if (!go) { return; }
    var n = +go.getAttribute('data-go');
    if (n === 3 && (needsChecks.disabled || !nameOk())) { return; }
    show(n);
  });

  document.body.addEventListener('htmx:afterSwap', function (event) {
    if (event.detail.target !== checks) { return; }
    var list = checks.querySelector('[data-all-ok]');
    needsChecks.disabled = !(list && list.getAttribute('data-all-ok') === 'true');
  });

  function invalidate() {
    needsChecks.disabled = true;
    clearTimeout(recheckTimer);
    if (current === 2) { recheckTimer = setTimeout(runChecks, 600); }
  }
  newDb.addEventListener('input', function () { dirty = true; invalidate(); });
  neutralize.addEventListener('change', invalidate);
  radios.forEach(function (radio) {
    radio.addEventListener('change', function () {
      if (!dirty) { newDb.value = radio.getAttribute('data-suggest') || newDb.value; }
    });
  });
  confirmDb.addEventListener('input', function () { setError('confirm-error', confirmDb, ''); });

  wizard.addEventListener('submit', function (event) {
    if (confirmDb.value.trim() !== newDb.value.trim() || !nameOk()) {
      event.preventDefault();
      setError('confirm-error', confirmDb, 'Type the database name exactly to continue.');
      confirmDb.focus();
    }
  });

  var start = +wizard.getAttribute('data-start') || 1;
  dirty = start !== 1;
  show(start, true);
})();
