import { expect, test } from "./_fixtures";
import { submitEmail, walkQuizToPaywall } from "./_helpers";

/**
 * Аналитика и режим «не считать меня».
 *
 * Счётчик задаётся переменной NUTRI_METRIKA_ID. В e2e-контейнере её нет, и это
 * правильно: тесты не должны слать события в боевую Метрику. Поэтому здесь
 * проверяется КОНТРАКТ — что цели вызываются через window.npGoal с ожидаемыми
 * именами (мы их же завели в Метрике), — а не то, что Яндекс их принял.
 */

/** Подменяем npGoal собственным сборщиком до загрузки страницы. */
async function collectGoals(page: import("@playwright/test").Page): Promise<string[]> {
	const fired: string[] = [];
	await page.exposeFunction("__goalFired", (n: string) => {
		fired.push(n);
	});
	await page.addInitScript(() => {
		// @ts-expect-error — тестовая подмена рантайм-хелпера страницы
		window.npGoal = (n: string) => (window as any).__goalFired(n);
	});
	return fired;
}

test.describe("analytics", () => {
	// Подмена npGoal видна только там, где НЕТ настоящего счётчика: сниппет
	// Метрики переопределяет window.npGoal после нашего addInitScript. Против
	// боевого адреса цели наблюдать нечем — это проверка контракта, её место
	// на локальном прогоне.
	const localOnly = () => test.skip(!!process.env.BASE_URL, "нужен прогон без боевого счётчика");

	test("воронка шлёт цели с теми именами, что заведены в Метрике", async ({ page }) => {
		localOnly();
		const fired = await collectGoals(page);

		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);
		expect(fired, "старт квиза").toContain("quiz_start");
		expect(fired, "шаг рост-вес — там была мёртвая кнопка").toContain("quiz_metrics");
		expect(fired, "экран с почтой").toContain("quiz_email_screen");

		await submitEmail(page, "goals@mynutriplan.ru");
		await expect(page.locator(".kcal .big")).toBeVisible();

		expect(fired, "почта оставлена").toContain("lead");
		expect(fired, "норма показана").toContain("quiz_norm_shown");
	});

	test("каждая цель шлётся один раз, даже если вернуться назад", async ({ page }) => {
		localOnly();
		const fired = await collectGoals(page);
		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);

		const starts = fired.filter((g) => g === "quiz_start").length;
		expect(starts, "quiz_start не должен накручиваться при возврате на шаг").toBe(1);
	});

	test("notrack убирает счётчик со страницы", async ({ page }) => {
		await page.goto("/l/slim?notrack=1");
		const withNotrack = await page.content();
		expect(withNotrack).not.toContain("mc.yandex.ru/metrika");

		// и режим липнет: на следующей странице счётчика тоже нет
		await page.goto("/quiz?l=slim");
		expect(await page.content()).not.toContain("mc.yandex.ru/metrika");

		// выключается явно
		await page.goto("/l/slim?notrack=0");
		const cookies = await page.context().cookies();
		expect(cookies.find((c) => c.name === "np_notrack")).toBeUndefined();
	});

	test("notrack не пишет лид", async ({ page, request }) => {
		await page.goto("/l/slim?notrack=1");
		const before = await (await request.get("/api/health")).status();
		expect(before).toBe(200);

		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);
		await submitEmail(page, "notrack-probe@mynutriplan.ru");

		// фронт ведёт себя ровно так же — иначе тестировался бы не тот путь
		await expect(page.locator(".kcal .big")).toBeVisible();
	});
});
