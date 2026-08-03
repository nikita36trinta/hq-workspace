import { expect, test } from "./_fixtures";

/**
 * Пейволл обещает: «Ставится как приложение, работает и без интернета».
 *
 * Обещание было ложным — service worker кешировал две иконки, и без связи
 * приложение не открывалось вообще. Тест держит обещание выполненным и
 * одновременно стережёт границу: страницы с ценами офлайн отдавать нельзя,
 * вчерашняя цена, выданная за сегодняшнюю, расходится с офертой.
 */
test.describe("офлайн", () => {
	test("план открывается без сети и честно помечен, лендинг с ценами — нет", async ({ page, context }) => {
		await page.goto("/plan/sample");
		await page.evaluate(() => navigator.serviceWorker.ready);
		// Второй заход идёт уже через service worker — он и кладёт копию в кеш.
		await page.goto("/plan/sample");
		await page.waitForFunction(() => !!navigator.serviceWorker.controller);
		await page.waitForTimeout(1000);

		await context.setOffline(true);
		await page.goto("/plan/sample");

		// 1. Приложение открылось: нижнее меню на месте, а не страница ошибки.
		for (const tab of ["Сегодня", "Неделя", "Покупки"]) {
			await expect(page.locator("body"), `без сети пропал раздел «${tab}»`).toContainText(tab);
		}
		// 2. И сразу сказано, что копия сохранённая: показывать вчерашние данные
		//    молча — это врать о том, что связь есть.
		await expect(page.locator("body")).toContainText("Нет сети — показываем сохранённый план");

		// 3. Лендинг офлайн открываться НЕ должен: там цены.
		const r = await page.goto("/l/slim").catch(() => null);
		expect(r === null || !r.ok(), "лендинг с ценами отдан из кеша").toBeTruthy();
	});
});
