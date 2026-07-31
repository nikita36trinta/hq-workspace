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

		const before = await page.locator("#calSub").textContent();
		await page.locator(".panel.on .meal .tick").first().click();
		await page.waitForTimeout(300);
		const after = await page.locator("#calSub").textContent();

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
			(meal.querySelector(".tick") as HTMLElement).click();
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
	 * Контракт просмотра дня ИЗМЕНИЛСЯ по решению владельца: сначала день был
	 * «только посмотреть», теперь в нём есть замена блюда, замена всего дня и
	 * подробности блюда. Спека переписана под новый контракт, но охраняемый
	 * дефект остался прежним и проверяется по-прежнему:
	 *
	 * отметка «приготовил» в ЧУЖОМ дне недопустима. Съеденное отмечают в тот
	 * день, когда едят; если галочка просочится в копию, человек закроет чужой
	 * день и собьёт себе серию — а с прежней копией отметка ещё и уходила в
	 * никуда, потому что у клона снимался data-k.
	 */
	test("в просмотре дня есть замена и рецепт, но нет отметки «приготовил»", async ({ page }) => {
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
		await expect(page.locator("#dvtitle"), "заголовок дня пуст").not.toHaveText("");

		// Новое в контракте: править меню недели заранее — ради этого сюда и заходят.
		await expect(view.locator(".swapday"), "нет кнопки «Заменить весь день»").toBeVisible();
		expect(await view.locator(".mopen:not([disabled])").count(),
			"строки должны открывать блюдо: там состав, рецепт и замена").toBeGreaterThan(0);

		// Неизменное: отметить «приготовил» в чужом дне нельзя ни из строки…
		expect(await view.locator(".tick").count(),
			"галочка «приготовил» просочилась в чужой день").toBe(0);

		// …ни с экрана блюда, открытого отсюда.
		await view.locator(".mopen").first().click();
		await page.waitForTimeout(400);
		const dish = page.locator("#dishview");
		await expect(dish, "экран блюда из просмотра дня не открылся").toHaveClass(/\bon\b/);
		await expect(dish, "экран блюда не помечен как открытый из чужого дня").toHaveClass(/\bfromday\b/);
		await expect(dish.locator(".done"), "«Приготовил» виден в чужом дне").toBeHidden();
		await expect(dish.locator(".swap"), "«Заменить» должно остаться").toBeVisible();

		// «Назад» снимает ОДИН слой: закрыло блюдо, просмотр дня остался.
		await page.goBack();
		await page.waitForTimeout(400);
		await expect(dish, "«назад» не закрыло экран блюда").not.toHaveClass(/\bon\b/);
		await expect(view, "«назад» закрыло сразу два экрана вместо одного").toHaveClass(/\bon\b/);

		// системное «назад» обязано закрывать экран, а не уводить из приложения
		await page.goBack();
		await page.waitForTimeout(300);
		await expect(view, "«назад» не закрыло просмотр дня").not.toHaveClass(/\bon\b/);
	});

	/**
	 * Приём пищи в списке — строка: фото, название, приём, калории, отметка.
	 * Всё остальное на экране блюда. Проверяем, что список не отрастил обратно
	 * рецепт-гармошку и три кнопки: именно из-за них день был на восемь экранов.
	 */
	test("в списке — строки, а не карточки с рецептом и кнопками", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(400);

		const m = page.locator(".panel.on .meal").first();
		await expect(m.locator(".mimg img"), "в строке нет фото").toBeVisible();
		await expect(m.locator(".mname"), "в строке нет названия").toBeVisible();
		await expect(m.locator(".mmeta"), "не видно, какой это приём").toBeVisible();
		await expect(m.locator(".mk"), "не видно калорий").toBeVisible();
		await expect(m.locator(".tick"), "нет отметки «приготовил»").toBeVisible();

		for (const sel of ["details", ".mact", ".swap", ".dislike"]) {
			expect(await m.locator(sel).evaluateAll((els) =>
				els.filter((e) => (e as HTMLElement).offsetParent !== null).length),
				`${sel} вернулся в строку списка`).toBe(0);
		}

		const h = await m.evaluate((e) => Math.round(e.getBoundingClientRect().height));
		expect(h, "строка не должна быть высокой карточкой").toBeLessThan(90);
	});

	/**
	 * Экран блюда собирается ИЗ САМОЙ СТРОКИ (скрытый .mhide). Если разметку
	 * переставят, экран молча опустеет — человек заплатил в том числе за рецепт.
	 * Кнопки там — клон, и работают они только потому, что обработчики
	 * делегированные; проверяем именно нажатие, а не наличие.
	 */
	test("экран блюда несёт фото, КБЖУ, состав, шаги и рабочие кнопки", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(400);

		const name = await page.locator(".panel.on .meal").first().locator(".mname").textContent();
		await page.locator(".panel.on .mopen").first().click();
		await page.waitForTimeout(400);

		const v = page.locator("#dishview");
		await expect(v, "экран блюда не открылся").toHaveClass(/\bon\b/);
		await expect(v.locator("h2"), "название блюда не перенеслось").toHaveText(name!.trim());
		await expect(v.locator(".dshot img"), "нет фото").toBeVisible();
		expect(await v.locator(".dkcal div").count(), "должны быть ккал и три макроса").toBe(4);
		expect(await v.locator(".ing li").count(), "пропал состав").toBeGreaterThan(0);
		expect(await v.locator(".steps li").count(), "пропали шаги").toBeGreaterThan(0);
		for (const sel of [".done", ".swap", ".dislike"]) {
			await expect(v.locator(sel), `нет кнопки ${sel}`).toBeVisible();
		}

		// Клон «Приготовил» обязан реально переключать отметку. Сравниваем ДО и
		// ПОСЛЕ, а не ждём «отмечено»: прогресс синхронизируется с сервером, и
		// блюдо вполне может быть уже отмечено прошлым прогоном.
		const before = await page.locator("#calSub").textContent();
		const wasOn = await v.locator(".done").evaluate((e) => e.classList.contains("on"));
		await v.locator(".done").click();
		await page.waitForTimeout(300);
		expect(await v.locator(".done").evaluate((e) => e.classList.contains("on")),
			"кнопка не показала смену отметки").toBe(!wasOn);
		await page.locator("#dishback").click();
		await page.waitForTimeout(300);
		expect(await page.locator("#calSub").textContent(),
			"отметка с экрана блюда не дошла до счётчика калорий").not.toBe(before);
	});

	/**
	 * График веса убран по решению владельца: на двух-трёх записях он рисовал
	 * шум, а не тренд. Проверка на боевом экране, а не на макете — макет от
	 * продукта уже разъехался однажды: график убрали в макете и оставили в
	 * приложении, и это никто не поймал.
	 */
	test("в профиле нет графика веса, а карточка веса показывает число и дельту", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.locator('.bnav button[data-s="me"]').click();
		await page.waitForTimeout(400);

		expect(await page.locator("#sc-me .wcard svg, #sc-me #wspark").count(),
			"график веса вернулся в профиль").toBe(0);
		await expect(page.locator("#wcur"), "в карточке веса нет числа").not.toHaveText("");
		expect(await page.locator("#sc-me .prow").count(),
			"нет блока «Параметры плана» — человеку негде свериться с анкетой").toBeGreaterThan(2);
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
