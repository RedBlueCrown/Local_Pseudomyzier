// Browser smoke test for the review app (run by tests/test_ui.py).
// usage: node smoke.js <app url> <decision.docx> <screenshot dir|-> [deviceScaleFactor]
// Uses only synthetic test documents. Prints "OK" when every step passed.
"use strict";
const { chromium } = require("playwright");
const path = require("path");
const [URL, DOCX, SHOTS, SCALE] = process.argv.slice(2);

function check(cond, msg) { if (!cond) throw new Error("check failed: " + msg); }

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: Number(SCALE || 1), acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const shot = async (name) => { if (SHOTS && SHOTS !== "-") await page.screenshot({ path: path.join(SHOTS, name + ".png") }); };
  try {
    await page.goto(URL);
    await page.waitForSelector("#drop");
    await shot("01-home");

    await page.setInputFiles("#file", DOCX);
    await page.waitForSelector("#paper", { timeout: 60000 });
    await shot("02-review");
    const marks = await page.locator("mark.m").count();
    const open = await page.locator("mark.d-open").count();
    check(marks > 10, "marks rendered");
    check(open > 0, "fixture has open questions");

    // export is blocked while questions are open
    await page.click("#export-btn");
    await page.waitForTimeout(500);
    check(!(await page.locator(".modal").count()), "no export with open questions");

    // popover on a mark, decide with the keyboard
    await page.click("mark.m >> nth=0");
    await page.waitForSelector("#popover:not([hidden])");
    await shot("03-popover");
    await page.keyboard.press("Escape");
    for (let i = 0; i < 40 && (await page.locator("mark.d-open").count()); i++) {
      await page.keyboard.press("n"); await page.waitForTimeout(400);
      if (i === 0) await shot("04-question");
      await page.keyboard.press("a"); await page.waitForTimeout(250);
    }
    check((await page.locator("mark.d-open").count()) === 0, "all questions decided");

    // keep one safe change as original via the popover
    await page.click("mark.d-accept >> nth=1");
    await page.waitForSelector("#popover:not([hidden])");
    await page.click('#popover [data-act="reject"]');
    await page.waitForTimeout(300);
    check((await page.locator("mark.d-reject").count()) >= 1, "reject recorded");

    // select a word and replace it manually ("[…]")
    const target = () => [...document.querySelectorAll("#paper p[data-pid]")].filter((p) => !p.querySelector("mark") && /\p{L}{8,}/u.test(p.textContent)).pop();
    await page.evaluate(`(${target})().scrollIntoView({ block: "center" })`);
    await page.waitForTimeout(400);
    const word = await page.evaluate((src) => {
      const p = eval(src)();
      const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
      let node; while ((node = walker.nextNode()) && !/\p{L}{8,}/u.test(node.data));
      const m = /\p{L}{8,}/u.exec(node.data);
      const r = document.createRange(); r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      p.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      return m[0];
    }, `(${target})`);
    await page.waitForSelector("#seltool:not([hidden])");
    await shot("05-selection");
    await page.click("#seltool [data-type=manual]");
    await page.waitForSelector("#man-in");
    check((await page.textContent(".modal .callout")).trim() === word, "selection maps to the right text");
    await page.fill("#man-in", "[…]");
    await page.click(".modal-f .btn.primary");
    await page.waitForSelector("mark.c-manual");

    // result view, settings, entities
    await page.click(".seg [data-mode=result]");
    await shot("06-result");
    const shown = await page.evaluate(() => document.querySelector("mark.c-manual").innerText.trim());
    check(shown === "[…]", "manual edit shown in result view (" + shown + ")");
    await page.click(".seg [data-mode=markup]");
    await page.click(".tabs [data-tab=entities]");
    await shot("07-entities");
    await page.click("#settings-btn");
    await page.waitForSelector("#set-url");
    await shot("08-settings");
    await page.keyboard.press("Escape");

    // export + download
    await page.click("#export-btn");
    await page.waitForSelector("#dl-clean", { timeout: 60000 });
    await shot("09-export");
    const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#dl-clean")]);
    check(/anonymisiert\.docx$/.test(dl.suggestedFilename()), "download name");
    check(!!(await dl.path()), "download saved");
    await page.click(".modal .x");

    await page.click('a[href="#/"]');
    await page.waitForSelector("table.cases");
    check((await page.textContent("table.cases")).includes("Exportiert"), "case marked exported");
    await shot("10-cases");
    check(errors.length === 0, "no browser errors: " + errors.join(" | "));
    console.log("OK");
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e.message); process.exit(1); });
