import { expect, test } from "./_fixtures";

/**
 * Найдено 2026-08-03 глазами на широком экране (1728px).
 *
 * Два дефекта одного класса — «родитель обрезает то, что должен показывать»:
 *   • chef: max-width:20ch стоял на КОНТЕЙНЕРЕ с шрифтом 16px, то есть 202px,
 *     а заголовок внутри — 86px. Восемь слов ломались на пять строк.
 *   • fitness: overflow:hidden на карточке фото резал плашки, которые по замыслу
 *     свешиваются с неё на 22px («Сегодня 1 840 ккал», «под твои тренировки»).
 *
 * Мобильные тесты этого не ловят: там оба помещаются. Проверяем на десктопе.
 */
const LANDINGS = ["slim", "easy", "pro", "chef", "coach", "reset", "energy", "fitness"];

test.describe("лендинги · вёрстка на широком экране", () => {
	test.skip(({ isMobile }) => !!isMobile, "дефекты видны только на десктопе");

	for (const l of LANDINGS) {
		test(`/l/${l}: ничего не обрезано и заголовок не сыплется в столбик`, async ({ page }) => {
			await page.setViewportSize({ width: 1600, height: 900 });
			await page.goto(`/l/${l}`);

			// 1. Обрезанный ТЕКСТ. Именно текст, а не любой вылет: картинки в рамках
			//    кадрируются нарочно (на /l/easy скриншот шире рамки на 13px с каждой
			//    стороны — это кроп, а не поломка), и проверка «ничего не вылезает»
			//    ловила бы дизайн. Текст же обрезаться не должен никогда: на
			//    /l/fitness так пропадала первая цифра «1 840 ккал».
			const clipped = await page.evaluate(() => {
				const out: string[] = [];
				document.querySelectorAll<HTMLElement>("*").forEach((el) => {
					const own = Array.from(el.childNodes)
						.filter((n) => n.nodeType === 3).map((n) => n.textContent || "").join("").trim();
					if (!own) return;                       // свой текст, а не текст потомков
					const r = el.getBoundingClientRect();
					if (r.width < 2 || r.height < 2 || r.top > 1400) return;
					let p = el.parentElement;
					while (p && p !== document.body) {
						const pr = p.getBoundingClientRect(), ps = getComputedStyle(p);
						if (/hidden|clip/.test(ps.overflow + ps.overflowX + ps.overflowY)) {
							if (r.left < pr.left - 2 || r.right > pr.right + 2) {
								out.push(`${el.className || el.tagName}: «${own.slice(0, 30)}»`);
							}
							return;
						}
						p = p.parentElement;
					}
				});
				return out;
			});
			expect(clipped, `обрезано предком: ${clipped.join(" / ")}`).toHaveLength(0);

			// 2. Заголовок первого экрана не должен разваливаться в столбик.
			const h1 = await page.evaluate(() => {
				const h = document.querySelector("h1");
				if (!h) return null;
				const s = getComputedStyle(h), r = h.getBoundingClientRect();
				const lh = parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.2;
				const words = (h.textContent || "").trim().split(/\s+/).length;
				return { lines: Math.round(r.height / lh), words };
			});
			if (h1) {
				expect(h1.lines, "заголовок сыплется в столбик — тесная колонка").toBeLessThanOrEqual(3);
				expect(h1.lines).toBeLessThan(h1.words);
			}
		});
	}
});
