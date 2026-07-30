import { expect, test } from "./_fixtures";
const OUT = "/private/tmp/claude-501/-Users-Nikita-Desktop-travel-bot/58928fc4-da50-446b-8ae9-8d6101f35126/scratchpad/";
test("вариант 5 · профиль без графика", async ({ page }) => {
  const errs: string[] = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await page.goto("/assets/ux/v5.html");
  await page.waitForTimeout(1400);
  await page.locator('.nav button[data-s="me"]').click();
  await page.waitForTimeout(400);
  const g = await page.evaluate(() => {
    const me = document.querySelector("#s-me") as HTMLElement;
    const wcard = me.querySelector(".wcard") as HTMLElement;
    const delta = me.querySelector(".pcell.down") as HTMLElement;
    const bad: string[] = [];
    me.querySelectorAll("*").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 24 && r.right > document.documentElement.clientWidth + 1) bad.push((el.className||"").toString().slice(0,20));
    });
    return {
      график: me.querySelectorAll("svg, #gwrap").length,
      пилюля_в_карточке: !!wcard.querySelector(".gdelta"),
      дельта_ниже: !!delta && delta.getBoundingClientRect().top > wcard.getBoundingClientRect().bottom,
      дельта_текст: (delta?.textContent || "").replace(/\s+/g, " ").trim(),
      плиток: me.querySelectorAll(".pcell").length,
      вылезает: bad,
    };
  });
  console.log("PROF " + JSON.stringify(g));
  expect(errs).toEqual([]);
  expect(g.график, "график должен быть убран").toBe(0);
  expect(g.пилюля_в_карточке, "пилюля осталась в карточке веса").toBe(false);
  expect(g.дельта_ниже, "дельта должна быть ниже карточки веса").toBe(true);
  expect(g.вылезает).toEqual([]);
  await page.screenshot({ path: OUT + "ux-v5-me.png", fullPage: true });
});
