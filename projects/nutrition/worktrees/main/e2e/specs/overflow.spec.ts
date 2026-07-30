import { expect, test } from "./_fixtures";
import { submitEmail, walkQuizToPaywall } from "./_helpers";

/**
 * Ни одна страница не должна уезжать боком на телефоне.
 *
 * Found 2026-07-30. Украшения на экране плана вынесены за карточку намеренно
 * (right:-86px), и документ при 390px становился 456px.
 *
 * ЧЕМ ЭТО ЛОВИТЬ. Первая версия теста дёргала window.scrollTo(400) и смотрела
 * scrollX — и ПРОХОДИЛА на сломанной странице. Мобильный браузер не прокручивает
 * документ, а расширяет layout viewport под вылезший контент и отзумливает всю
 * вёрстку: clientWidth оставался 390, а innerWidth становился 456. Прокрутки в
 * терминах DOM нет, но пальцем страница таскается, и всё рисуется мельче.
 *
 * Поэтому проверяем именно это: layout viewport не должен быть шире экрана.
 * Признак работает и там, где overflow-x:clip не поддержан, потому что смотрит
 * на результат, а не на способ его добиться.
 */
async function viewport(page: any) {
	return page.evaluate(() => ({
		client: document.documentElement.clientWidth,
		inner: window.innerWidth,
		scroll: document.documentElement.scrollWidth,
	}));
}

const PAGES = ["/", "/quiz?l=slim", "/l/slim", "/showcase", "/privacy", "/consent", "/offer",
	"/login", "/pay/success?o=nope"];

for (const url of PAGES) {
	test(`страница не шире экрана: ${url}`, async ({ page }) => {
		const r = await page.goto(url);
		if (!r || r.status() >= 400) test.skip(true, `страница отдала ${r?.status()}`);
		await page.waitForTimeout(900);
		const v = await viewport(page);
		expect(v.inner, `${url}: layout viewport ${v.inner} шире экрана ${v.client}`).toBeLessThanOrEqual(v.client);
		expect(v.scroll, `${url}: содержимое ${v.scroll} шире экрана ${v.client}`).toBeLessThanOrEqual(v.client);
	});
}

test("экран плана с украшениями не шире экрана", async ({ page }) => {
	await page.goto("/quiz?l=slim&reset=1");
	await walkQuizToPaywall(page);
	await submitEmail(page, "bleed@example.com");
	await page.waitForTimeout(1200);
	const v = await viewport(page);
	expect(v.inner, `украшения растянули viewport до ${v.inner} при экране ${v.client}`).toBeLessThanOrEqual(v.client);
	expect(v.scroll, `содержимое ${v.scroll} шире экрана ${v.client}`).toBeLessThanOrEqual(v.client);
});

test("кнопка оплаты остаётся прилипшей — clip не сделал .shell скроллером", async ({ page }) => {
	await page.goto("/quiz?l=slim&reset=1");
	await walkQuizToPaywall(page);
	const foot = page.locator(".foot");
	expect(await foot.evaluate((e) => getComputedStyle(e).position)).toBe("sticky");
	const before = await foot.evaluate((e) => e.getBoundingClientRect().bottom);
	await page.mouse.wheel(0, 150);
	await page.waitForTimeout(400);
	const after = await foot.evaluate((e) => e.getBoundingClientRect().bottom);
	expect(Math.abs(after - before), "подвал уехал вместе со страницей — sticky сломан").toBeLessThan(4);
});
