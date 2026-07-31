import { expect, test } from "./_fixtures";
import { dismissWelcome } from "./_helpers";

/**
 * Приложение плана после переноса дизайна v5: четыре вкладки, просмотр дня
 * недели, плитка БЖУ «набрано из нормы».
 *
 * Спеки написаны по дефектам, которые реально случились при переносе, а не по
 * списку фич.
 */
test.describe("приложение плана · оболочка", () => {
	/**
	 * Found 2026-07-31. paint() дёргали из activateDay, а activateDay вызывается
	 * при загрузке — раньше, чем объявлено хранилище отметок. ReferenceError в
	 * мёртвой зоне обрывал ВЕСЬ скрипт: обработчики ниже (॒«Приготовил», вода,
	 * список покупок, навигация) не навешивались вообще. На вид страница была
	 * цела — просто ничего не нажималось.
	 *
	 * Поэтому проверка не «есть ли кнопка», а «работает ли она».
	 */
	test("скрипт страницы доживает до конца: отметка блюда меняет счётчик калорий", async ({ page }) => {
		const errors: string[] = [];
		page.on("pageerror", (e) => errors.push(String(e)));

		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(400);

		const before = await page.locator(".panel.on .caltxt").textContent();
		await page.locator(".panel.on .meal .done").first().click();
		await page.waitForTimeout(300);
		const after = await page.locator(".panel.on .caltxt").textContent();

		expect(errors, "на странице выброшено исключение — обработчики ниже не навесились").toEqual([]);
		expect(after, "«Приготовил» не пересчитал съеденное").not.toBe(before);
	});

	/**
	 * Плитка БЖУ показывала одну норму — то есть выглядела как факт съеденного.
	 * Владелец на это и указал: «непонятно, сколько из скольки».
	 */
	test("БЖУ показывает набранное из нормы и растёт от отметок", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(400);

		// Стартовое значение не обязано быть нулём: прогресс синхронизируется с
		// сервером, и на живом плане отметки уже есть. Спека, требующая нуля,
		// падала бы на проде без единого дефекта — проверяем ПРИРОСТ.
		for (const id of ["P", "F", "C"]) {
			await expect(page.locator(`#got${id}`), "нет числа «набрано»").toHaveText(/^\d+$/);
			const goal = await page.locator(`#got${id} + i`).textContent();
			expect(goal ?? "", "рядом с набранным обязана стоять норма").toMatch(/^\/\d+$/);
		}

		const was = Number(await page.locator("#gotP").textContent());
		// отмечаем ещё НЕ отмеченный приём, у которого белок точно не нулевой
		const add = await page.evaluate(() => {
			const meal = [...document.querySelectorAll(".panel.on .meal")]
				.find((m) => Number((m as HTMLElement).dataset.p) > 0 && !m.classList.contains("on")) as HTMLElement;
			if (!meal) return 0;
			(meal.querySelector(".done") as HTMLElement).click();
			return Number(meal.dataset.p);
		});
		test.skip(add === 0, "в этом дне не осталось неотмеченных приёмов с белком");
		await page.waitForTimeout(300);

		await expect(page.locator("#gotP"), "набранный белок не пересчитался").toHaveText(String(was + add));
		const w = await page.locator("#barP").evaluate((e) => (e as HTMLElement).style.width);
		expect(w, "полоса под белком осталась пустой").toMatch(/^[1-9]\d*%$/);
	});

	test("нижняя навигация переключает экраны, а не скроллит один свиток", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);

		for (const [s, id] of [["week", "sc-week"], ["cart", "sc-cart"], ["me", "sc-me"], ["today", "sc-today"]]) {
			await page.locator(`.bnav button[data-s="${s}"]`).click();
			await page.waitForTimeout(200);
			const on = await page.locator(".scr.on").getAttribute("id");
			expect(on, `вкладка ${s} не открыла свой экран`).toBe(id);
		}
	});

	/**
	 * День недели открывается ТОЛЬКО на просмотр — так это и заказано. Если в
	 * копию дня просочатся «Приготовил»/«Заменить», человек будет менять блюда
	 * в клоне, а отметки уйдут в никуда: у клона снят data-k.
	 */
	test("тап по дню недели открывает меню дня без единого действия", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.locator('.bnav button[data-s="week"]').click();
		await page.waitForTimeout(200);

		// Берём ПОСЛЕДНИЙ день, а не третий: в фикстуре план на два дня, и жёсткий
		// индекс молча ловил бы таймаут вместо настоящего дефекта.
		const rows = page.locator(".drow");
		const n = await rows.count();
		expect(n, "список недели пуст").toBeGreaterThan(1);
		await rows.nth(n - 1).click();
		await page.waitForTimeout(400);

		const view = page.locator("#dayview");
		await expect(view, "просмотр дня не открылся").toHaveClass(/\bon\b/);
		expect(await view.locator(".meal").count(), "в просмотре дня нет блюд").toBeGreaterThan(2);
		expect(await view.locator(".mact, details").count(),
			"в просмотре дня не должно быть кнопок и рецептов").toBe(0);
		expect(await view.locator("[data-k]").count(),
			"клон дня не должен нести ключи отметок — иначе отметит чужой день").toBe(0);
		await expect(page.locator("#dvtitle"), "заголовок дня пуст").not.toHaveText("");

		// системное «назад» обязано закрывать экран, а не уводить из приложения
		await page.goBack();
		await page.waitForTimeout(300);
		await expect(view, "«назад» не закрыло просмотр дня").not.toHaveClass(/\bon\b/);
	});

	/**
	 * Шрифты лежат у нас, а не на CDN. Молчаливый фолбэк на системный —
	 * единственное, что отличает перенесённый дизайн от «почти такого же».
	 */
	test("свои шрифты действительно загрузились", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(600);

		const loaded = await page.evaluate(async () => {
			await (document as any).fonts.ready;
			return [...(document as any).fonts].map((f: any) => `${f.family}:${f.status}`);
		});
		expect(loaded.join(" "), "Unbounded не загрузился — заголовки поедут на системный шрифт")
			.toContain("Unbounded:loaded");
		expect(loaded.join(" "), "Onest не загрузился").toContain("Onest:loaded");
	});
});
