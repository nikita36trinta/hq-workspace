import { expect, test } from "./_fixtures";

/**
 * Найдено 2026-08-03 на ПЕРВОЙ БОЕВОЙ ОПЛАТЕ, живым человеком.
 *
 * Экран возврата предлагает задать пароль — и это единственное место в
 * продукте, где его вообще предлагают. Рядом крутился поллер готовности плана,
 * который при ready делал location.href на план. План собрался за секунды,
 * страница сменилась прямо во время ввода, и задать пароль не успел никто.
 *
 * Правило: пока форма пароля на экране, страницу уводит только человек.
 */
test.describe("экран возврата · пароль", () => {
	test("готовый план не выбрасывает со страницы, пока пароль не задан", async ({ page }) => {
		await page.goto("/pay/success?o=paidtest");

		const form = page.locator("#pwf");
		await expect(form, "форма пароля не показана после оплаты").toHaveCount(1);
		await expect(page.locator("#pw1")).toBeVisible();

		// Поллер за это время успевает увидеть готовый план (фикстура плана лежит
		// на месте) и — по старому коду — увёл бы страницу.
		await page.locator("#pw1").fill("Sm0rodina-77");
		await page.waitForTimeout(4500);

		expect(page.url(), "со страницы возврата унесло во время ввода").toContain("/pay/success");
		await expect(form, "форму пароля унесло вместе со страницей").toHaveCount(1);
		await expect(page.locator("#pw1"), "введённый пароль потерян").toHaveValue("Sm0rodina-77");

		// И при этом человеку явно предложен выход: план готов, кнопка на месте.
		await expect(page.locator("#wait")).toContainText("План готов");
		const open = page.locator("a", { hasText: "Открыть план" });
		await expect(open, "нет кнопки «Открыть план» — уйти можно только назад").toHaveCount(1);
		await expect(open).toHaveAttribute("href", "/plan/paidtest");
	});
});
