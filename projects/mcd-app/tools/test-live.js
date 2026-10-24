const path = require('path');

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    'playwright',
    path.join(process.env.HOME || '', '.npm-global/lib/node_modules/playwright'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (e) { /* try next */ }
  }
  throw new Error(
    'playwright not found. Install it with:  npm i -D playwright\n' +
    'or set PLAYWRIGHT_MODULE=/path/to/playwright');
}

const { chromium } = loadPlaywright();

const BASE = process.argv[2] || 'http://127.0.0.1:8779/';

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(BASE, { waitUntil: 'load' });
  await page.waitForTimeout(3500);
  const R = {};

  R.mode = await page.evaluate(() => (document.getElementById('dataNote') || {}).innerText || '');
  R.store = await page.locator('.hero h1').innerText();
  R.productPill = await page.locator('.hero .pill').allInnerTexts();
  R.recCards = await page.locator('.rec').count();
  await page.waitForTimeout(2500);
  R.images = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('.rec img')];
    return { total: imgs.length, loaded: imgs.filter((i) => i.complete && i.naturalWidth > 0).length };
  });
  await page.screenshot({ path: '/tmp/live-01-home.png' });

  /* ---- 菜单 ---- */
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(1200);
  R.menuCards = await page.locator('.pc').count();
  R.kcalTags = await page.locator('.pc .tg i').filter({ hasText: 'kcal' }).count();
  R.specButtons = await page.locator('.pc .add.spec').count();
  await page.screenshot({ path: '/tmp/live-02-menu.png' });

  /* ---- 单品详情的营养块 ---- */
  const burger = page.locator('.pc .nm').filter({ hasText: /^麦辣鸡腿汉堡$/ }).first();
  R.burgerFound = await burger.count();
  await burger.click();
  await page.waitForTimeout(1200);
  R.detailTitle = await page.locator('#sheetTitle').innerText();
  R.nutriCells = await page.locator('.nutri div').allInnerTexts();
  R.provenance = await page.locator('#sheetBody .hint').allInnerTexts();
  await page.screenshot({ path: '/tmp/live-03-detail.png' });
  await page.click('#sheetAdd');
  await page.waitForTimeout(600);
  R.cartLine = await page.locator('.cart-line').count();
  R.bill = await page.locator('.bill').innerText();

  /* ---- 门店选择器 ---- */
  await page.click('.rail [data-nav="home"]');
  await page.waitForTimeout(400);
  await page.click('#addrChip');
  await page.waitForTimeout(2500);
  R.pickerOpen = await page.locator('#pickPanel.on').count();
  R.pickRows = await page.locator('#pickList .pick-row').count();
  const first = page.locator('#pickList .pick-row').nth(0);
  R.pickFirstName = await first.innerText();
  await page.screenshot({ path: '/tmp/live-04-picker.png' });

  /* ---- 门店选择器三个数据源 ---- */
  await page.click('[data-picktab="deliver"]');
  await page.waitForTimeout(6000);
  R.deliverRows = await page.locator('#pickList .pick-row').count();
  await page.click('[data-picktab="all"]');
  await page.waitForTimeout(12000);
  R.allRows = await page.locator('#pickList .pick-row').count();
  await page.screenshot({ path: '/tmp/live-04b-picker-all.png' });
  await page.click('[data-picktab="nearby"]');
  await page.waitForTimeout(2500);

  const target = page.locator('#pickList .pick-row').nth(1);
  const targetCode = await target.getAttribute('data-store');
  await target.click();
  await page.waitForTimeout(9000);
  R.storeAfter = await page.locator('.hero h1').innerText();
  R.pickClosed = await page.locator('#pickPanel.on').count();
  R.pillAfter = await page.locator('.hero .pill').allInnerTexts();
  R.codeChanged = targetCode;
  await page.screenshot({ path: '/tmp/live-05-after-switch.png' });

  /* ---- 团餐 ---- */
  await page.click('[data-channel="6"]');
  await page.waitForTimeout(9000);
  R.groupPill = await page.locator('.hero .pill').allInnerTexts();
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(800);
  R.groupCards = await page.locator('.pc').count();
  const add = page.locator('.pc .add:not(.spec)').first();
  if (await add.count()) {
    await add.click();
    await page.waitForTimeout(400);
    await page.click('.rail [data-nav="preview"]');
    await page.waitForTimeout(500);
    R.groupWarn = await page.locator('.warnbox').count();
    for (let i = 0; i < 22; i++) {
      const inc = page.locator('[data-cart-inc="0"]');
      if (!(await inc.count())) break;
      await inc.click();
      await page.waitForTimeout(60);
      if (!(await page.locator('.warnbox').count())) break;
    }
    R.groupBill = await page.locator('.bill').innerText();
    await page.screenshot({ path: '/tmp/live-06-group.png' });
  }

  /* ---- 移动端 ---- */
  const m = await ctx.newPage();
  await m.setViewportSize({ width: 390, height: 844 });
  m.on('pageerror', (e) => errors.push('mobile pageerror: ' + e.message));
  await m.goto(BASE, { waitUntil: 'load' });
  await m.waitForTimeout(3500);
  await m.screenshot({ path: '/tmp/live-07-mobile.png' });
  await m.click('.bottom [data-nav="menu"]');
  await m.waitForTimeout(1500);
  R.mobileOverflow = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  await m.screenshot({ path: '/tmp/live-08-mobile-menu.png' });

  R.errors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})();
