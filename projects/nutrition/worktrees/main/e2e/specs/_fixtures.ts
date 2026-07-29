import { test as base } from "@playwright/test";

/**
 * Любой прогон идёт в режиме «не считать меня».
 *
 * Это не удобство, а требование: первый же запуск против прода добавил живые
 * лиды и накрутил заходы в квиз, и их пришлось выковыривать из leads.jsonl и
 * counters.json руками. Полагаться на то, что человек не забудет флаг, нельзя —
 * кука ставится самим набором, до первого перехода.
 *
 * Тест, которому нужно поведение БЕЗ notrack, снимает куку сам.
 */
export const test = base.extend({
	context: async ({ context, baseURL }, use) => {
		const url = new URL(baseURL ?? "http://localhost:8791");
		await context.addCookies([
			{ name: "np_notrack", value: "1", domain: url.hostname, path: "/" },
		]);
		await use(context);
	},
});

export { expect } from "@playwright/test";
