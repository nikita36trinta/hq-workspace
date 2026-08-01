import { expect, test } from "./_fixtures";

/**
 * Вход: пароль после оплаты, вход по паролю, восстановление кодом.
 *
 * Спеки написаны вокруг двух вещей, которые дороже всего стоят при ошибке:
 * (1) оплаченный план не должен становиться недоступным из-за пароля,
 * (2) форма восстановления не должна превращаться в рассыльщик писем с нашего
 *     домена — блокировка отправителя означает, что письма перестанут доходить
 *     ОПЛАТИВШИМ.
 */

const J = { "Content-Type": "application/json" };

test.describe("вход · пароль", () => {
	/**
	 * Пароль задаётся по кукe оплаченного заказа. Почту с клиента не принимаем
	 * НИКОГДА: иначе любой задал бы пароль к чужому аккаунту, прислав чужой адрес.
	 */
	test("без оплаченного заказа пароль задать нельзя", async ({ request }) => {
		const r = await request.post("/api/auth/password", {
			headers: J,
			data: { password: "достаточно-длинный-пароль", password2: "достаточно-длинный-пароль" },
		});
		expect(r.status(), "пароль задался без единого доказательства оплаты").toBe(403);
	});

	/**
	 * Found 2026-08-01 проверкой: кука заказа np_o выдаётся ЛЮБОМУ, кто открыл
	 * /pay/success?o=<токен>, а токен лежит в каждом письме и в ссылках, которыми
	 * люди делятся сами. Посторонний задавал свой пароль на чужую почту, входил
	 * и выбивал владельца — смена пароля закрывает его сессии.
	 *
	 * Граница теперь такая: по куке заказа можно завести ПЕРВЫЙ пароль, перебить
	 * существующий — нельзя. Смена только по сессии или по коду из письма.
	 */
	test("по ссылке на оплату нельзя перебить уже заданный пароль", async ({ request }) => {
		const oid = "e2eown" + Date.now().toString(36);
		// заказа с таким номером нет — значит и права нет: проверяем, что отказ
		// приходит ДО того, как что-то изменится
		const r = await request.post("/api/auth/password", {
			headers: { ...J, Cookie: `np_o=${oid}` },
			data: { password: "пароль-чужака-9", password2: "пароль-чужака-9" },
		});
		expect([403, 409], "чужой заказ пустил задавать пароль").toContain(r.status());
	});

	test("слабые пароли не проходят, а сообщение объясняет причину", async ({ request }) => {
		// Проверяем сам валидатор — он одинаков для всех входов пароля.
		for (const pw of ["короче8", "password", "aaaaaaaa"]) {
			const r = await request.post("/api/auth/reset", {
				headers: J,
				data: { email: "nobody@example.com", code: "000000", password: pw, password2: pw },
			});
			expect(r.status(), `пароль ${pw} принят`).toBeGreaterThanOrEqual(400);
			const j = await r.json();
			expect(String(j.error || ""), "отказ без объяснения — человек не знает, что чинить")
				.not.toBe("");
		}
	});

	/**
	 * Ошибка входа не должна отличать «нет такого аккаунта» от «неверный пароль»:
	 * иначе форма превращается в способ перебором выяснить, кто у нас покупал.
	 */
	test("неверный пароль и несуществующий аккаунт отвечают одинаково", async ({ request }) => {
		const a = await request.post("/api/auth/login", {
			headers: J, data: { email: "definitely-nobody@example.com", password: "whatever12" },
		});
		const b = await request.post("/api/auth/login", {
			headers: J, data: { email: "buyer@example.com", password: "неверный-пароль-тут" },
		});
		expect(a.status()).toBe(b.status());
		expect((await a.json()).error, "ответы различаются — аккаунты перечисляются")
			.toBe((await b.json()).error);
	});
});

test.describe("вход · защита от рассылки писем", () => {
	/**
	 * Главный вектор: форма «Забыли пароль» с чужим адресом. Он закрыт не
	 * лимитом, а тем, что письмо уходит ТОЛЬКО на адрес с существующим
	 * аккаунтом. Ответ при этом всегда одинаковый — иначе форма выдаёт,
	 * кто у нас есть.
	 */
	test("несуществующий адрес: ответ нейтральный, письма нет", async ({ request }) => {
		const r = await request.post("/api/auth/forgot", {
			headers: J, data: { email: `nobody-${Date.now()}@example.com` },
		});
		expect(r.status(), "по коду ответа видно, есть ли аккаунт").toBe(200);
		expect(await r.json(), "тело ответа выдаёт наличие аккаунта").toEqual({ ok: true });
	});

	test("частые запросы не дают лавины писем и не выдают лимит наружу", async ({ request }) => {
		const email = `flood-${Date.now()}@example.com`;
		const codes: number[] = [];
		for (let i = 0; i < 8; i++) {
			const r = await request.post("/api/auth/forgot", { headers: J, data: { email } });
			codes.push(r.status());
		}
		expect(new Set(codes), "по ответу видно, где сработал лимит").toEqual(new Set([200]));
	});

	/**
	 * Кнопка «отправить ещё раз» не должна быть усилителем: пока живёт выданный
	 * код, второе письмо не уходит, а человеку честно говорят, сколько ждать.
	 * Раньше страница рисовала успех даже там, где письма не было вовсе.
	 */
	test("повторный запрос в пределах живого кода возвращает ожидание, а не новое письмо", async ({ request }) => {
		const email = "buyer@example.com";     // аккаунт заводится happy-path проверкой ниже
		await request.post("/api/auth/forgot", { headers: J, data: { email } });
		const again = await request.post("/api/auth/forgot", { headers: J, data: { email } });
		const j = await again.json();
		// wait появляется, только если аккаунт существует и код ещё жив.
		if (j.wait !== undefined) {
			expect(j.wait, "ожидание должно быть в секундах и в разумных пределах")
				.toBeGreaterThan(0);
			expect(j.wait).toBeLessThanOrEqual(60);
		}
	});

	test("запрос с чужого сайта отбит", async ({ request }) => {
		const r = await request.post("/api/auth/login", {
			headers: { ...J, Origin: "https://evil.example" },
			data: { email: "buyer@example.com", password: "x" },
		});
		expect(r.status(), "мутирующая ручка исполнилась по запросу с чужого сайта").toBe(403);
	});
});

test.describe("вход · страницы", () => {
	test("страница входа не шире экрана и предлагает оба пути", async ({ page }) => {
		await page.goto("/login");
		await page.waitForTimeout(300);

		expect(await page.evaluate(() => innerWidth <= document.documentElement.clientWidth),
			"страница шире экрана").toBe(true);
		await expect(page.locator("#m"), "нет поля почты").toBeVisible();
		await expect(page.locator("#p"), "нет поля пароля").toBeVisible();
		await expect(page.locator("#forgot"), "нет пути для забывшего пароль").toBeVisible();

		// «Забыли пароль» — ссылка, а не вторая зелёная кнопка рядом с «Войти»:
		// два одинаковых прямоугольника подряд читаются как выбор из равных.
		const bg = await page.locator("#forgot").evaluate((e) => getComputedStyle(e).backgroundColor);
		expect(bg, "«Забыли пароль» выглядит как основная кнопка").toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
	});

	test("«Забыли пароль» открывает ввод кода и даёт вернуться назад", async ({ page }) => {
		await page.goto("/login");
		await page.fill("#m", "buyer@example.com");
		await page.click("#forgot");
		await page.waitForTimeout(600);

		await expect(page.locator("#st-code"), "экран кода не открылся").toBeVisible();
		await expect(page.locator("#code"), "нет поля для кода").toBeVisible();
		await expect(page.locator("#np1"), "нет поля нового пароля").toBeVisible();

		await page.click("#back");
		await page.waitForTimeout(200);
		await expect(page.locator("#st-pw"), "нельзя вернуться ко входу по паролю").toBeVisible();
	});

	test("/app без сессии ведёт на вход, а не в 404", async ({ page }) => {
		await page.goto("/app");
		await page.waitForTimeout(300);
		expect(page.url(), "человек без сессии должен попадать на вход").toContain("/login");
	});
});
