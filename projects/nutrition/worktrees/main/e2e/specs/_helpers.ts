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

/** Distance from the top of the viewport to the first rendered content. */
export async function contentTop(page: Page): Promise<number> {
	return page.evaluate(() => {
		const stage = document.getElementById("stage");
		return stage ? Math.round(stage.getBoundingClientRect().top) : -1;
	});
}
