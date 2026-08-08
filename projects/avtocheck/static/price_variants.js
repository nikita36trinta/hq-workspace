/* Варианты ЭКРАНА ЦЕНЫ — визуальный A/B/C (раунд 4, 05.08.2026).
 *
 * Плечо приходит с сервера в window.CS_VARIANT (кука cs_v2), цена у всех одна.
 *   A — текущий экран, рисуется боевыми wizFinal/wizObjectOnly (здесь его нет)
 *   B — «заключение уже создано»: бланк с номером ждёт открытия
 *   C — «он знает — вы нет»: что известно продавцу и что известно покупателю
 *
 * Шаг выбран не наугад: до экрана цены доходит меньше половины, а нажимает
 * оплату примерно каждый пятый из дошедших — это самая дорогая потеря воронки.
 *
 * Данные берём те же, что видит человек в превью (window._objRaw и мастер), и
 * ничего не выдумываем: если объекта нет — вариант обязан это пережить и не
 * обещать того, чего мы не проверяли.
 */
(function () {
  'use strict';

  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[<>&"]/g, function (c) {
      return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c];
    });
  };
  var money = function (n) {
    return Math.round(Number(n) || 0).toLocaleString('ru-RU') + ' ₽';
  };
  var num = function (n) { return String(n).replace('.', ','); };

  function data() {
    var o = window._objRaw || {};
    var wiz = window._wiz || {};
    var isObj = !!(window._wiz && window._wiz.objectOnly) ||
                (window.currentTariff === 'object');
    var price = isObj ? (window.CS_OBJECT_PRICE || 199) : (window.CS_PRICE || 449);
    return {
      vin: o.vin || window._lastObjectRef || '',
      marka: o.marka || '', model: o.model || '', year: o.year || '',
      engine: o.engine || '', body: o.body || '',
      found: !!o.marka,
      email: wiz.email || '',
      isObj: isObj, price: price,
      reportId: window._lastReportId || ''
    };
  }

  /* Строка-подпись под заголовком: «BMW X1 18d» либо сам VIN. Пустую строку не
     отдаём — она рисует заголовок с болтающимся разделителем. */
  function carLine(d) {
    var name = [d.marka, d.model].filter(Boolean).join(' ');
    return name || d.vin || 'автомобиль не опознан';
  }

  /* Шапка со знаком сервиса — как на текущем экране, чтобы плечи отличались
     содержанием, а не фирменным стилем. */
  function head(title, sub) {
    var hex = (typeof hexMark === 'function') ? hexMark(38) : '';
    return '<div style="display:flex;align-items:center;gap:11px;margin-bottom:13px">' + hex +
      '<div><h3 style="margin:0;font-size:18.5px;letter-spacing:-.015em;font-weight:800">' + title + '</h3>' +
      (sub ? '<p style="margin:2px 0 0;font-size:13px;color:#6b7280">' + sub + '</p>' : '') +
      '</div></div>';
  }

  /* Низ карточки — тот же, что у контрольного экрана: гарантия, правовая
     сноска, кнопка, дешёвый выход и знак оплаты. Если плечи будут отличаться
     ещё и обвязкой, тест перестанет измерять то, ради чего затеян. */
  function foot(d, label, cls) {
    var consent = (typeof consentLine === 'function') ? consentLine() : '';
    var yk = (typeof YK_BADGE === 'string') ? YK_BADGE : '';
    var guarantee = window.CS_GUARANTEE
      ? '<div style="display:flex;gap:8px;align-items:flex-start;background:#f0fdf4;border:1px solid #bbf7d0;' +
        'border-radius:10px;padding:9px 11px;margin:12px 0 4px">' +
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2" style="flex:0 0 auto;margin-top:1px">' +
        '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>' +
        '<div style="font-size:11.5px;color:#166534"><b>Гарантия.</b> Не смогли проверить по реестрам — вернём деньги.</div></div>'
      : '';
    var payFn = d.isObj ? 'wizPayObject()' : 'wizPay()';
    var errId = d.isObj ? 'wizErrObj' : 'wizErr';
    var fallback = d.isObj
      ? '<div style="text-align:center;margin-top:10px"><a href="#" ' +
        'onclick="window._wiz.objectOnly=false;wizFinal();return false" ' +
        'style="font-size:12px;color:#6b7280;text-decoration:none">← Полный отчёт — вся история машины</a></div>'
      : '<div style="text-align:center;margin-top:11px;padding-top:11px;border-top:1px solid #dbeafe">' +
        '<div style="font-size:12px;color:#6b7280;margin-bottom:7px">Нужны только стоп-факторы?</div>' +
        '<button type="button" onclick="wizObjectOnly()" style="width:100%;background:#fff;color:#2563eb;' +
        'border:1.5px solid #bfdbfe;border-radius:11px;padding:11px;font-size:14px;font-weight:700;' +
        'cursor:pointer;font-family:inherit">Залог, ограничения, розыск — ' +
        money(window.CS_OBJECT_PRICE || 199) + '</button></div>';
    return guarantee +
      '<div class="merr" id="' + errId + '" style="color:#dc2626;font-size:12.5px;margin:8px 0 0;display:none"></div>' +
      '<div class="cta-sticky">' + consent +
      '<button id="unlockBtn" class="btn" style="width:100%' +
      (cls === 'dark' ? ';background:#111827' : cls === 'red' ? ';background:#b91c1c' : '') +
      '" type="button" onclick="' + payFn + '">' + label + '</button>' +
      fallback +
      '<div style="display:flex;align-items:center;justify-content:center;gap:7px;margin-top:10px;' +
      'font-size:11.5px;color:#6b7280">' +
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2" style="flex:0 0 auto">' +
      '<rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>' +
      'Безопасная оплата через' + yk + '</div></div>';
  }

  /* ── B · заключение уже создано ─────────────────────────────────────────── */
  function renderB(d) {
    var raw = (d.reportId || '').replace(/[^0-9a-f]/gi, '');
    var no;
    if (raw.length >= 6) {
      no = raw.slice(0, 6).toLowerCase();
    } else {
      var h = 0, src = d.vin || 'auto';
      for (var i = 0; i < src.length; i++) { h = (h * 31 + src.charCodeAt(i)) >>> 0; }
      no = ('00000' + h.toString(16)).slice(-6);
    }
    var t = new Date();
    var dd = ('0' + t.getDate()).slice(-2) + '.' + ('0' + (t.getMonth() + 1)).slice(-2) + '.' + t.getFullYear();
    return head('Заключение уже создано', 'Ждёт открытия · ' + esc(carLine(d))) +
      '<div class="pv-doc">' +
      '<div class="hd"><span>ЧистаяСделка Авто · заключение</span><span>№ ' + esc(no) + '</span></div>' +
      '<div class="ttl">Риск покупки автомобиля</div>' +
      '<div class="ln m"></div><div class="ln"></div><div class="ln s"></div>' +
      '<div class="ln"></div><div class="ln m"></div>' +
      '<div class="seal">подпись и печать · ' + dd + '</div></div>' +
      '<p style="font-size:13.5px;color:#374151;line-height:1.55;margin:11px 0 0">' +
      'Бланк на вашу машину сформирован, номер закреплён. Осталось запустить проверку по реестрам.</p>' +
      '<div style="font-size:11.5px;color:#6b7280;text-align:center;margin-top:9px">' +
      'Хранится в вашем кабинете · PDF на почту</div>' +
      foot(d, 'Открыть моё заключение — ' + money(d.price), 'dark');
  }

  /* ── C · он знает — вы нет ──────────────────────────────────────────────── */
  function renderC(d) {
    var left, title, sub, note, fineTxt;
    if (d.isObj) {
      // Базовый тариф — только стоп-факторы, поэтому и асимметрия строится
      // на них: обещать в левой колонке пробег и ДТП, которых в этом тарифе
      // нет, значит продать одно, а выдать другое.
      title = 'Продавец знает, в залоге ли машина. Вы — нет';
      sub = 'И узнаете либо сейчас, либо в ГИБДД';
      left = ['в залоге ли у банка', 'запрет на регистрацию', 'числится ли в розыске',
              'заберут ли её у вас'];
      note = 'Залог переходит к покупателю вместе с машиной — по ст. 353 ГК банк заберёт её ' +
        'уже у вас. Раскрывать это продавец не обязан, но запись лежит в реестре залогов.';
      fineTxt = 'Реестр залогов ФНП и ГИБДД · результат за 15 минут';
    } else {
      title = 'Продавец знает историю машины. Вы — нет';
      sub = 'И узнаете либо сейчас, либо на сервисе';
      left = ['в залоге ли у банка', 'скручен ли пробег', 'была ли в такси', 'сколько было ДТП'];
      note = 'Ни одну из четырёх записей слева продавец раскрывать не обязан. Они лежат в ' +
        'государственных реестрах — и открываются по запросу.';
      fineTxt = 'Восемь реестров · результат за 15 минут';
    }
    var right = [];
    if (d.year) { right.push(String(d.year).indexOf('с ') === 0 ? 'поколение ' + esc(d.year) : esc(d.year) + ' год'); }
    else { right.push('то, что в объявлении'); }
    if (d.engine) right.push(esc(d.engine));
    if (d.vin) right.push('VIN');
    right.push('пробег с его слов');
    return head(title, sub) +
      '<div class="pv-two">' +
      '<div class="pv-side"><div class="who">Он знает</div><ul>' +
      left.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></div>' +
      '<div class="pv-side you"><div class="who">Вы знаете</div><ul>' +
      right.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></div></div>' +
      '<p style="font-size:13.5px;color:#374151;line-height:1.55;margin:0">' + note + '</p>' +
      '<div style="font-size:11.5px;color:#6b7280;text-align:center;margin-top:9px">' + fineTxt + '</div>' +
      foot(d, d.isObj ? 'Узнать то, что знает он — ' + money(d.price)
                      : 'Узнать то, что знает он — ' + money(d.price), 'red');
  }

  var RENDERERS = { B: renderB, C: renderC };

  /* Отрисовать экран цены по назначенному плечу. Возвращает false, если плечо
     контрольное (A) или что-то пошло не так — тогда рисует боевой код. */
  window.renderPriceVariant = function (boxId) {
    try {
      var v = window.CS_VARIANT;
      var fn = RENDERERS[v];
      if (!fn) return false;
      var box = document.getElementById(boxId || 'wizBox');
      if (!box) return false;
      box.innerHTML = fn(data());
      return true;
    } catch (e) {
      // Любая ошибка в варианте не должна оставить человека без экрана оплаты.
      try { console.error('price variant failed', e); } catch (e2) {}
      return false;
    }
  };
})();
