/* End-to-end check for the store-switching rework.
   Runs against the local proxy so the live paths are exercised too. */
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
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(BASE, { waitUntil: 'load' });
  await page.waitForTimeout(2500);
  const R = {};

  /* ---- proxy detected? ---- */
  R.proxyOnline = await page.evaluate(() => !!document.querySelector('#snapStamp') &&
    document.querySelector('#snapStamp').textContent.includes('实时'));
  R.stamp = await page.locator('#snapStamp').innerText();

  /* ---- address chip shows the live store ---- */
  R.addrName = await page.locator('#addrName').innerText();
  R.addrSub = await page.locator('#addrSub').innerText();

  /* ---- picker opens in place, no scroll jump ---- */
  await page.evaluate(() => window.scrollTo(0, 0));
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await page.click('#addrChip');
  await page.waitForTimeout(400);
  R.pickerOpen = await page.locator('#pickPanel.on').count();
  R.pickerRows = await page.locator('.pick-row').count();
  R.scrollAfterOpen = await page.evaluate(() => window.scrollY);
  R.noScrollJump = scrollBefore === R.scrollAfterOpen;
  R.pickerBelowTopbar = await page.evaluate(() => {
    const p = document.querySelector('#pickPanel').getBoundingClientRect();
    const t = document.querySelector('.topbar').getBoundingClientRect();
    return p.top >= t.bottom - 2;
  });
  await page.screenshot({ path: '/tmp/mcd-n1-picker.png' });

  /* ---- every picker tab must return data without a server error ---- */
  R.tabs = {};
  for (const tab of ['nearby', 'deliver', 'fav', 'all']) {
    await page.click(`[data-picktab="${tab}"]`);
    await page.waitForTimeout(1800);
    R.tabs[tab] = await page.locator('.pick-row').count();
  }
  /* back to nearby for the switch test */
  await page.click('[data-picktab="nearby"]');
  await page.waitForTimeout(1500);

  /* ---- switch store from the picker ---- */
  await page.click('[data-picktab="nearby"]');
  await page.waitForTimeout(1800);
  const firstRowName = await page.locator('.pick-row .tx b').first().innerText();
  await page.locator('.pick-row').nth(1).click();
  await page.waitForTimeout(3000);
  R.switchedTo = await page.locator('#addrName').innerText();
  R.switchedOk = R.switchedTo !== firstRowName;
  R.pickerClosed = (await page.locator('#pickPanel.on').count()) === 0;
  R.heroStore = await page.locator('.hero h1').innerText();
  R.scrollAfterSwitch = await page.evaluate(() => window.scrollY);
  R.switchNoScroll = R.scrollAfterSwitch === 0;
  await page.screenshot({ path: '/tmp/mcd-n2-switched.png' });

  /* ---- menu reflects the new store (live menu load) ---- */
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(2000);
  R.menuCards = await page.locator('.pc').count();
  R.menuHeadline = await page.locator('.sec-head p').first().innerText();

  /* ---- combo detection: a 套餐 item must offer spec selection ---- */
  R.comboSpecButtons = await page.locator('.pc .add.spec').count();
  const comboNamed = page.locator('.pc').filter({ hasText: '套餐' }).first();
  if (await comboNamed.count()) {
    R.comboSample = await comboNamed.locator('.nm').innerText();
    await comboNamed.locator('.nm').click();
    await page.waitForTimeout(1200);
    R.comboSheetOpen = await page.locator('#sheet.on').count();
    R.comboRounds = await page.locator('.round').count();
    R.comboTitle = await page.locator('#sheetTitle').innerText();
    await page.screenshot({ path: '/tmp/mcd-n3-combo.png' });
    await page.click('#sheetClose');
    await page.waitForTimeout(250);
  }

  /* ---- store page ---- */
  await page.click('.rail [data-nav="stores"]');
  await page.waitForTimeout(600);
  R.storeCards = await page.locator('.store-card').count();
  R.storePageHeading = await page.locator('.stores-top h2').innerText();
  await page.screenshot({ path: '/tmp/mcd-n4-storepage.png' });

  /* ---- search inside the store page ---- */
  await page.fill('#storeSearch', '七宝');
  await page.waitForTimeout(400);
  R.searchCards = await page.locator('.store-card').count();
  await page.fill('#storeSearch', '');
  await page.waitForTimeout(300);

  /* ---- sorting ---- */
  await page.click('[data-sort="name"]');
  await page.waitForTimeout(300);
  R.sortByNameFirst = await page.locator('.store-card .hd b').first().innerText();
  await page.click('[data-sort="distance"]');
  await page.waitForTimeout(300);
  R.sortByDistanceFirst = await page.locator('.store-card .hd b').first().innerText();

  /* ---- pick a store from the page, cart must reset ---- */
  await page.click('.rail [data-nav="home"]');
  await page.waitForTimeout(300);
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(800);
  const addBtn = page.locator('.pc .add:not(.spec)').first();
  if (await addBtn.count()) {
    await addBtn.click();
    await page.waitForTimeout(900);
    /* A combo opens the spec sheet; close it so the page stays interactive. */
    if (await page.locator('#sheet.on').count()) {
      R.comboSheetOpenedOnAdd = true;
      await page.click('#sheetClose');
      await page.waitForTimeout(300);
    }
  }
  R.cartBefore = await page.locator('#cartCount').innerText();
  await page.click('.rail [data-nav="stores"]');
  await page.waitForTimeout(400);
  await page.locator('.store-card').nth(2).click();
  await page.waitForTimeout(2000);
  R.cartAfterStoreSwitch = await page.locator('#cartCount').innerText();
  R.cartResetOnSwitch = R.cartAfterStoreSwitch === '0';

  /* ---- no leftover store-list section forcing a jump ---- */
  R.legacyStoreList = await page.locator('.store-list').count();

  /* ---- mobile ---- */
  const m = await ctx.newPage();
  await m.setViewportSize({ width: 390, height: 844 });
  m.on('pageerror', (e) => errors.push('mobile pageerror: ' + e.message));
  m.on('console', (m2) => { if (m2.type() === 'error') errors.push('mobile console: ' + m2.text()); });
  await m.goto(BASE, { waitUntil: 'load' });
  await m.waitForTimeout(2500);
  await m.click('#addrChip');
  await m.waitForTimeout(500);
  R.mobilePickerOpen = await m.locator('#pickPanel.on').count();
  R.mobilePickerRows = await m.locator('.pick-row').count();
  R.mobilePickerBottomSheet = await m.evaluate(() => {
    const p = document.querySelector('#pickPanel').getBoundingClientRect();
    return Math.abs(p.bottom - window.innerHeight) < 4;
  });
  await m.screenshot({ path: '/tmp/mcd-n5-mobile-picker.png' });
  await m.locator('.pick-row').nth(1).click();
  await m.waitForTimeout(1500);
  R.mobileSwitched = await m.locator('#addrName').innerText();
  R.mobileBottomNavItems = await m.locator('.bottom button').count();
  R.mobileOverflow = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  await m.screenshot({ path: '/tmp/mcd-n6-mobile-home.png' });

  R.errors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})();
