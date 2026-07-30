/* Общие данные для всех четырёх прототипов.
   Данные НАСТОЯЩИЕ — первый день боевого плана /plan/sample и его список покупок.
   Иначе сравнивать нечестно: на выдуманных коротких названиях любая вёрстка
   выглядит хорошо, а ломается она как раз на «Рыбное филе запеченное». */
window.NP = {
  norm: { cal: 1580, p: 118, f: 53, c: 158 },
  water: { done: 5, goal: 9 },
  day: 1,
  days: 7,
  meals: [
    { slot: "Завтрак", time: "08:00", name: "Овсянка на воде", slug: "ovsyanka-vode",
      kcal: 280, p: 10, f: 5, c: 50, done: true,
      ing: ["овсяные хлопья 50 г", "вода 200 мл", "яблоко 100 г", "корица 2 г"],
      steps: ["Залить хлопья водой, довести до кипения и варить 5 минут.",
              "Нарезать яблоко кубиками, добавить к каше.", "Посыпать корицей."] },
    { slot: "Перекус", time: "11:00", name: "Банан свежий", slug: "banan-svezhiy",
      kcal: 105, p: 1, f: 0, c: 27, done: false,
      ing: ["банан 1 шт (100 г)"], steps: ["Очистить банан и съесть."] },
    { slot: "Обед", time: "14:00", name: "Куриная грудка с рисом", slug: "kurinaya-grudka-risom",
      kcal: 450, p: 40, f: 10, c: 45, done: false,
      ing: ["куриная грудка 150 г", "рис бурый 70 г", "брокколи 150 г", "соевый соус 10 мл"],
      steps: ["Отварить рис до готовности.",
              "Грудку нарезать кубиками, обжарить на антипригарной сковороде.",
              "Брокколи отварить 5 минут, соединить и полить соусом."] },
    { slot: "Перекус", time: "17:00", name: "Орехи ассорти", slug: "orehi-assorti",
      kcal: 180, p: 6, f: 15, c: 6, done: false,
      ing: ["миндаль 15 г", "грецкий орех 15 г"], steps: ["Съесть орехи."] },
    { slot: "Ужин", time: "19:30", name: "Рыбное филе запеченное", slug: "rybnoe-file-zapechennoe",
      kcal: 550, p: 50, f: 20, c: 30, done: false,
      ing: ["филе трески 200 г", "картофель 200 г", "помидоры черри 100 г", "зелень 10 г"],
      steps: ["Картофель нарезать дольками, помидоры пополам.",
              "Филе выложить на противень, вокруг разложить овощи.",
              "Запекать 25 минут при 190 °C."] },
  ],
  shopping: [
    { cat: "Овощи и зелень", items: ["яблоки 7 шт", "бананы 3 шт", "морковь 500 г", "брокколи 150 г",
        "помидоры черри 200 г", "картофель 1 кг", "зелень 30 г"] },
    { cat: "Мясо и птица", items: ["куриная грудка (филе) 1.2 кг"] },
    { cat: "Рыба и морепродукты", items: ["филе трески 400 г", "филе минтая 150 г"] },
    { cat: "Бакалея", items: ["овсяные хлопья 150 г", "рис бурый 140 г", "чечевица красная 70 г",
        "миндаль 100 г", "грецкий орех 100 г"] },
  ],
};

/* Иконки — рисованные, не эмодзи: эмодзи по-разному выглядят на разных системах
   и выдают «сделано на скорую руку». */
window.IC = {
  fire: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3c3.5 4.2 6 7.2 6 10a6 6 0 0 1-12 0c0-2.8 2.5-5.8 6-10Z"/></svg>',
  drop: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3c3.2 4 5.5 6.9 5.5 9.6A5.5 5.5 0 0 1 6.5 12.6C6.5 9.9 8.8 7 12 3Z"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
  swap: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h13l-3-3M20 16H7l3 3"/></svg>',
  cart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.2 10.2a2 2 0 0 0 2 1.6h7.4a2 2 0 0 0 2-1.5L20 8H6"/><circle cx="10" cy="20" r="1.3"/><circle cx="17" cy="20" r="1.3"/></svg>',
  week: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 10h18"/></svg>',
  home: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 11 12 4l8 7v8a2 2 0 0 1-2 2h-3v-6H9v6H6a2 2 0 0 1-2-2Z"/></svg>',
  me: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20c1.3-3.6 4-5.4 7.5-5.4S18.2 16.4 19.5 20"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>',
};

window.eaten = () => NP.meals.filter((m) => m.done).reduce((s, m) => s + m.kcal, 0);
window.dishUrl = (slug) => "/dish/" + slug;
