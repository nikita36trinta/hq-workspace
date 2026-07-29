import { expect, test } from "@playwright/test";
import { contentTop, walkQuizToPaywall } from "./_helpers";

/**
 * The one journey that has to work: landing → quiz → norm → paywall.
 * Everything the product sells sits behind this path, so if it breaks the
 * product earns nothing regardless of what else is green.
 */

test.describe("funnel", () => {
	test("landing serves the quiz CTA", async ({ page }) => {
		const res = await page.goto("/l/slim");
		expect(res?.status(), "the landing must serve").toBe(200);
		await expect(page.locator("body")).toContainText(/план|питани/i);
	});

	test("quiz runs end to end and reveals the norm after the email", async ({ page }) => {
		await page.goto("/quiz?l=slim");

		const steps = await walkQuizToPaywall(page);
		expect(steps, "the quiz should take a bounded number of steps").toBeGreaterThan(5);

		// Before the email the norm is hidden — that gate is the whole reason
		// the email is asked for. If it ever renders unlocked, the lead magnet
		// is being given away and the funnel below it dies.
		await expect(page.locator(".lockrow"), "the norm must stay locked until the email").toBeVisible();

		await page.locator("#mail").fill("e2e@mynutriplan.ru");
		await page.locator("#send").click();

		// the real calorie number replaces the dots
		const kcal = page.locator(".kcal .big");
		await expect(kcal).toBeVisible();
		await expect(kcal).toHaveText(/^\d{3,4}$/);

		// and the paywall with both tariffs is now reachable
		await expect(page.locator("#pay"), "the pay button must exist after the reveal").toBeVisible();
		await expect(page.locator("label.tar")).toHaveCount(2);
	});

	test("consent wording sits next to the email field", async ({ page }) => {
		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);

		const trust = page.locator(".trust").first();
		await expect(trust, "collecting an email needs a visible basis").toContainText(/Соглас/i);
		await expect(trust.locator('a[href="/consent"]')).toHaveCount(1);
		await expect(trust.locator('a[href="/privacy"]')).toHaveCount(1);
	});

	test("the plan screen opens at the top, with no dead band", async ({ page }) => {
		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);

		// Regression: `.top.hide` used visibility:hidden, which keeps the
		// element's space — the plan screen opened with 99px of nothing above
		// the headline, a quarter of the first screen on a phone.
		expect(await contentTop(page), "content must start at the top of the plan screen").toBeLessThan(20);
	});
});
