import { expect, test } from "./_fixtures";
import { submitEmail, walkQuizToPaywall } from "./_helpers";

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

test.describe("regressions · multi-select", () => {
	/**
	 * Found 2026-07-29: выбор варианта на шаге «можно выбрать несколько» вызывал
	 * renderChoice(st) — полную пересборку stage.innerHTML. Вместе с DOM заново
	 * запускалась входная анимация .step и перезапускалось видео маскота: экран
	 * моргал на каждый тап и выглядел так, будто отрисовывается с нуля.
	 *
	 * Проверяем не «нет анимации» (её не измерить надёжно), а причину: узел
	 * экрана должен ПЕРЕЖИТЬ выбор.
	 */
	test("выбор нескольких вариантов не пересобирает экран", async ({ page }) => {
		await page.goto("/quiz?l=slim");

		// доходим до первого шага с множественным выбором
		for (let i = 0; i < 12; i++) {
			const txt = await page.locator("#stage").innerText();
			if (/Можно выбрать несколько/i.test(txt)) break;
			const opts = page.locator("#stage button.opt");
			if ((await opts.count()) > 0 && (await page.locator("#stage button.opt.sel").count()) === 0) {
				await opts.first().click();
			}
			const next = page.locator("#next");
			if (await next.isVisible().catch(() => false)) await next.click();
			await page.waitForTimeout(250);
		}
		expect(
			/Можно выбрать несколько/i.test(await page.locator("#stage").innerText()),
			"не дошли до шага с множественным выбором",
		).toBe(true);

		// метим текущий узел экрана — пересборка innerHTML его уничтожит
		await page.evaluate(() => {
			const el = document.querySelector("#stage .step") as HTMLElement | null;
			if (el) el.dataset.mark = "keep";
		});

		const opts = page.locator("#stage button.opt");
		await opts.nth(1).click();
		await page.waitForTimeout(200);
		await opts.nth(2).click();
		await page.waitForTimeout(200);

		const survived = await page.evaluate(
			() => (document.querySelector("#stage .step") as HTMLElement | null)?.dataset.mark === "keep",
		);
		expect(survived, "экран пересобрался на выборе варианта — вернулось моргание").toBe(true);
		expect(await page.locator("#stage button.opt.sel").count(), "оба варианта должны остаться выбранными").toBe(2);
	});
});

test.describe("regressions · возврат с оплаты", () => {
	/**
	 * Found 2026-07-29: страница /pay/success показывала «Оплата получена!» во
	 * ВСЕХ случаях, кроме явного canceled. Человек закрывал окно ЮKassa не
	 * заплатив (статус остаётся pending), возвращался — и читал, что деньги
	 * получены. То же самое при недоступном API и при ненайденном заказе.
	 *
	 * По HTTP воспроизводится только ветка «заказа нет» — остальные требуют
	 * ответа ЮKassa и закреплены в tests_pay_states.py.
	 */
	test("несуществующий заказ не выдаётся за оплаченный", async ({ page }) => {
		await page.goto("/pay/success?o=definitelynotanorder");
		const h1 = page.locator("h1");
		await expect(h1).toBeVisible();
		await expect(h1, "утверждать оплату, ничего о ней не зная, нельзя").not.toContainText(/Оплата получена/i);
		await expect(h1).toContainText(/Не нашли/i);
	});

	test("страницы оплаты красят фон на всю ширину", async ({ page }) => {
		await page.goto("/pay/success?o=definitelynotanorder");
		const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
		expect(bg, "фон должен быть на body, иначе по бокам белые поля").toBe("rgb(251, 248, 241)");
	});
});

test.describe("regressions · пример дня на пейволле", () => {
	/**
	 * Found 2026-07-30 по скриншоту от владельца.
	 *
	 * Две разные вещи ломались одновременно.
	 *
	 * 1) Жирной строкой стоял ПРИЁМ ПИЩИ («Завтрак»), а название блюда не
	 *    показывалось вообще. Человек читал «Завтрак · Яйца, помидоры, зелень»
	 *    и не понимал, что ему предлагают омлет. Название блюда — это то, что
	 *    мы продаём, оно обязано быть главным в строке.
	 *
	 * 2) Ответы квиза живут в localStorage сутки, поэтому экран показывал
	 *    ограничения прошлого прохода. Отсюда ?reset=1 — начать с чистого листа.
	 */
	test("в строке дня крупным идёт название блюда, а не приём пищи", async ({ page }) => {
		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);
		await submitEmail(page, "day-title@example.com");

		const rows = page.locator("#dayBlock .meal");
		expect(await rows.count(), "пример дня должен быть на экране").toBeGreaterThan(1);

		const first = rows.first();
		const title = (await first.locator(".mt b").textContent())?.trim() ?? "";
		const sub = (await first.locator(".mt span").textContent())?.trim() ?? "";
		const label = (await first.locator(".kc i").textContent())?.trim() ?? "";

		expect(title, "жирным должно быть название блюда, а не «Завтрак»").not.toMatch(/^(Завтрак|Обед|Ужин)$/);
		expect(title.length, "название блюда не может быть пустым").toBeGreaterThan(3);
		expect(sub, "серой строкой идёт состав блюда").not.toMatch(/^(Завтрак|Обед|Ужин)/);
		expect(sub.length, "состав блюда не может быть пустым").toBeGreaterThan(3);
		// Приём пищи уехал в правую колонку — в левой на телефоне всего 156px,
		// и «Завтрак · Яйца, помидоры, зелень» там ломалось пополам.
		expect(label, "приём пищи должен остаться на экране, над калориями").toBe("Завтрак");

		// Ни одна строка в примере дня не должна переноситься на второй раз —
		// на этом экране правки уже дважды ломали вёрстку переносами.
		const wraps = await rows.evaluateAll((els) =>
			els.map((el) => {
				const s = el.querySelector(".mt span") as HTMLElement | null;
				if (!s) return 0;
				return Math.round(s.getBoundingClientRect().height / parseFloat(getComputedStyle(s).lineHeight));
			}),
		);
		expect(Math.max(...wraps), "состав блюда обязан уложиться в одну строку").toBeLessThanOrEqual(1);

		// Без ограничений в еде показываем дефолт, который выбрал владелец.
		const shown = await page.locator("#dayBlock").innerText();
		expect(shown, "по умолчанию завтрак — омлет").toContain("Омлет");
		expect(shown, "по умолчанию обед — рыба").toContain("Рыба");
	});

	test("?reset=1 стирает ответы прошлого прохода", async ({ page }) => {
		await page.goto("/quiz?l=slim");
		await page.evaluate(() => localStorage.setItem("np_quiz_v2", JSON.stringify({
			answers: { goal: "lose", diet: ["nomeat", "nolact", "nogluten"] },
			idx: 99, planRevealed: true, leadEmail: "old@example.com", t: Date.now(),
		})));

		// без сброса состояние подхватывается
		await page.goto("/quiz?l=slim");
		expect(await page.evaluate(() => localStorage.getItem("np_quiz_v2"))).not.toBeNull();

		await page.goto("/quiz?l=slim&reset=1");
		expect(
			await page.evaluate(() => localStorage.getItem("np_quiz_v2")),
			"?reset=1 обязан стереть сохранённые ответы",
		).toBeNull();
		// адрес чистится, иначе обновление страницы стирает уже настоящий проход
		expect(await page.evaluate(() => location.search)).not.toContain("reset");
	});
});

test.describe("regressions · гейт почты", () => {
	/**
	 * Found 2026-07-30: на месте нормы стояли четыре точки «••••» и замок.
	 * Точки не сообщали, ЧТО откроется, и карточка читалась как заглушка, а не
	 * как готовый результат. Теперь на своих местах вся строка КБЖУ, числа
	 * размыты — видно, что за почтой норма и три макронутриента.
	 *
	 * Размыто СВОЁ число, посчитанное по ответам: подписи и разрядность
	 * настоящие, поэтому обещание совпадает с тем, что откроется.
	 */
	test("гейт показывает всю строку КБЖУ, а не точки", async ({ page }) => {
		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);

		const cells = page.locator(".lockcell");
		expect(await cells.count(), "норма и три макронутриента").toBe(4);
		await expect(page.locator(".lockrow"), "точки вернулись вместо чисел").not.toContainText("••");

		const data = await cells.evaluateAll((els) =>
			els.map((el) => {
				const n = el.querySelector(".n") as HTMLElement;
				const c = el.querySelector(".c") as HTMLElement;
				return {
					num: (n.textContent ?? "").trim(),
					blur: getComputedStyle(n).filter,
					capLines: Math.round(c.getBoundingClientRect().height / parseFloat(getComputedStyle(c).lineHeight)),
				};
			}),
		);
		for (const d of data) {
			expect(d.num, "в гейте должно стоять настоящее посчитанное число").toMatch(/^\d+$/);
			expect(d.blur, "число обязано быть размыто до отправки почты").toContain("blur");
			expect(d.capLines, "подпись под числом не должна переноситься").toBeLessThanOrEqual(1);
		}

		// и после почты те же числа открываются как есть
		await submitEmail(page, "gate@example.com");
		const kcal = (await page.locator(".kcal .big").textContent())?.trim();
		expect(kcal, "открытая норма должна совпасть с тем, что было размыто").toBe(data[0].num);
	});
});

test.describe("regressions · заливка под кнопкой", () => {
	/**
	 * Found 2026-07-30 по скриншоту владельца: под кнопкой был виден светлый
	 * прямоугольник с прямыми краями.
	 *
	 * Заливка была фоном самого .foot, поэтому её края совпадали с его боксом:
	 * по бокам — отступы .shell, снизу — на 11px выше низа страницы (высоту
	 * подвала задаёт flex, в CSS её заранее не посчитать). Под ней лежит не
	 * ровный --bg, а свечение и фото блюд, поэтому края читались. Стоило
	 * докрутить до конца — подвал отлипал, и прямоугольник оставался посреди
	 * контента.
	 *
	 * Теперь заливка — фиксированный ::before у нижней кромки окна: раскладки
	 * она не знает, значит и обрываться ей негде.
	 */
	test("заливка прибита к окну, а не к боксу подвала", async ({ page }) => {
		await page.goto("/quiz?l=slim");
		await walkQuizToPaywall(page);
		await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
		await page.waitForTimeout(400);

		const foot = page.locator(".foot");
		expect(
			await foot.evaluate((e) => getComputedStyle(e).backgroundImage),
			"фон вернулся на сам подвал — снова будут видны края",
		).toBe("none");
		expect(
			await foot.evaluate((e) => getComputedStyle(e, "::before").position),
			"заливка обязана быть fixed",
		).toBe("fixed");

		// и она действительно доходит до кромок окна
		const g = await page.evaluate(() => {
			const probe = document.createElement("div");
			probe.style.cssText = "position:fixed;left:0;right:0;bottom:0;height:150px";
			document.body.appendChild(probe);
			const r = probe.getBoundingClientRect();
			probe.remove();
			return { left: Math.round(r.left), right: Math.round(r.right), bottom: Math.round(r.bottom),
				vw: document.documentElement.clientWidth, vh: window.innerHeight };
		});
		expect(g.left).toBe(0);
		expect(g.right).toBe(g.vw);
		expect(g.bottom).toBe(g.vh);
	});
});
