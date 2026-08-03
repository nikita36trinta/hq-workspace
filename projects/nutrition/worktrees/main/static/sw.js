// Service worker NutriPlan.
//
// Две разные политики, и путать их нельзя.
//
// 1. СТАТИКА (/assets/, фото блюд /dish/) — сначала кеш. Файлы неизменяемые:
//    имя ассета меняется вместе с содержимым, фото привязано к слагу блюда.
//
// 2. СТРАНИЦА ПЛАНА (/plan/…) — сначала сеть, кеш только когда сети нет.
//    Пейволл обещает: «ставится как приложение, работает и без интернета». Это
//    было неправдой — кешировались ровно две иконки, и без связи приложение не
//    открывалось вовсе. Обещание либо выполняют, либо снимают; выполнить тут
//    дешевле, чем снять: план в телефоне без сети — то самое, за чем с ним идут
//    на кухню и в магазин.
//
// Почему именно сначала сеть, а не «сначала кеш»: страница плана несёт
// состояние подписки, то есть деньги. Свежая копия обязана побеждать всегда,
// кеш — аварийный выход, и на нём мы честно пишем, что данные сохранённые.
//
// Что НЕ кешируется ни при каких условиях: лендинги, квиз, экраны оплаты. Там
// цены, а вчерашняя цена, выданная за сегодняшнюю, — расхождение с офертой.
// Раньше в кеш попадал каждый GET, включая эти страницы; тот запрет остаётся.
//
// Версию поднимать при изменении набора кешируемого: старые кеши сносятся в
// activate.
const C = 'nutriplan-v3';           // неизменяемая статика
const P = 'nutriplan-pages-v3';     // последняя удачная копия страницы плана
const SHELL = ['/assets/icon-192.png', '/assets/icon-512.png'];

// Что можно держать в кеше. Всё остальное — только сеть.
const CACHEABLE = /^\/(assets|dish)\//;
const PAGE = /^\/plan\//;
const STAMP = 'X-NP-Cached-At';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== C && k !== P).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Сервер сам размечает, что можно хранить: настоящее фото блюда отдаётся с
// immutable, заглушка на время генерации — с no-store. Кладём в кеш по ЕГО
// сигналу, иначе первая же заглушка осталась бы там навсегда.
function storable(r) {
  return r && r.ok && !/no-store/i.test(r.headers.get('Cache-Control') || '');
}

function offlineBanner(html, at) {
  let when = '';
  try {
    if (at) {
      when = ' от ' + new Date(at).toLocaleString('ru-RU',
        { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
    }
  } catch (e) { /* дата не разобралась — обойдёмся без неё */ }
  // Обычный блок в начале страницы, а не плашка поверх: fixed сверху перекрыл бы
  // шапку, снизу — нижнее меню, то есть навигацию, а это единственный экран, на
  // котором человеку и так уже мешают.
  const b = '<div style="background:#20321F;color:#fff;padding:11px 16px;text-align:center;'
    + 'font:600 13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif">'
    + 'Нет сети — показываем сохранённый план' + when
    + '<span style="display:block;font-weight:400;opacity:.75;margin-top:2px">'
    + 'Замена блюд и подписка заработают, когда связь вернётся</span></div>';
  return html.replace(/<body[^>]*>/i, m => m + b);
}

async function planPage(req) {
  try {
    const r = await fetch(req);
    if (storable(r)) {
      // Копия и отметка времени: без неё человек не знает, насколько стар план,
      // а «сохранённый план» без даты — это просто «может быть, врём».
      const body = await r.clone().text();
      const h = new Headers(r.headers);
      h.set(STAMP, new Date().toISOString());
      const copy = new Response(body, { status: 200, headers: h });
      caches.open(P).then(c => c.put(req, copy)).catch(() => {});
    }
    return r;
  } catch (err) {
    const hit = await caches.match(req, { cacheName: P });
    if (!hit) throw err;                       // копии нет — честная ошибка сети
    const html = await hit.text();
    return new Response(offlineBanner(html, hit.headers.get(STAMP)), {
      status: 200,
      headers: { 'Content-Type': 'text/html; charset=utf-8' }
    });
  }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === 'navigate') {
    // Из всех страниц офлайн имеет смысл только план: остальные либо про цены,
    // либо про ввод данных, и без сети всё равно ни одна из этих задач не решается.
    if (PAGE.test(url.pathname)) e.respondWith(planPage(req));
    return;                                    // без respondWith — обычное поведение браузера
  }

  if (!CACHEABLE.test(url.pathname)) return;

  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (storable(r)) { const cp = r.clone(); caches.open(C).then(c => c.put(req, cp)); }
      return r;
    }))
  );
});
