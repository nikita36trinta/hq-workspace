import { expect, test } from "./_fixtures";
const L = ["slim","easy","pro","chef","coach","reset","energy","fitness"];
for (const l of L) {
  test(`цены на /l/${l}: видны, не шире экрана, не в первом экране`, async ({ page }) => {
    await page.goto(`/l/${l}`);
    const s = page.locator('section[aria-label="Стоимость"]');
    await expect(s).toHaveCount(1);
    const m = await s.evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { top: r.top + window.scrollY, w: r.width, vw: document.documentElement.clientWidth,
               vh: window.innerHeight, docH: document.documentElement.scrollHeight,
               txt: (el.textContent || "").replace(/\s+/g, " ").trim() };
    });
    expect(m.w, "блок шире экрана").toBeLessThanOrEqual(m.vw + 1);
    expect(m.top, "цены не должны быть в первом экране").toBeGreaterThan(m.vh * 2);
    expect(m.txt).toContain("299 ₽");
    expect(m.txt).toContain("499 ₽/мес");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
      "страница поехала вбок").toBeTruthy();
  });
}
