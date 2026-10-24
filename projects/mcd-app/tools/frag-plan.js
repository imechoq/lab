/* ---------------- 配餐方案：按人数 + 预算生成可落地清单 ----------------
   只用当前门店 / 当前渠道的真实菜单数据。套餐类商品需要逐项选规格，
   无法一键加购；部分商品不参与团餐优惠。这两类统一排除并在界面说明。 */

function planRole(item, cat){
  const c = String(cat || '');
  if (/饮品|咖啡/.test(c)) return 'drink';
  if (/小食|甜品/.test(c)) return 'snack';
  if (/主食/.test(c)) return 'main';
  const n = item.name || '';
  if (/可乐|雪碧|芬达|红茶|绿茶|奶茶|咖啡|果汁|柠檬|气泡/.test(n)) return 'drink';
  if (/堡|卷|饭|面|三明治|巨无霸|双层|麦香鸡|肉霸堡|鳕鱼/.test(n)) return 'main';
  if (/薯|鸡|翅|派|麦旋风|沙拉|苹果|玉米|奶昔|蛋挞|脆卜卜|粒/.test(n)) return 'snack';
  return null;
}
function planEligible(item){
  if (!item || !item.price) return false;
  if (GOODS.test(item.name)) return false;          // 周边不是餐品
  if (blocked(item)) return false;                   // 忌口直接排除
  if (item.promoExcluded) return false;              // 不参与优惠，不进方案
  if ((item.combo && item.combo.isCombo) || COMBO_HINT.test(item.name || '')) return false;
  return true;
}
function planPools(){
  const pools = { main: [], snack: [], drink: [] };
  allItems().forEach((i) => {
    if (!planEligible(i)) return;
    const r = planRole(i, i.cat);
    if (r) pools[r].push(i);
  });
  Object.keys(pools).forEach((k) => pools[k].sort((a, b) => a.price - b.price));
  return pools;
}
function planBest(pool){ return pool.slice().sort((a, b) => score(b) - score(a))[0] || null; }
function planQuote(lines){
  const sub = lines.reduce((s, l) => s + l.i.price * l.n, 0);
  const gd = groupDiscount(sub);
  const pay = Math.round(sub * gd.rate);
  const budget = budgetTotal();
  return { sub, pay, gd, budget, diff: budget - pay, over: pay > budget };
}
/* 最省的一人份：最便宜主食 + 最便宜饮品 */
function planCheapestHead(P){
  const m = P.main[0], d = P.drink[0];
  if (!m || !d) return null;
  return { main: m, drink: d, per: m.price + d.price };
}
function planSteps(t){
  const P = planPools();
  const mainL = t.lines.find((l) => planRole(l.i, l.i.cat) === 'main');
  const drinkL = t.lines.find((l) => planRole(l.i, l.i.cat) === 'drink');
  const snackL = t.lines.find((l) => planRole(l.i, l.i.cat) === 'snack');
  const seq = [t.lines.slice()];
  if (snackL && snackL.n > 1) {
    seq.push(t.lines.map((l) => l === snackL ? Object.assign({}, l, { n: Math.max(1, Math.floor(l.n / 2)) }) : l));
  }
  if (snackL) seq.push(t.lines.filter((l) => l !== snackL));
  if (mainL) {
    seq.push([
      { i: P.main[0], n: Math.min(mainL.n, state.people) },
      { i: P.drink[0], n: Math.min(drinkL ? drinkL.n : state.people, state.people) }
    ].filter((l) => l && l.i));
  }
  return seq;
}
function buildPlans(){
  const N = state.people;
  const P = planPools();
  if (!P.main.length || !P.drink.length) return { plans: [], conflict: null, cheapest: null };
  const mainCheap = P.main[0], mainBest = planBest(P.main);
  const drinkCheap = P.drink[0], drinkBest = planBest(P.drink);
  const snackBest = planBest(P.snack);
  const share = Math.max(1, Math.ceil(N / 4));
  const cheapest = planCheapestHead(P);
  const budget = budgetTotal();
  /* 冲突说人话：预算连最省配置都罩不住全部人数时，直接讲清差多少 */
  let conflict = null;
  if (cheapest && cheapest.per * N > budget) {
    const covered = Math.max(1, Math.floor(budget / cheapest.per));
    conflict = {
      perHead: cheapest.per,
      covered,
      gap: N - covered,
      need: cheapest.per * N - budget
    };
  }
  const tpl = [
    { id: 'A', title: '省预算 · 每人一份', tag: '最省',
      note: '主食与饮品都取当前菜单里单价最低的单品。',
      lines: [{ i: mainCheap, n: N }, { i: drinkCheap, n: N }] },
    { id: 'B', title: '均衡 · 主食 + 饮品 + 分享小食', tag: '推荐',
      note: '主食饮品按你的偏好挑，小食按人数四舍五入分享。',
      lines: [{ i: mainBest, n: N }, { i: drinkBest, n: N }].concat(snackBest ? [{ i: snackBest, n: share }] : []) },
    { id: 'C', title: '聚餐 · 小食加倍、主食减半', tag: '分享',
      note: '主食吃半份，小食与饮品顶饱，适合边聊边吃。',
      lines: [{ i: mainBest, n: Math.max(1, Math.ceil(N / 2)) }]
        .concat(snackBest ? [{ i: snackBest, n: Math.max(2, share * 2) }] : [])
        .concat([{ i: drinkCheap, n: N }]) }
  ];
  const plans = tpl.map((t) => {
    const steps = planSteps(t);
    let chosen = null;
    for (let i = 0; i < steps.length; i++) {
      const q = planQuote(steps[i]);
      if (!q.over) { chosen = { lines: steps[i], q, level: i }; break; }
    }
    if (!chosen) {
      const last = steps[steps.length - 1];
      chosen = { lines: last, q: planQuote(last), level: steps.length - 1, over: true };
    }
    return Object.assign({}, t, { lines: chosen.lines, quote: chosen.q, degraded: chosen.level > 0, over: !!chosen.over });
  });
  /* 方案 D：预算内最多能覆盖多少人，回答"预算和人数打架" */
  if (cheapest) {
    const k = Math.max(1, Math.min(N, Math.floor(budget / cheapest.per)));
    const lines = [{ i: cheapest.main, n: k }, { i: cheapest.drink, n: k }];
    plans.push({
      id: 'D', title: '预算内最多覆盖', tag: '兜底',
      note: '按最省组合算，你的预算最多能配 ' + k + ' 人份。',
      lines, quote: planQuote(lines), degraded: false, over: k < N
    });
  }
  return { plans, conflict, cheapest };
}
function planCard(p){
  const q = p.quote;
  const diffText = q.over ? '超预算 ' + yuan(-q.diff) : '预算内，余 ' + yuan(q.diff);
  const minNote = q.gd.note ? '<div class="hint" style="margin-top:2px">' + esc(q.gd.note) + '</div>' : '';
  return `<article class="plan ${q.over ? 'over' : ''}" data-plan="${esc(p.id)}">
    <div class="hd"><b>方案 ${esc(p.id)} · ${esc(p.title)}</b><span class="pill ${p.id === 'B' ? 'red' : ''}">${esc(p.tag)}</span></div>
    <p class="note">${esc(p.note)}${p.degraded && !p.over ? ' 已为预算自动降级配置。' : ''}</p>
    <ul>
      ${p.lines.map((l) => `<li><span class="n">${esc(l.i.name)}</span><span class="q">×${l.n}</span><span class="p num">${yuan(l.i.price * l.n)}</span></li>`).join('')}
    </ul>
    <div class="foot">
      <div class="sum"><span>合计${groupMode() ? '（含团餐折扣估算）' : ''}</span><b class="num ${q.over ? 'over' : ''}">${yuan(q.pay)}</b></div>
      <div class="per"><span>人均 ${yuan(Math.round(q.pay / Math.max(1, state.people)))} · 预算 ${yuan(q.budget)}</span><b class="${q.over ? 'bad' : 'ok'}">${diffText}</b></div>
      ${minNote}
    </div>
    <button class="btn" data-planadd="${esc(p.id)}">按此方案加购</button>
  </article>`;
}
/* 只刷方案区，输入框不重建，输数字时不丢焦点 */
function planBodyHTML(){
  const r = buildPlans();
  const N = state.people;
  const budget = budgetTotal();
  const per = Math.round(budget / Math.max(1, N));
  const conflict = r.conflict;
  return `<div class="plan-sum">${N} 人 · ${state.budgetMode === 'per' ? '人均 ' + yuan(state.budgetPer) : '总额 ' + yuan(state.budgetTotal)} · 合计预算 ${yuan(budget)}（人均 ${yuan(per)}）</div>
    ${conflict ? `<div class="plan-warn"><b>预算和人数打架了</b>按最省的一人份（${esc(r.cheapest.main.name)} + ${esc(r.cheapest.drink.name)}，人均 ${yuan(conflict.perHead)}）算，${N} 人需要 ${yuan(conflict.perHead * N)}，比预算多 ${yuan(conflict.need)}。现在预算最多配 <b>${conflict.covered} 人</b>，差 ${conflict.gap} 人；要么把人均预算提到 ${yuan(conflict.perHead)} 以上，要么减到 ${conflict.covered} 人。</div>` : ''}
    ${r.plans.length ? `<div class="plan-grid">${r.plans.map(planCard).join('')}</div>`
      : `<div class="empty"><b>当前菜单算不出配餐方案</b>可用商品不足或全为套餐类，换个门店或渠道再试。</div>`}
    <p class="plan-note">口径说明：金额为菜单价估算，${groupMode() ? '团餐折扣按当前门店促销档位预（满 300 / 500 / 1000 / 2000 分档），' : ''}最终价格以结算接口为准。已排除需逐项选规格的套餐商品、不参与优惠的商品，以及与忌口冲突的商品。</p>`;
}
function refreshPlanBody(){
  const box = byId('planBody');
  if (box) box.innerHTML = planBodyHTML();
}
function viewPlans(){
  return `<section class="panel sec">
    <div class="sec-head"><div><h2>按人数和预算配餐</h2><p>方案全部来自当前门店菜单，金额为菜单价估算</p></div><button class="link" data-nav="home">返回首页</button></div>
    <div class="plan-meta">
      <div class="stepper"><button data-people="-1" aria-label="减少人数">−</button><input id="planPeople" class="num" type="number" min="1" max="99" value="${state.people}" aria-label="用餐人数"><button data-people="1" aria-label="增加人数">+</button></div>
      <div class="money" style="max-width:220px"><span class="cur">¥</span><input id="planBudget" class="num" type="number" min="0" step="1" value="${((state.budgetMode === 'per' ? state.budgetPer : state.budgetTotal) / 100).toFixed(0)}" aria-label="预算金额"></div>
      <span class="seg"><button data-bmode="per" aria-pressed="${state.budgetMode === 'per'}">人均</button><button data-bmode="total" aria-pressed="${state.budgetMode === 'total'}">总额</button></span>
      <span class="hint">${state.budgetMode === 'per' ? '当前按人均填写' : '当前按总额填写'}</span>
    </div>
    <div id="planBody">${planBodyHTML()}</div>
  </section>`;
}
function applyPlan(id){
  const r = buildPlans();
  const p = r.plans.find((x) => x.id === id);
  if (!p) return;
  p.lines.forEach((l) => { if (l.i) addToCart(l.i.code, [], '', l.n); });
  save(); syncChrome(); refreshPlanBody();
  toast('已按方案 ' + id + ' 加入 ' + p.lines.reduce((s, l) => s + l.n, 0) + ' 件');
}
