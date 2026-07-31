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
