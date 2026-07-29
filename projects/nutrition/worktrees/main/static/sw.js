// Service worker NutriPlan.
//
// Кешируем ТОЛЬКО статику. Раньше сюда попадал каждый GET, включая HTML, и при
// любом сбое сети человек получал страницу из кеша — сколь угодно старую, с
// ценами, которых уже нет. Имя кеша при этом было прибито к v1 и никогда не
// поднималось, так что деплой ничего не инвалидировал. Расхождение цены на
// экране с ценой в оферте — ровно тот класс проблем, который мы ловим
// пайплайном verify-web-product, и создавать его своими руками незачем.
//
// Версию поднимать при изменении набора кешируемого: старые кеши сносятся в
// activate.
const C = 'nutriplan-v2';
const SHELL = ['/assets/icon-192.png', '/assets/icon-512.png'];

// Что можно держать в кеше: неизменяемые ассеты. Всё остальное — только сеть.
const CACHEABLE = /^\/assets\//;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Навигация и любой не-ассет идут в сеть и НИКОГДА не подменяются кешем.
  // Лучше честная ошибка сети, чем вчерашняя цена, выданная за сегодняшнюю.
  if (req.mode === 'navigate' || url.origin !== self.location.origin || !CACHEABLE.test(url.pathname)) {
    return; // без respondWith — обычное поведение браузера
  }

  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (r.ok) { const cp = r.clone(); caches.open(C).then(c => c.put(req, cp)); }
      return r;
    }))
  );
});
