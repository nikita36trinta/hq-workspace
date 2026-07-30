import { expect, test } from "./_fixtures";
import { submitEmail, walkQuizToPaywall } from "./_helpers";

/**
 * Ни одна страница не должна уезжать боком на телефоне.
 *
 * Found 2026-07-30: украшения на экране плана вынесены за карточку намеренно
 * (right:-86px), и документ при 390px становился 456px — палец таскал всю
 * страницу вбок. Вылет — часть замысла, поэтому убирать его нельзя; обрезаем
 * страницу через html{overflow-x:clip} (именно clip: hidden сделал бы элемент
 * контейнером прокрутки и сломал sticky-кнопку оплаты), плюс @supports-ветка
 * с body{overflow-x:hidden} для Safari младше 16, где clip не знают.
 *
 * Проверяем именно ПОВЕДЕНИЕ — «страница не двигается боком», а не «ничего не
 * вылезает»: вылезать тут как раз должно.
 */
async function slidesSideways(page: any): Promise<number> {
	return page.evaluate(() => {
		const before = window.scrollX;
		window.scrollTo(400, window.scrollY);
		const moved = window.scrollX;
		window.scrollTo(before, window.scrollY);
		return moved;
	});
}

const PAGES = ["/", "/quiz?l=slim", "/l/slim", "/showcase", "/privacy", "/consent", "/offer",
	"/login", "/pay/success?o=nope"];

for (const url of PAGES) {
	test(`страница не уезжает боком: ${url}`, async ({ page }) => {
		const r = await page.goto(url);
		if (!r || r.status() >= 400) test.skip(true, `страница отдала ${r?.status()}`);
		await page.waitForTimeout(900);
		expect(await slidesSideways(page), `${url}: страница прокрутилась вбок`).toBe(0);
	});
}

test("экран плана с украшениями не уезжает боком", async ({ page }) => {
	await page.goto("/quiz?l=slim&reset=1");
	await walkQuizToPaywall(page);
	await submitEmail(page, "bleed@example.com");
	await page.waitForTimeout(1200);
	expect(await slidesSideways(page), "украшения не должны тянуть страницу вбок").toBe(0);
});
