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

const BASE = process.argv[2] || 'http://127.0.0.1:8791/';

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(BASE, { waitUntil: 'load' });
  await page.waitForTimeout(5000);
  const R = {};

  R.mode = (await page.locator('#dataNote').innerText()).slice(0, 60);
  R.store = await page.locator('.hero h1').innerText();
  R.pills = await page.locator('.hero .pill').allInnerTexts();
  await page.waitForTimeout(2500);
  R.images = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('.rec img')];
    return { total: imgs.length, loaded: imgs.filter((i) => i.complete && i.naturalWidth > 0).length };
  });
  await page.screenshot({ path: '/tmp/v-01-home.png' });

  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(1500);
  R.menuCards = await page.locator('.pc').count();
  R.kcalTags = await page.evaluate(() => [...document.querySelectorAll('.pc .tg i')].filter((n) => /kcal/.test(n.textContent)).length);
  await page.screenshot({ path: '/tmp/v-02-menu.png' });

  /* 单品详情：营养块 */
  const burger = page.locator('.pc .nm').filter({ hasText: /^麦辣鸡腿汉堡$/ }).first();
  if (await burger.count()) {
    await burger.click();
    await page.waitForTimeout(1500);
    R.detail = {
      title: await page.locator('#sheetTitle').innerText(),
      nutri: await page.locator('.nutri div').allInnerTexts(),
      rounds: await page.locator('.round').count(),
      opts: await page.locator('.opt').count(),
    };
    await page.screenshot({ path: '/tmp/v-03-detail.png' });
    await page.click('#sheetAdd');
    await page.waitForTimeout(800);
  }

  /* 预览 + 真实试算（不建单） */
  R.cartLines = await page.locator('.cart-line').count();
  await page.click('.rail [data-nav="preview"]');
  await page.waitForTimeout(600);
  R.quoteBtnBefore = await page.locator('#quoteBtn').innerText();
  await page.click('#quoteBtn');
  await page.waitForTimeout(6000);
  R.quoteBlock = await page.evaluate(() => {
    const heads = [...document.querySelectorAll('.sec h2')];
    const h = heads.find((n) => n.textContent.includes('接口试算结果'));
    if (!h) return null;
    return h.closest('.sec').innerText.replace(/\n+/g, ' | ').slice(0, 300);
  });
  R.createBtn = await page.evaluate(() => {
    const b = document.getElementById('createOrderBtn');
    return b ? b.innerText : null;
  });
  await page.screenshot({ path: '/tmp/v-04-quote.png' });

  /* 门店选择器 */
  await page.click('.rail [data-nav="home"]');
  await page.waitForTimeout(500);
  await page.click('#addrChip');
  await page.waitForTimeout(4000);
  R.pickRows = await page.locator('#pickList .pick-row').count();
  await page.screenshot({ path: '/tmp/v-05-picker.png' });
  const target = page.locator('#pickList .pick-row').nth(1);
  if (await target.count()) {
    await target.click();
    await page.waitForTimeout(12000);
    R.storeAfter = await page.locator('.hero h1').innerText();
    R.pillsAfter = await page.locator('.hero .pill').allInnerTexts();
    await page.screenshot({ path: '/tmp/v-06-store2.png' });
  }

  /* 移动端 */
  const m = await ctx.newPage();
  await m.setViewportSize({ width: 390, height: 844 });
  m.on('pageerror', (e) => errors.push('mobile pageerror: ' + e.message));
  await m.goto(BASE, { waitUntil: 'load' });
  await m.waitForTimeout(5000);
  await m.screenshot({ path: '/tmp/v-07-mobile.png' });
  await m.click('.bottom [data-nav="menu"]');
  await m.waitForTimeout(1500);
  R.mobileOverflow = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  await m.screenshot({ path: '/tmp/v-08-mobile-menu.png' });

  R.errors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})();
