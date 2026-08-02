import { defineConfig, devices } from "@playwright/test";

/**
 * NutriPlan E2E — the funnel, exercised as a person, at phone width first.
 *
 * Most of the traffic is mobile, so `mobile` is the primary project: 390×844,
 * the same viewport we lint by hand. `desktop` runs the same journey to catch
 * the layout that only breaks wide.
 *
 * The app runs in its own container against a THROWAWAY data dir, so a test
 * run never writes into real leads/counters. Payments are unreachable without
 * ЮKassa keys (the app answers 503), which is exactly what we want — the suite
 * covers everything up to the paywall and never touches money.
 */
export default defineConfig({
	testDir: "./specs",
	fullyParallel: false, // the app keeps counters in one file; serial keeps them readable
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	workers: 1,
	reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
	timeout: 90_000, // the plan-builder screen alone runs ~23s
	expect: { timeout: 15_000 },

	use: {
		baseURL: process.env.BASE_URL ?? "http://localhost:8791",
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
		video: "off",
	},

	projects: [
		// iPhone 13 metrics, but on chromium: devices["iPhone 13"] defaults to
		// WebKit, and pinning one engine keeps the suite fast and the failures
		// about OUR css. Real Safari quirks still need a real phone.
		{
			name: "mobile",
			use: { ...devices["iPhone 13"], browserName: "chromium", defaultBrowserType: "chromium" },
		},
		{ name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
	],

	// Always build and run OUR image, on a port of our own.
	//
	// Two deliberate choices, both learned the hard way:
	//   - port 8791, not the app default 8790 — a hand-started uvicorn on 8790
	//     silently absorbed the whole suite once, and it "passed" against code
	//     that predated the fixes being tested.
	//   - reuseExistingServer: false — the suite must test the image built from
	//     this working tree, never whatever happens to be listening.
	// Point somewhere else deliberately with BASE_URL=... (then this is skipped).
	// BASE_URL задан → тестируем ЧУЖОЙ адрес (прод, стейдж), свой сервер не поднимаем.
	webServer: process.env.BASE_URL ? undefined : {
		command:
			"docker rm -f nutriplan-e2e >/dev/null 2>&1; " +
			"docker build -q -t nutriplan-e2e .. >/dev/null && " +
			// Фикстура плана подкладывается в data-каталог только для чтения.
			// Без неё /plan/sample собирается банк-фолбэком (ключа LLM в наборе
			// нет), а у банк-плана НЕТ списка покупок и рецептов — и тест на
			// список молча уходил в skip. Пропущенный тест ничего не проверяет.
			"docker run --rm --name nutriplan-e2e -p 8791:8790 -e DATA_DIR=/tmp/e2edata " +
			"-v \"$PWD/fixtures/plans/sample.json:/tmp/e2edata/plans/sample.json:ro\" " +
			// Планы и подписки для экранов состояния подписки. Каталог subs
			// монтируется целиком и только для чтения: подписку в тестах никто
			// не создаёт (касса недоступна), а иначе эти экраны не проверить —
			// их шесть, и все они про деньги.
			"-v \"$PWD/fixtures/plans/subended.json:/tmp/e2edata/plans/subended.json:ro\" " +
			"-v \"$PWD/fixtures/plans/subcanceled.json:/tmp/e2edata/plans/subcanceled.json:ro\" " +
			"-v \"$PWD/fixtures/subs:/tmp/e2edata/subs:ro\" nutriplan-e2e",
		url: "http://localhost:8791/api/health",
		reuseExistingServer: false,
		timeout: 300_000,
		stdout: "ignore",
		stderr: "pipe",
	},
});
