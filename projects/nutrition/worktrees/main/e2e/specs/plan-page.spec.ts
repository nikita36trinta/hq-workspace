import { expect, test } from "./_fixtures";

/** Экран плана — то, что человек получает за деньги. /plan/sample открыт всем. */
test.describe("экран плана", () => {
	/**
	 * Found 2026-07-30 по скриншоту владельца: «что за чёрточка справа снизу».
	 *
	 * Это была кнопка «не нравится». У неё не было НИ ОДНОГО правила в CSS:
	 * инлайновый svg без размеров сплющивал её в вертикальную полоску рядом с
	 * «Заменить» — на экране читалось как случайный артефакт вёрстки.
	 *
	 * Вторая половина дефекта: подтверждение вторым нажатием сообщалось через
	 * title, то есть подсказку, которой на тач-экране НЕ БЫВАЕТ. Первый тап
	 * выглядел как «ничего не произошло».
	 */
	test("кнопка «не нравится» — кнопка, а не чёрточка", async ({ page }) => {
		await page.goto("/plan/sample");
		await page.waitForTimeout(1000);

		const g = await page.evaluate(() => {
			const m = [...document.querySelectorAll(".meal")]
				.find((e) => (e as HTMLElement).offsetParent !== null) as HTMLElement;
			const box = (sel: string) => {
				const e = m.querySelector(sel) as HTMLElement | null;
				if (!e) return null;
				const r = e.getBoundingClientRect();
				return { w: Math.round(r.width), h: Math.round(r.height) };
			};
			return { swap: box(".swap"), dislike: box(".dislike"), svg: box(".dislike svg") };
		});
		expect(g.dislike, "кнопки «не нравится» нет на экране").not.toBeNull();
		expect(g.dislike!.w, "кнопка сплющилась в чёрточку").toBeGreaterThanOrEqual(40);
		expect(g.dislike!.h, "высота должна совпадать с соседями").toBeGreaterThanOrEqual(g.swap!.h - 2);
		expect(g.svg!.w, "иконке не задан размер").toBeGreaterThanOrEqual(16);
	});

	test("первый тап по «не нравится» подтверждает видимой строкой, а не подсказкой", async ({ page }) => {
		await page.goto("/plan/sample");
		await page.waitForTimeout(1000);
		const meals = await page.locator(".meal").count();

		await page.evaluate(() => {
			const m = [...document.querySelectorAll(".meal")]
				.find((e) => (e as HTMLElement).offsetParent !== null)!;
			(m.querySelector(".dislike") as HTMLElement).click();
		});
		await page.waitForTimeout(300);

		const armed = await page.evaluate(() => {
			const m = [...document.querySelectorAll(".meal")]
				.find((e) => (e as HTMLElement).offsetParent !== null)!;
			const h = m.querySelector(".hintx") as HTMLElement | null;
			return { hintShown: !!h && getComputedStyle(h).display !== "none",
				buttonArmed: !!m.querySelector(".dislike.armed") };
		});
		expect(armed.hintShown, "первый тап обязан показать видимую подсказку").toBe(true);
		expect(armed.buttonArmed, "кнопка должна показать, что взведена").toBe(true);
		expect(await page.locator(".meal").count(), "первый тап не должен ничего удалять").toBe(meals);
	});
});
