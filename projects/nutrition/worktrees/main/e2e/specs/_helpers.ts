import { expect, type Page } from "@playwright/test";

/**
 * Walk the quiz the way a person does, without hardcoding the step order —
 * the questions get reordered and A/B'd, and a spec that pins the sequence
 * would break on every copy change instead of on real defects.
 *
 * Each turn: fill any number inputs, pick an option if none is picked, press
 * the primary button. Stop when the email capture appears.
 */
export async function walkQuizToPaywall(page: Page, maxSteps = 30): Promise<number> {
	let steps = 0;

	for (; steps < maxSteps; steps++) {
		// the email step is the destination
		if (await page.locator("#mail").isVisible().catch(() => false)) return steps;

		const numbers = page.locator("#stage input[type=number]");
		const nCount = await numbers.count();
		if (nCount > 0) {
			// height / weight / target weight — fill from the placeholders so the
			// values stay plausible for whatever the step is asking
			for (let i = 0; i < nCount; i++) {
				const input = numbers.nth(i);
				const ph = (await input.getAttribute("placeholder")) ?? "70";
				await input.fill(ph);
			}
		}

		const options = page.locator("#stage button.opt");
		if ((await options.count()) > 0 && (await page.locator("#stage button.opt.sel").count()) === 0) {
			await options.first().click();
		}

		const next = page.locator("#next");
		if (!(await next.isVisible().catch(() => false))) {
			// the plan-builder screen has no button — it advances by itself
			await page.waitForTimeout(1500);
			continue;
		}
		await expect(next, "the primary button must be usable once the step is answered").toBeEnabled();
		await next.click();
		await page.waitForTimeout(400);
	}

	throw new Error(`quiz did not reach the email step within ${maxSteps} steps`);
}

/**
 * Закрыть приветственный оверлей на экране плана.
 *
 * Он показывается один раз на токен и накрывает ВЕСЬ экран, перехватывая
 * нажатия. Тесты, которые щёлкали элементы через page.evaluate, этого не
 * замечали — JS-click не проверяет попадание. Настоящий тап в него упирается,
 * поэтому проходить его надо так же, как человек.
 */
export async function dismissWelcome(page: Page): Promise<void> {
	const skip = page.locator("#wskip");
	if (await skip.isVisible().catch(() => false)) {
		await skip.click();
		await page.waitForTimeout(250);
	}
	await expect(page.locator("#welcome"), "оверлей приветствия должен закрыться").toBeHidden();
}

/** Distance from the top of the viewport to the first rendered content. */
export async function contentTop(page: Page): Promise<number> {
	return page.evaluate(() => {
		const stage = document.getElementById("stage");
		return stage ? Math.round(stage.getBoundingClientRect().top) : -1;
	});
}

/**
 * Отправить почту на пейволле и дождаться раскрытия нормы.
 *
 * Тапаем по координатам, а не click(): после перехода карточек на стекло
 * (backdrop-filter создаёт новый контекст наложения) проверка попадания в
 * Playwright начала считать, что кнопку перекрывает текст подсказки. Настоящий
 * тап пальцем при этом срабатывает — проверено вручную, норма раскрывается,
 * и elementFromPoint в центре кнопки возвращает саму кнопку.
 *
 * Чтобы это не превратилось в «ну и ладно», проверка сохранена по СУТИ: ниже
 * ждём, что норма действительно появилась. Если кнопка правда перестанет
 * работать, тест упадёт здесь же.
 */
export async function submitEmail(page: Page, email: string): Promise<void> {
	await page.locator("#mail").fill(email);
	const send = page.locator("#send");
	await send.scrollIntoViewIfNeeded();
	await page.waitForTimeout(350);          // даём доехать плавному скроллу
	const box = await send.boundingBox();
	if (!box) throw new Error("кнопка отправки почты не найдена");
	await page.mouse.click(Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2));
	await expect(
		page.locator(".kcal .big"),
		"после отправки почты норма обязана раскрыться",
	).toBeVisible();
}
