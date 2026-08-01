/* Перенос рекламных меток с лендинга в квиз.
 *
 * Дефект: человек приходит по объявлению на /l/slim?utm_source=yandex&yclid=…,
 * а все кнопки на странице ведут на «/quiz?l=slim» — метки остаются в адресной
 * строке лендинга и до оплаты не доезжают. Каждая платная покупка писалась в
 * отчёты как organic, то есть рекламу было нечем оценивать.
 *
 * Один файл на все лендинги: их восемь и будет больше, а восемь копий скрипта
 * расходятся уже на второй правке. Подключается строкой перед </body>.
 *
 * Тащим ВСЕ параметры входного адреса, а не заранее известный список меток:
 * площадки добавляют новые (yclid, ymclid, gclid, roistat, rb_clickid…)
 * быстрее, чем мы правим код. Свои параметры ссылки (?l=slim) приоритетнее —
 * они часть маршрута, а не метка.
 */
(function () {
  var src;
  try { src = new URLSearchParams(location.search); } catch (e) { return; }
  if (!src.toString()) return;

  function patch(a) {
    var href = a.getAttribute('href');
    if (!href) return;
    var u;
    try { u = new URL(href, location.href); } catch (e) { return; }
    // Наружу метки не отдаём: чужой домен наших меток не ждёт, а yclid в
    // ссылке на оферту или соцсеть — это утечка данных о кампании.
    if (u.origin !== location.origin) return;
    if (!/^\/quiz(\/|$)/.test(u.pathname)) return;
    src.forEach(function (v, k) {
      if (!u.searchParams.has(k)) u.searchParams.set(k, v);   // ?l=slim не затираем
    });
    a.setAttribute('href', u.pathname + u.search + u.hash);
  }

  function patchAll() {
    var i, list = document.querySelectorAll('a[href]');
    for (i = 0; i < list.length; i++) patch(list[i]);
  }

  patchAll();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', patchAll);
  // Подстраховка на клике: ссылка могла появиться после разметки (липкая
  // кнопка, попап) — тогда её никто не переписал бы. patch идемпотентен.
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (a) patch(a);
  }, true);
})();

/* Цели Метрики на лендингах.
 *
 * Их не было ни одной: клик по кнопке «Собрать план» не отправлял ничего.
 * Значит нельзя ни сравнить восемь лендингов между собой, ни оптимизировать
 * кампанию по первому осмысленному действию, ни увидеть, доходят ли люди до
 * квиза вообще. А раздача трафика с корня по восьми вариантам крутится именно
 * ради этого сравнения.
 *
 * Две цели, обе — на самом лендинге:
 *   landing_view — просмотр (для знаменателя, отдельно от визита Метрики)
 *   landing_cta  — клик по любой кнопке, ведущей в квиз
 *
 * Лендинг подставляется в параметры цели, а не в её имя: восемь имён вида
 * cta_slim, cta_chef… пришлось бы заводить в Директе поштучно и переделывать
 * при каждом новом лендинге.
 */
(function () {
  function slug() {
    var m = location.pathname.match(/^\/l\/([a-z0-9_-]+)/i);
    return m ? m[1] : 'root';
  }
  function goal(name) {
    // npGoal ставит страница вместе со счётчиком. Нет счётчика (или notrack) —
    // функции нет, и мы молча ничего не делаем.
    try { if (window.npGoal) window.npGoal(name, { landing: slug() }); } catch (e) {}
  }
  // Только на самих лендингах: /all и превью в iframe накрутили бы просмотры
  // всем восьми сразу.
  if (!/^\/l\//.test(location.pathname) || window.top !== window.self) return;
  goal('landing_view');
  var sent = false;
  function onPress(e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || sent) return;
    var u;
    try { u = new URL(a.getAttribute('href') || '', location.href); } catch (err) { return; }
    if (u.origin !== location.origin || !/^\/quiz(\/|$)/.test(u.pathname)) return;
    // Один раз на загрузку страницы: человек может ткнуть в кнопку дважды,
    // а цель «дошёл до квиза» от этого не становится двумя людьми.
    sent = true;
    goal('landing_cta');
  }
  // pointerdown срабатывает до начала навигации — этого достаточно в большинстве
  // случаев, но не всегда: замер на живом сайте показал, что при быстром переходе
  // браузер успевает отменить незавершённый запрос цели. Поэтому на самом клике
  // придерживаем переход, пока цель не уйдёт.
  document.addEventListener('pointerdown', onPress, true);
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    // Новую вкладку, среднюю кнопку и модификаторы не трогаем: там навигации в
    // текущем окне нет, отменять запрос нечему, а перехват сломал бы привычное.
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey ||
        e.shiftKey || e.altKey || a.target === '_blank') { onPress(e); return; }
    var u;
    try { u = new URL(a.getAttribute('href') || '', location.href); } catch (err) { return; }
    if (u.origin !== location.origin || !/^\/quiz(\/|$)/.test(u.pathname)) return;
    if (!window.npGoal || !window.ym) return;          // счётчика нет — не мешаем
    e.preventDefault();
    var went = false;
    function go() { if (!went) { went = true; location.href = a.href; } }
    if (!sent) {
      sent = true;
      try { window.npGoal('landing_cta', { landing: slug() }, go); } catch (err) { go(); }
    }
    // Предохранитель: Метрика может не ответить (блокировщик, сеть). Ждать её
    // дольше четверти секунды нельзя — это самый важный клик на странице, и
    // задержать его ради аналитики значит платить конверсией за отчёт.
    setTimeout(go, 250);
  }, true);
})();
