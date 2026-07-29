import { expect, test } from "./_fixtures";

/**
 * One spec per defect found by hand, so it can only be found once.
 * Each test names the incident it came from.
 */

test.describe("regressions", () => {
	/**
	 * Found 2026-07-29 by walking the quiz at 390px.
	 *
	 * Step 5 asks for height and weight. The fields render placeholders
	 * "170" / "70" / "64" in the same size and weight as real values, so the
	 * step looks filled in — but the fields are empty and «Далее» is disabled
	 * with no message. A person who believes the form is complete taps a dead
	 * button, gets nothing, and leaves. On step 5 of 11, after investing in
	 * eight answers.
	 *
	 * The rule: never a silent dead end. Either the button works, or pressing
	 * it says what is missing.
	 */
	test("body-metrics step must not be a silent dead end", async ({ page }) => {
		await page.goto("/quiz?l=slim");

		// walk until the number inputs appear, answering as we go
		for (let i = 0; i < 30; i++) {
			if ((await page.locator("#stage input[type=number]").count()) > 0) break;
			const opts = page.locator("#stage button.opt");
			if ((await opts.count()) > 0 && (await page.locator("#stage button.opt.sel").count()) === 0) {
				await opts.first().click();
			}
			const next = page.locator("#next");
			if (await next.isVisible().catch(() => false)) await next.click();
			await page.waitForTimeout(300);
		}

		const inputs = page.locator("#stage input[type=number]");
		expect(await inputs.count(), "expected the body-metrics step").toBeGreaterThan(0);

		// the state a real user lands in: nothing typed yet
		await expect(inputs.first()).toHaveValue("");

		const next = page.locator("#next");
		const before = await page.locator("#stage").innerHTML();
		await next.click({ force: true });
		await page.waitForTimeout(500);
		const after = await page.locator("#stage").innerHTML();

		// Either the button advanced, or the page told the user what to do.
		// Silence is the failure mode.
		const advanced = before !== after;
		const explained = await page
			.locator("#stage")
			.evaluate((el) => /заполни|укажи|введи|обязательн/i.test(el.textContent ?? ""));
		expect(
			advanced || explained,
			"pressing the disabled button must either advance or say what is missing — placeholders that look like values plus a mute button is a dead end",
		).toBe(true);
	});

	/**
	 * Found 2026-07-29 by the verify-web-product pipeline: /login collected an
	 * email with no visible basis for processing it. Fixed with a consent line
	 * directly above the button (option B of the golive-legal doctrine).
	 */
	test("login page shows the consent line and links the documents", async ({ page }) => {
		await page.goto("/login");

		const consent = page.locator(".cns");
		await expect(consent).toBeVisible();
		await expect(consent).toContainText(/Нажимая кнопку/i);
		await expect(consent.locator('a[href="/consent"]')).toHaveCount(1);
		await expect(consent.locator('a[href="/privacy"]')).toHaveCount(1);

		// the line has to sit ABOVE the button, not below it in the footer
		const cy = await consent.evaluate((el) => el.getBoundingClientRect().bottom);
		const by = await page.locator("#s").evaluate((el) => el.getBoundingClientRect().top);
		expect(cy, "consent wording must be directly above the button").toBeLessThanOrEqual(by);
	});

	/**
	 * The golive-legal document set must stay served. A 404 here is a
	 * compliance hole, not a broken link.
	 */
	for (const path of ["/privacy", "/consent", "/offer"]) {
		test(`legal page ${path} is served and is a document, not a stub`, async ({ page }) => {
			const res = await page.goto(path);
			expect(res?.status()).toBe(200);
			const text = await page.locator("body").innerText();
			expect(text.length, `${path} is too short to be a real document`).toBeGreaterThan(1000);
		});
	}

	/** The demo plan is the safe surface for manual QA — keep it reachable. */
	test("sample plan opens", async ({ page }) => {
		const res = await page.goto("/plan/sample");
		expect(res?.status()).toBe(200);
		await expect(page.locator("body")).toContainText(/ккал/i);
	});
});
