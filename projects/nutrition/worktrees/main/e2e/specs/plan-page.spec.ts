import { expect, test } from "./_fixtures";
import { dismissWelcome } from "./_helpers";

/** Экран плана — то, что человек получает за деньги. /plan/sample открыт всем. */
test.describe("экран плана", () => {
	/**
	 * Found 2026-07-30 по скриншоту владельца: «что за чёрточка справа снизу».
	 *
	 * Это была кнопка «не нравится». У неё не было НИ ОДНОГО правила в CSS:
	 * инлайновый svg без размеров сплющивал её в вертикальную полоску рядом с
	 * «Заменить» — на экране читалось как случайный артефакт вёрстки.
	 *
	 * Вторая половина дефекта: подтверждение вторым нажатием сообщалось через
	 * title, то есть подсказку, которой на тач-экране НЕ БЫВАЕТ. Первый тап
	 * выглядел как «ничего не произошло».
	 */
	test("кнопка «не нравится» — кнопка, а не чёрточка", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(600);

		const g = await page.evaluate(() => {
			const m = [...document.querySelectorAll(".meal")]
				.find((e) => (e as HTMLElement).offsetParent !== null) as HTMLElement;
			const box = (sel: string) => {
				const e = m.querySelector(sel) as HTMLElement | null;
				if (!e) return null;
				const r = e.getBoundingClientRect();
				return { w: Math.round(r.width), h: Math.round(r.height) };
			};
			return { swap: box(".swap"), dislike: box(".dislike"), svg: box(".dislike svg") };
		});
		expect(g.dislike, "кнопки «не нравится» нет на экране").not.toBeNull();
		expect(g.dislike!.w, "кнопка сплющилась в чёрточку").toBeGreaterThanOrEqual(40);
		expect(g.dislike!.h, "высота должна совпадать с соседями").toBeGreaterThanOrEqual(g.swap!.h - 2);
		expect(g.svg!.w, "иконке не задан размер").toBeGreaterThanOrEqual(16);
	});

	test("первый тап по «не нравится» подтверждает видимой строкой, а не подсказкой", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(600);
		const meals = await page.locator(".meal").count();

		await page.evaluate(() => {
			const m = [...document.querySelectorAll(".meal")]
				.find((e) => (e as HTMLElement).offsetParent !== null)!;
			(m.querySelector(".dislike") as HTMLElement).click();
		});
		await page.waitForTimeout(300);

		const armed = await page.evaluate(() => {
			const m = [...document.querySelectorAll(".meal")]
				.find((e) => (e as HTMLElement).offsetParent !== null)!;
			const h = m.querySelector(".hintx") as HTMLElement | null;
			return { hintShown: !!h && getComputedStyle(h).display !== "none",
				buttonArmed: !!m.querySelector(".dislike.armed") };
		});
		expect(armed.hintShown, "первый тап обязан показать видимую подсказку").toBe(true);
		expect(armed.buttonArmed, "кнопка должна показать, что взведена").toBe(true);
		expect(await page.locator(".meal").count(), "первый тап не должен ничего удалять").toBe(meals);
	});
});

test.describe("экран плана · фото на весь экран", () => {
	test("тап по фото открывает его, Esc закрывает, прокрутка возвращается", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		await page.waitForTimeout(600);

		const opened = await page.evaluate(() => {
			const b = [...document.querySelectorAll(".mimg[data-zoom]")]
				.find((e) => (e as HTMLElement).offsetParent !== null) as HTMLElement;
			b.click();
			const lb = document.querySelector(".lb")!;
			return { on: lb.classList.contains("on"),
				src: lb.querySelector("img")!.getAttribute("src") ?? "",
				caption: lb.querySelector("figcaption")!.textContent ?? "",
				name: b.dataset.name ?? "",
				bodyLocked: document.body.style.overflow };
		});
		expect(opened.on, "просмотр не открылся").toBe(true);
		expect(opened.caption, "под фото должно стоять название блюда").toBe(opened.name);
		expect(opened.src, "для просмотра запрашиваем крупный вариант").toContain("lg=1");
		expect(opened.bodyLocked, "фон не должен ехать под открытым фото").toBe("hidden");

		await page.keyboard.press("Escape");
		await page.waitForTimeout(250);
		const closed = await page.evaluate(() => ({
			on: document.querySelector(".lb")!.classList.contains("on"),
			bodyLocked: document.body.style.overflow,
		}));
		expect(closed.on, "Escape должен закрывать просмотр").toBe(false);
		expect(closed.bodyLocked, "прокрутка обязана вернуться").toBe("");
	});

	/**
	 * Марка блюда стала <button> с <img> внутри, а догрузка фото по мере
	 * генерации искала 'img.mimg[data-slug]'. Такая перестановка тихо ломает
	 * догрузку: у первого покупателя фото просто не появятся.
	 */
	test("догрузка фото по-прежнему находит картинки", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		expect(
			await page.locator(".mimg img[data-slug]").count(),
			"селектор догрузки фото больше ничего не находит",
		).toBeGreaterThan(0);
	});
});

test.describe("экран плана · список покупок", () => {
	/**
	 * Found 2026-07-30: список отдавался стеной из 41 строки текста — ни одного
	 * элемента управления. То есть сделать с ним то, ради чего он нужен —
	 * отметить купленное в магазине, — было нельзя.
	 */
	test("позиции отмечаются, счётчик считает, отметки живут после перезагрузки", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);
		// Список переехал на свою вкладку: приложение больше не один свиток на
		// восемь экранов. Открываем её, как это делает человек.
		await page.locator('.bnav button[data-s="cart"]').click();
		await page.waitForTimeout(500);

		const shop = page.locator("#shop");
		if (!(await shop.count())) test.skip(true, "в этом плане нет списка покупок");

		const boxes = page.locator("#shop input[data-si]");
		const total = Number(await shop.getAttribute("data-total"));
		expect(await boxes.count(), "позиции должны быть переключателями").toBe(total);
		expect(total).toBeGreaterThan(3);

		await page.evaluate(() => {
			[...document.querySelectorAll("#shop input[data-si]")].slice(0, 3).forEach((b) => {
				(b as HTMLInputElement).checked = true;
				b.dispatchEvent(new Event("change", { bubbles: true }));
			});
		});
		await expect(page.locator("#sdone"), "счётчик купленного не обновился").toHaveText("3");
		expect(
			await page.evaluate(() => getComputedStyle(document.querySelector("#shop .si input:checked ~ .st")!).textDecorationLine),
			"купленное должно зачёркиваться",
		).toContain("line-through");

		await page.reload();
		await dismissWelcome(page);
		await page.locator('.bnav button[data-s="cart"]').click();
		await page.waitForTimeout(600);
		expect(await page.locator("#shop input:checked").count(),
			"отметки обязаны пережить перезагрузку — список нужен в магазине").toBe(3);

		await page.locator("#sclear").click();
		await page.waitForTimeout(200);
		expect(await page.locator("#shop input:checked").count(), "«Снять отметки» не сработала").toBe(0);
		await expect(page.locator("#sdone")).toHaveText("0");
	});
});

test.describe("экран плана · вода", () => {
	/**
	 * Выпитые стаканы были того же зелёного, что «Приготовил» и серия дней. Две
	 * разные привычки читались как одна шкала. Вода — синяя.
	 */
	test("выпитый стакан синий, а не зелёный", async ({ page }) => {
		await page.goto("/plan/sample");
		await dismissWelcome(page);

		const cups = page.locator(".cup");
		expect(await cups.count(), "стаканов нет на экране").toBeGreaterThan(3);
		await cups.nth(2).click();
		await page.waitForTimeout(250);

		const c = await page.evaluate(() => {
			const root = getComputedStyle(document.documentElement);
			const f = document.querySelector(".cup.f") as HTMLElement;
			const e = document.querySelector(".cup:not(.f)") as HTMLElement;
			const hex = (v: string) => v.trim().toLowerCase();
			return { filled: getComputedStyle(f).color, empty: getComputedStyle(e).color,
				water: hex(root.getPropertyValue("--wtr")), green: hex(root.getPropertyValue("--g")) };
		});
		// сравниваем по каналам: getComputedStyle отдаёт rgb(), токены — hex
		const rgb = (h: string) => {
			const m = h.replace("#", "");
			return `rgb(${parseInt(m.slice(0, 2), 16)}, ${parseInt(m.slice(2, 4), 16)}, ${parseInt(m.slice(4, 6), 16)})`;
		};
		expect(c.filled, "выпитый стакан должен быть цветом воды").toBe(rgb(c.water));
		expect(c.filled, "и не должен совпадать с зелёным «выполнено»").not.toBe(rgb(c.green));
		expect(await page.locator(".cup.f").count(), "залиться должны стаканы до нажатого").toBe(3);
	});
});
