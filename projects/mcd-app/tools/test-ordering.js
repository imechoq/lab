/* End-to-end check for the ordering flow.
   Creates one real order against the proxy and cancels it afterwards, so the
   chain is proven without leaving a live order behind. */
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

const BASE = process.env.MCD_BASE || 'http://127.0.0.1:8777/qibao-mcd-app.html';

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 960 } });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  const R = { dialogs: [] };
  page.on('dialog', async (d) => { R.dialogs.push(d.message().split('\n')[0]); await d.accept(); });
  await page.goto(BASE, { waitUntil: 'load' });
  await page.waitForTimeout(2500);

  R.proxyOnline = await page.evaluate(() => document.querySelector('#snapStamp').textContent.includes('实时'));

  /* ---- add 麦香鱼套餐 (the item from the screenshot) ---- */
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(1500);
  /* Match the exact name so 麦香鱼薯翅四件套 does not get picked up. */
  const target = page.locator('.pc').filter({
    has: page.locator('.nm', { hasText: /^麦香鱼套餐$/ })
  }).first();
  R.itemFound = await target.count() > 0;
  R.itemName = await target.locator('.nm').innerText();
  R.itemPrice = await target.locator('.price').innerText();
  await target.locator('.nm').click();
  /* The detail sheet loads rounds asynchronously on first open. */
  await page.waitForSelector('#sheetAdd', { state: 'attached', timeout: 20000 });
  await page.waitForTimeout(800);
  R.detailOpen = await page.locator('#sheet.on').count();
  R.detailTitle = await page.locator('#sheetTitle').innerText();
  R.detailRounds = await page.locator('.round').count();
  await page.screenshot({ path: '/tmp/mcd-o1-detail.png' });
  await page.click('#sheetAdd');
  await page.waitForTimeout(1000);

  /* ---- cart ---- */
  R.cartCount = await page.locator('#cartCount').innerText();
  R.cartBill = await page.locator('.bill').innerText();

  /* ---- preview: the button must be enabled now ---- */
  await page.click('.rail [data-nav="preview"]');
  await page.waitForTimeout(600);
  const quoteBtn = page.locator('#quoteBtn');
  R.quoteBtnExists = await quoteBtn.count() > 0;
  R.quoteBtnEnabled = await quoteBtn.isEnabled();
  R.quoteBtnText = await quoteBtn.innerText();
  R.oldDisabledButtonGone = (await page.locator('.confirmbar .btn[disabled]').count()) === 0;
  await page.screenshot({ path: '/tmp/mcd-o2-preview.png' });

  /* ---- quote through calculate-price ---- */
  await quoteBtn.click();
  await page.waitForSelector('#createOrderBtn', { state: 'attached', timeout: 25000 });
  await page.waitForTimeout(600);
  R.quoteBlock = await page.locator('.sec').filter({ hasText: '接口试算结果' }).innerText();
  R.quoteTotal = await page.locator('.sec-head .pill.green').count();
  const createBtn = page.locator('#createOrderBtn');
  R.createBtnExists = await createBtn.count() > 0;
  R.createBtnText = await createBtn.count() ? await createBtn.innerText() : '';
  await page.screenshot({ path: '/tmp/mcd-o3-quoted.png' });

  /* ---- create the order ---- */
  if (await createBtn.count()) {
    await createBtn.click();
    await page.waitForTimeout(5000);
    R.orderBox = await page.locator('.orderbox').count();
    R.orderStatus = await page.locator('.orderbox-hd b').innerText().catch(() => '');
    R.orderId = await page.locator('.orderbox .kv div').first().innerText().catch(() => '');
    R.payLink = await page.locator('.orderbox a.btn').getAttribute('href').catch(() => null);
    R.cartCleared = await page.locator('#cartCount').innerText();
    await page.screenshot({ path: '/tmp/mcd-o4-ordered.png' });

    /* ---- cancel it again ---- */
    await page.click('#cancelOrderBtn');
    await page.waitForTimeout(4000);
    R.orderGone = (await page.locator('.orderbox').count()) === 0;
    R.cancelDialog = R.dialogs.some((d) => d.includes('取消'));
  }

  R.errors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})();
