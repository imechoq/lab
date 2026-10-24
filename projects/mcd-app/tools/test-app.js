const path = require('path');

// Resolve playwright from the local install if present, else fall back to the
// global one. Set PLAYWRIGHT_MODULE to override explicitly.
function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_MODULE,
    'playwright',
    path.join(process.env.HOME || '', '.npm-global/lib/node_modules/playwright'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (e) {
      /* try next */
    }
  }
  throw new Error(
    'playwright not found. Install it with:  npm i -D playwright\n' +
    'or set PLAYWRIGHT_MODULE=/path/to/playwright');
}

const { chromium } = loadPlaywright();

const APP = path.resolve(__dirname, '..', 'app', 'index.html');
const FILE = 'file://' + APP;

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) {} });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(FILE, { waitUntil: 'load' });
  await page.waitForTimeout(700);
  const R = {};

  R.store = await page.locator('.hero h1').innerText();
  R.recCards = await page.locator('.rec').count();
  await page.waitForTimeout(2500);
  R.imagesFirst = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('.rec img')].slice(0, 4);
    return { total: imgs.length, loaded: imgs.filter((i) => i.complete && i.naturalWidth > 0).length };
  });
  await page.evaluate(() => { const h = document.querySelector('.hscroll'); if (h) h.scrollLeft = h.scrollWidth; });
  await page.waitForTimeout(1800);
  R.images = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('.rec img')];
    return { total: imgs.length, loaded: imgs.filter((i) => i.complete && i.naturalWidth > 0).length };
  });
  await page.screenshot({ path: '/tmp/mcd-01-home.png' });

  /* ---- budget: 人均 68 × 2 人 ---- */
  await page.fill('#budgetInput', '68');
  await page.waitForTimeout(120);
  R.budgetAfterFill = await page.locator('#budgetSummary').innerText();
  await page.click('[data-people="1"]');
  await page.waitForTimeout(180);
  R.people = await page.inputValue('#peopleInput');
  R.budgetAfterPeople = await page.locator('#budgetSummary').innerText();
  R.railBudget = await page.locator('#railBudget').innerText();

  /* ---- 总额口径 ---- */
  await page.click('[data-bmode="total"]');
  await page.waitForTimeout(150);
  await page.fill('#budgetInput', '400');
  await page.waitForTimeout(150);
  R.budgetTotalMode = await page.locator('#budgetSummary').innerText();
  R.railBudgetTotalMode = await page.locator('#railBudget').innerText();
  await page.click('[data-bmode="per"]');
  await page.waitForTimeout(150);
  await page.fill('#budgetInput', '68');
  await page.waitForTimeout(150);
  R.budgetBackPer = await page.locator('#budgetSummary').innerText();

  /* ---- 菜单 ---- */
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(350);
  R.menuCards = await page.locator('.pc').count();
  R.specButtons = await page.locator('.pc .add.spec').count();
  R.plainButtons = await page.locator('.pc .add:not(.spec)').count();
  R.cats = await page.locator('.cats button').count();
  await page.screenshot({ path: '/tmp/mcd-02-menu.png' });

  /* ---- 搜索 ---- */
  await page.fill('#searchInput', '鸡腿堡');
  await page.waitForTimeout(300);
  R.searchCards = await page.locator('.pc').count();
  await page.fill('#searchInput', '');
  await page.waitForTimeout(250);

  /* ---- 单品详情 + 定制 ---- */
  await page.locator('.pc .nm').filter({ hasText: '巨无霸' }).first().click();
  await page.waitForTimeout(320);
  R.singleSheet = { title: await page.locator('#sheetTitle').innerText(), opts: await page.locator('.opt').count(), rounds: await page.locator('.round').count() };
  await page.locator('.opt').first().click();
  await page.waitForTimeout(120);
  R.singleFoot = await page.locator('.sheet-foot .sum').innerText();
  await page.click('#sheetAdd');
  await page.waitForTimeout(320);
  R.cartAfterSingle = await page.locator('.cart-line').count();
  R.billSingle = await page.locator('.bill .total').innerText();
  await page.screenshot({ path: '/tmp/mcd-03-cart.png' });

  /* ---- 套餐必须选规格 ---- */
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(300);
  await page.locator('.pc .add.spec').first().click();
  await page.waitForTimeout(320);
  R.comboSheet = {
    title: await page.locator('#sheetTitle').innerText(),
    rounds: await page.locator('.round').count(),
    opts: await page.locator('.opt').count(),
    opened: await page.locator('#sheet.on').count()
  };
  await page.screenshot({ path: '/tmp/mcd-04-combo.png' });
  R.comboFootBefore = await page.locator('.sheet-foot .sum').innerText();
  const altOpt = page.locator('.opt[aria-pressed="false"]').first();
  if (await altOpt.count()) await altOpt.click();
  await page.waitForTimeout(150);
  R.comboFootAfter = await page.locator('.sheet-foot .sum').innerText();
  await page.click('#sheetAdd');
  await page.waitForTimeout(320);
  R.cartLines = await page.locator('.cart-line').count();
  R.cartMods = await page.locator('.cart-line .sub').first().innerText();
  R.billAfterCombo = await page.locator('.bill .total').innerText();

  /* ---- 预览 ---- */
  await page.click('.rail [data-nav="preview"]');
  await page.waitForTimeout(300);
  R.previewHeading = await page.locator('.preview-hd h2').innerText();
  R.confirmDisabled = await page.locator('.confirmbar .btn').first().isDisabled();
  await page.screenshot({ path: '/tmp/mcd-05-preview.png' });

  /* ---- 团餐：加到满 300 看折扣 ---- */
  await page.click('.rail [data-nav="home"]');
  await page.waitForTimeout(200);
  await page.click('[data-channel="6"]');
  await page.waitForTimeout(300);
  R.groupStore = await page.locator('.hero h1').innerText();
  await page.click('.rail [data-nav="menu"]');
  await page.waitForTimeout(300);
  R.groupCards = await page.locator('.pc').count();
  R.groupCats = await page.locator('.cats button').count();
  const addBtn = page.locator('.pc .add:not(.spec)').first();
  const code = await addBtn.getAttribute('data-add');
  await addBtn.click();
  await page.waitForTimeout(250);
  await page.click('.rail [data-nav="preview"]');
  await page.waitForTimeout(250);
  R.groupWarnBefore = await page.locator('.warnbox').count();
  R.groupSubtotal = await page.locator('.bill div').first().innerText();
  let clicks = 0;
  for (let i = 0; i < 24; i++) {
    const inc = page.locator(`[data-cart-inc="0"]`);
    if (!(await inc.count())) break;
    await inc.click();
    clicks++;
    await page.waitForTimeout(45);
    if (!(await page.locator('.warnbox').count())) break;
  }
  R.groupIncClicks = clicks;
  R.groupWarnAfter = await page.locator('.warnbox').count();
  R.groupBill = await page.locator('.bill').innerText();
  await page.screenshot({ path: '/tmp/mcd-06-group.png' });

  /* ---- 换门店清空 ---- */
  await page.click('.rail [data-nav="home"]');
  await page.waitForTimeout(200);
  await page.locator('.store-row').nth(1).click();
  await page.waitForTimeout(300);
  R.storeAfterSwitch = await page.locator('.hero h1').innerText();
  R.cartAfterSwitch = await page.locator('#cartCount').innerText();

  /* ---- 偏好影响推荐 ---- */
  await page.click('.rail [data-nav="prefs"]');
  await page.waitForTimeout(250);
  R.prefToggles = await page.locator('.toggle').count();
  await page.locator('[data-pref="spice"]').click();
  await page.waitForTimeout(200);
  await page.fill('#prefPer', '150');
  await page.waitForTimeout(150);
  R.prefSync1 = { per: await page.inputValue('#prefPer'), total: await page.inputValue('#prefTotal') };
  await page.fill('#prefTotal', '900');
  await page.waitForTimeout(150);
  R.prefSync2 = { per: await page.inputValue('#prefPer'), total: await page.inputValue('#prefTotal') };
  await page.screenshot({ path: '/tmp/mcd-07-prefs.png' });
  await page.click('.rail [data-nav="home"]');
  await page.waitForTimeout(300);
  R.recAfterPref = await page.locator('.rec .nm').first().innerText();
  R.prefPills = await page.locator('.hero-note .pill').count();

  /* ---- 移动端 ---- */
  const m = await ctx.newPage();
  await m.setViewportSize({ width: 390, height: 844 });
  m.on('pageerror', (e) => errors.push('mobile pageerror: ' + e.message));
  await m.goto(FILE, { waitUntil: 'load' });
  await m.waitForTimeout(800);
  await m.screenshot({ path: '/tmp/mcd-08-mobile-home.png', fullPage: false });
  await m.click('.bottom [data-nav="menu"]');
  await m.waitForTimeout(400);
  await m.screenshot({ path: '/tmp/mcd-09-mobile-menu.png' });
  R.mobileOverflow = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  R.mobileBottomNav = await m.locator('.bottom').isVisible();
  R.mobileCards = await m.locator('.pc').count();
  await m.locator('.pc .nm').first().click();
  await m.waitForTimeout(320);
  await m.screenshot({ path: '/tmp/mcd-10-mobile-sheet.png' });
  R.mobileSheetOpen = await m.locator('#sheet.on').count();

  R.errors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})();
