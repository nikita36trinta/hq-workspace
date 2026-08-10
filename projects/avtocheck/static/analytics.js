/* Переиспользуемый фронт аналитики (вендорится в каждый проект).
 * Подключение:
 *   <script>window.ANALYTICS_CONFIG={endpoint:'/api/goal',metrika:110382224};</script>
 *   <script src="/static/analytics.js"></script>
 * Использование: Analytics.goal('checkout_open',{...}); Analytics.startLoading(); Analytics.stopLoading();
 * Автоматически: сквозной device-id (localStorage), visit при загрузке, отвал на загрузке (beacon).
 */
(function () {
  var CFG = window.ANALYTICS_CONFIG || {};
  var EP = CFG.endpoint || '/api/goal';
  var FIELDS = CFG.fields || ['object', 'bump', 'shown', 'fields', 'sec', 'method', 'order',
                              'ref', 'err', 'part'];

  // сквозной persistent id (переживает сессии — для отложенной оплаты/retention)
  var DID;
  try {
    DID = localStorage.getItem('an_did');
    if (!DID) { DID = (Date.now().toString(36) + Math.random().toString(36).slice(2, 10)); localStorage.setItem('an_did', DID); }
  } catch (e) { DID = ''; }

  /* ── Заслон от ботов ─────────────────────────────────────────────────────
     Роботы, дёргающие ссылку объявления, исполняют скрипты и потому попадали
     в статистику как живые визиты: 48 сессий за ночь, все с одинаковым
     набором из трёх событий, парами через минуту, без единого касания. Они
     ломали ровно то, ради чего аналитика заведена, — знаменатель конверсии.

     По UA их не отсечь: заголовки честные, браузеры настоящие. Отличает их
     поведение — они уходят мгновенно и ничего не трогают. Поэтому события
     КОПИМ и отправляем только после признака человека: любое касание, клик,
     прокрутка, нажатие клавиши — либо просто четыре секунды на видимой вкладке.

     Живого посетителя это не теряет: даже беглый взгляд на страницу длиннее
     четырёх секунд, а очередь после подтверждения уходит целиком и в том же
     порядке. Робот, закрывший вкладку через секунду, не пришлёт ничего. */
  var HUMAN = false;
  var QUEUE = [];
  var HUMAN_MS = CFG.humanDelayMs || 4000;

  function flushQueue() {
    if (!HUMAN) return;
    var q = QUEUE; QUEUE = [];
    for (var i = 0; i < q.length; i++) rawSend(q[i]);
  }
  function confirmHuman() {
    if (HUMAN) return;
    HUMAN = true;
    flushQueue();
  }
  try {
    ['pointerdown', 'mousemove', 'touchstart', 'keydown', 'scroll', 'wheel'].forEach(
      function (ev) {
        window.addEventListener(ev, confirmHuman, { once: true, passive: true, capture: true });
      });
    // Тихий посетитель — тоже человек: читает и не трогает. Ждём, но только
    // пока вкладка видима, иначе фоновая вкладка робота «досидит» до порога.
    var waited = 0;
    var tick = setInterval(function () {
      if (document.visibilityState === 'visible') waited += 500;
      if (waited >= HUMAN_MS) { clearInterval(tick); confirmHuman(); }
    }, 500);
  } catch (e) { HUMAN = true; }   // нет DOM-событий — не теряем данные вовсе

  function rawSend(body) {
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(EP, new Blob([body], { type: 'application/json' }));
      } else {
        fetch(EP, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true, credentials: 'same-origin' }).catch(function () {});
      }
    } catch (e) {}
  }

  function send(payload) {
    payload.did = DID;
    try {
      var body = JSON.stringify(payload);
      if (HUMAN) { rawSend(body); return; }
      // Очередь не бесконечная: сломанный цикл на странице не должен съесть память.
      if (QUEUE.length < 60) QUEUE.push(body);
    } catch (e) {}
  }

  function goal(name, params) {
    params = params || {};
    // Яндекс.Метрика (если подключена)
    try { if (CFG.metrika && typeof ym === 'function') ym(CFG.metrika, 'reachGoal', name, params); } catch (e) {}
    var p = { name: name };
    FIELDS.forEach(function (k) { if (params[k] !== undefined) p[k] = params[k]; });
    send(p);
    if (CFG.debug) console.log('[analytics]', name, params);
  }

  // ---- отвал на загрузке: heartbeat (backbone) + финальный beacon (precision) ----
  // Heartbeat: пока экран загрузки ВИДЕН, шлём пинг с секундой раз в HB_MS. Сервер берёт
  // last-seen → секунду ухода видно, даже если финальный beacon не дошёл (мобильный дроп,
  // отошёл-не-закрыл). Финальный beacon на visibilitychange('hidden')/pagehide уточняет
  // последний интервал, когда браузер успевает. beforeunload/unload НЕ используем (ненадёжны,
  // ломают bfcache). См. ресёрч 2026-07 (best-practice: heartbeat + visibilitychange).
  var loaderStart = 0, loaderActive = false, sentAbandon = false, hbTimer = null;
  var HB_EVENT = CFG.heartbeatEvent || 'check_heartbeat';
  var HB_MS = CFG.heartbeatMs || 5000;
  function elapsedSec() { return Math.round((Date.now() - loaderStart) / 1000); }
  function ping(name, sec) {  // как goal, но БЕЗ Метрики (heartbeat не должен спамить цели)
    send({ name: name, sec: sec });
  }
  function startLoading() {
    loaderStart = Date.now(); loaderActive = true; sentAbandon = false;
    if (hbTimer) clearInterval(hbTimer);
    hbTimer = setInterval(function () {
      if (!loaderActive) return;
      if (document.visibilityState !== 'visible') return;  // пингуем, только пока смотрят
      ping(HB_EVENT, elapsedSec());
    }, HB_MS);
  }
  function stopLoading() {
    loaderActive = false;
    if (hbTimer) { clearInterval(hbTimer); hbTimer = null; }
  }
  function abandonNow() {
    if (sentAbandon || !loaderActive || !loaderStart) return;
    sentAbandon = true;
    goal(CFG.abandonEvent || 'check_abandoned', { sec: elapsedSec() });
  }
  window.addEventListener('pagehide', abandonNow);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') abandonNow();
    else if (loaderActive) sentAbandon = false;  // вернулся — разрешаем зафиксировать новый уход
  });

  // ---- JS-ошибки ----
  // Раньше сломанный на конкретном браузере скрипт был виден только как «этот сегмент
  // почему-то не платит»: страница молча переставала работать, а мы гадали по конверсии.
  // Шлём не больше трёх ошибок на загрузку — иначе цикл в чужом коде затопит журнал.
  var errLeft = 3;
  /* Домены счётчиков и рекламных пикселей. Их падение — НЕ ошибка сайта: у
     четырёх посетителей из пяти блокировщик режет mc.yandex.ru, и метрика
     «ошибка JS» была забита этим на 100%. Настоящая ошибка в нашем коде в
     таком шуме просто не видна. Считаем отдельно: заодно получаем честную
     долю блокировки, на которую надо поправлять данные Метрики. */
  var TRACKERS = /(^|\.)(mc\.yandex\.ru|yandex\.ru\/metrika|google-analytics\.com|googletagmanager\.com|top-fwz1\.mail\.ru|vk\.com\/rtrg|ads\.|analytics\.)/i;
  function isTracker(src) {
    try { return TRACKERS.test(String(src || '')); } catch (e) { return false; }
  }
  var trackerReported = false;
  function reportError(where, msg, src, line) {
    if (where === 'load' && isTracker(src)) {
      if (trackerReported) return;      // один раз на загрузку, а не по каждому пикселю
      trackerReported = true;
      var host = '';
      try { host = new URL(String(src), location.href).hostname; } catch (e) {}
      goal('tracker_blocked', { host: host.slice(0, 60) });
      return;
    }
    if (errLeft-- <= 0) return;
    var s = (src || '').split('/').pop().split('?')[0];
    goal('js_error', { err: (where + ': ' + msg + ' @' + s + ':' + (line || 0)).slice(0, 200) });
  }
  window.addEventListener('error', function (e) {
    if (!e) return;
    // ошибки загрузки картинок/скриптов приходят без message — их отделяем
    if (e.message) reportError('js', e.message, e.filename, e.lineno);
    else if (e.target && e.target.src) reportError('load', 'ресурс не загрузился', e.target.src, 0);
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    reportError('promise', (r && (r.message || r)) + '', '', 0);
  });

  window.Analytics = { goal: goal, startLoading: startLoading, stopLoading: stopLoading, abandonNow: abandonNow, did: DID };

  // Визит — первый шаг воронки (один раз на загрузку). С ним же уходит источник
  // перехода: только ХОСТ, без пути и параметров — путь чужого сайта нам не нужен,
  // а в параметрах могут оказаться чужие персональные данные.
  var ref = '';
  try {
    if (document.referrer) {
      var h = new URL(document.referrer).hostname;
      if (h && h !== location.hostname) ref = h.replace(/^www\./, '');
    }
  } catch (e) {}
  try { goal(CFG.visitEvent || 'visit', ref ? { ref: ref } : {}); } catch (e) {}
})();
