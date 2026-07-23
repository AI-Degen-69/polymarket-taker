"""Taker kanban view. Same pipeline shape as the maker dashboard, different
stages and different metrics -- because a taker's job is a different job.

    maker asks:  did I get filled, and did I get picked off?
    taker asks:  was the gate right, and did I beat the breakeven I paid for?

Lanes follow what actually happens to a taker order:
    WATCH -> GATE -> FIRE -> HOLD -> SETTLE

Served by the existing taker dashboard app at /kanban, so the React UI and this
view coexist and can be compared.
"""
from __future__ import annotations

PAGE = r"""
<style>
 :root{--bg:#0a0c0d;--pan:#121618;--pan2:#161b1e;--bd:#232a2e;--tx:#d6dbd8;
       --dim:#79847f;--am:#eda92c;--gn:#46c46a;--rd:#e2564f;--bl:#5b9bd5;
       --pu:#9b7fd4}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--tx);
      font:14.5px ui-monospace,SFMono-Regular,Menlo,monospace}
 .bar{display:flex;align-items:center;gap:12px;padding:7px 14px;
      border-bottom:1px solid var(--bd);background:var(--pan)}
 .bar b{color:var(--am);letter-spacing:1.4px;font-size:16px}
 .chip{border:1px solid var(--gn);color:var(--gn);padding:2px 9px;font-size:11.5px;letter-spacing:1.4px}
 .samp{display:flex;align-items:center;gap:18px;padding:7px 14px;
       border-bottom:1px solid var(--bd);background:#0d1113;flex-wrap:wrap}
 .lab{color:var(--dim);font-size:11.5px;letter-spacing:1.2px}
 .tgt{display:inline-flex;align-items:center;gap:7px;font-size:12.5px}
 .track{display:inline-block;width:150px;height:11px;background:#1b2124;
        border:1px solid var(--bd);overflow:hidden;vertical-align:middle}
 .fillbar{display:block;height:100%;background:var(--bl);transition:width .6s ease}
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
       gap:7px;padding:9px 12px;border-bottom:1px solid var(--bd)}
 .k{border:1px solid var(--bd);background:var(--pan);padding:6px 9px}
 .k .n{color:var(--dim);font-size:10.5px;letter-spacing:.8px}
 .k .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
 .k .s{color:var(--dim);font-size:11px}
 .kan{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;padding:10px 12px;align-items:start}
 @media(max-width:1250px){.kan{grid-template-columns:repeat(2,1fr)}}
 .lane{border:1px solid var(--bd);background:var(--pan);display:flex;flex-direction:column;min-height:150px}
 .lane h3{margin:0;padding:8px 11px;font-size:11.5px;letter-spacing:1.3px;
          border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;
          align-items:center;font-weight:700}
 .lane .body{padding:6px;display:flex;flex-direction:column;gap:5px;max-height:520px;overflow-y:auto}
 .cnt{background:#1c2225;color:var(--dim);padding:1px 7px;border-radius:8px;font-size:10.5px}
 .l1 h3{color:var(--dim)} .l1{border-top:2px solid #3a4145}
 .l2 h3{color:var(--pu)}  .l2{border-top:2px solid var(--pu)}
 .l3 h3{color:var(--bl)}  .l3{border-top:2px solid var(--bl)}
 .l4 h3{color:var(--am)}  .l4{border-top:2px solid var(--am)}
 .l5 h3{color:var(--gn)}  .l5{border-top:2px solid var(--gn)}
 .card{background:var(--pan2);border:1px solid var(--bd);border-left:2px solid var(--bd);
       padding:7px 9px;font-size:12.5px;line-height:1.55}
 .card .top{display:flex;justify-content:space-between;gap:6px;align-items:baseline}
 .card .sub{color:var(--dim);font-size:11px}
 .card.up{border-left-color:var(--gn)} .card.dn{border-left-color:var(--rd)}
 .card.win{border-left-color:var(--gn)} .card.loss{border-left-color:var(--rd)}
 .card.skip{opacity:.62}
 .g{color:var(--gn)}.r_{color:var(--rd)}.a{color:var(--am)}.d{color:var(--dim)}.bl{color:var(--bl)}.pu{color:var(--pu)}
 @keyframes flowin{0%{opacity:0;transform:translateX(-26px) scale(.97)}60%{opacity:1}100%{opacity:1;transform:none}}
 .enter{animation:flowin .55s cubic-bezier(.22,.9,.3,1)}
 @media (prefers-reduced-motion: reduce){.enter{animation:none}}
 .note{color:var(--dim);font-size:11px;padding:6px 10px;line-height:1.5;border-top:1px solid var(--bd)}
 .livebar{display:flex;gap:18px;align-items:center;padding:7px 14px;
          border-bottom:1px solid var(--bd);background:var(--pan);flex-wrap:wrap}
 table{width:100%;border-collapse:collapse;font-size:11.5px}
 th{color:var(--dim);text-align:right;font-weight:400;padding:3px 4px;border-bottom:1px solid var(--bd)}
 th:first-child,td:first-child{text-align:left}
 td{padding:3px 4px;text-align:right;font-variant-numeric:tabular-nums}
</style>

<div class="bar">
  <b>TAKER_BOT</b><span class="d">·</span><span>BTC 5MIN</span>
  <span class="chip" id="mode">PAPER</span>
  <span id="live" class="d"></span>
  <span class="nav" style="display:flex;gap:6px;margin-left:10px">
    <a href="/" style="color:var(--dim);text-decoration:none;font-size:12px;padding:3px 10px;border:1px solid var(--bd);border-radius:4px">LIVE</a>
    <a href="/kanban" style="color:var(--bg);background:var(--am);border:1px solid var(--am);text-decoration:none;font-size:12px;padding:3px 10px;border-radius:4px;font-weight:700">KANBAN</a>
    <a href="/collector" style="color:var(--dim);text-decoration:none;font-size:12px;padding:3px 10px;border:1px solid var(--bd);border-radius:4px">COLLECTOR</a>
  </span>
  <span style="flex:1"></span>
  <span id="clock" class="d"></span>
</div>
<div class="samp" id="samp"></div>
<div class="livebar" id="livebar"></div>
<div class="kpis" id="kpis"></div>
<div class="kan" id="kan"></div>
<div class="note" id="foot" style="display:flex;gap:18px;flex-wrap:wrap"></div>

<script>
const $=(x)=>document.getElementById(x);
const usd=(v,d=2)=>v==null?'—':(v<0?'-':'')+'$'+Math.abs(v).toFixed(d);
const pct=(v,d=1)=>v==null?'—':(v*100).toFixed(d)+'%';
const num=(v,d=0)=>v==null?'—':Number(v).toFixed(d);
const cls=(v)=>v==null?'':(v>=0?'g':'r_');
const hhmm=(t)=>t?new Date(t*1000).toLocaleTimeString():'—';
const seen={};

(async function(){
  let meta={};
  try{ meta=await (await fetch('/api/meta',{cache:'no-store'})).json(); }catch(e){}
  const f=document.getElementById('foot');
  if(f) f.innerHTML =
      (meta.deploy_sha?`<span>sha: ${meta.deploy_sha}</span>`:'')
    + (meta.railway_deploy_id?`<span>railway: ${meta.railway_deploy_id.slice(0,8)}</span>`:'');
})();

function lane(id,title,c,cards,note){
  const s=seen[id]=seen[id]||new Set();
  const html=cards.map(x=>{const n=!s.has(x.key);s.add(x.key);
    return `<div class="card ${x.cls||''} ${n?'enter':''}">${x.html}</div>`}).join('');
  return `<div class="lane ${c}"><h3><span>${title}</span><span class="cnt">${cards.length}</span></h3>
    <div class="body">${html||'<div class="d" style="padding:6px">—</div>'}</div>
    ${note?`<div class="note">${note}</div>`:''}</div>`;
}

/* Sample-size targets for a TAKER are a win-rate question, not a mean-PnL one:
   how many settled markets until the Wilson CI on win rate clears the
   payoff-implied breakeven? Computed client-side from the KPI payload. */
function sampleBar(k){
  const n=k.markets||0, w=k.win_rate, be=k.breakeven_wr;
  if(!n||w==null||be==null)
    return `<span class="lab">SAMPLE</span><span>${n} settled markets</span>`;
  let h=`<span class="lab">SAMPLE SIZE</span><span><b class="bl">${n}</b> settled</span>
    <span class="d">win ${pct(w)} vs breakeven ${pct(be)}</span>`;
  const p=w, gap=Math.abs(p-be);
  for(const [lvl,z] of [['90%',1.645],['95%',1.960],['99%',2.576]]){
    let need=null;
    if(gap>1e-6) need=Math.ceil(z*z*p*(1-p)/(gap*gap));
    const prog=need?Math.min(100,100*n/need):0, done=need&&n>=need;
    h+=`<span class="tgt"><span class="d">${lvl}</span>
      <span class="track"><span class="fillbar" style="width:${prog.toFixed(1)}%;
        background:${done?'var(--gn)':'var(--bl)'}"></span></span>
      <span class="${done?'g':''}">${done?'REACHED':n+'/'+(need==null?'∞':need)}</span></span>`;
  }
  return h;
}

async function tick(){
  let s; try{ s=await (await fetch('/api/state',{cache:'no-store'})).json(); }catch(e){ return; }
  $('clock').textContent=new Date().toLocaleTimeString();
  const k=s.kpi||{}, cfg=s.config||{}, m=s.market, sp=s.spot||{}, acct=s.account||{};
  $('mode').textContent=(s.bot_mode||'stopped').toUpperCase();
  $('live').textContent=s.bot_running?'● bot running':'● bot stopped';
  $('live').className=s.bot_running?'g':'r_';
  $('samp').innerHTML=sampleBar(k);

  /* live market + the gate, which is the taker's whole thesis */
  const bu=s.book_up||{},bd=s.book_down||{};
  const gateColor=sp.gate==='OPEN'?'g':(sp.gate==='FLAT'?'a':'r_');
  $('livebar').innerHTML = m ? `
    <span class="lab">LIVE</span>
    <a href="https://polymarket.com/event/${m.market_slug}" target="_blank"
       style="color:var(--am);text-decoration:none">${m.market_slug} ↗</a>
    <span class="a" style="font-size:20px;font-weight:700">${num(Math.max(0,m.t_remaining))}s</span>
    <span class="d">UP</span><span class="g">${bu.best_bid==null?'—':bu.best_bid.toFixed(2)}</span>
      <span class="d">/</span><span class="a">${bu.best_ask==null?'—':bu.best_ask.toFixed(2)}</span>
    <span class="d">DOWN</span><span class="g">${bd.best_bid==null?'—':bd.best_bid.toFixed(2)}</span>
      <span class="d">/</span><span class="a">${bd.best_ask==null?'—':bd.best_ask.toFixed(2)}</span>
    <span style="flex:1"></span>
    <span class="lab">SPOT GATE</span>
    <span class="${gateColor}" style="font-weight:700">${sp.gate||'—'}</span>
    <span class="d">${sp.offset_bps==null?'':num(sp.offset_bps,2)+' bps vs open'}</span>
    <span class="d">favours</span><span class="${sp.favored==='UP'?'g':'r_'}">${sp.favored||'—'}</span>`
    : '<span class="d">awaiting next 5-min window…</span>';

  const K=(n,v,sub,c)=>`<div class="k"><div class="n">${n}</div>
      <div class="v ${c||''}">${v}</div><div class="s">${sub||''}</div></div>`;
  $('kpis').innerHTML =
      K('EQUITY',usd(acct.equity),'from '+usd(acct.bankroll),cls(acct.total_pnl))
    + K('NET P&L',usd(k.total_pnl),pct(k.roi_on_cost,2)+' of risk',cls(k.total_pnl))
    + K('WIN RATE',pct(k.win_rate),'needs '+pct(k.breakeven_wr),
        (k.win_rate??0)>=(k.breakeven_wr??1)?'g':'r_')
    + K('EXPECTANCY',usd(k.expectancy),'per market',cls(k.expectancy))
    + K('AVG WIN',usd(k.avg_win),'vs loss '+usd(k.avg_loss),'g')
    + K('PROFIT FACTOR',num(k.profit_factor,2),'gross W ÷ L',
        (k.profit_factor??0)>=1?'g':'r_')
    + K('MAX DRAWDOWN',usd(-(k.max_drawdown||0)),num(k.max_drawdown_pct,1)+'%','r_')
    + K('FEES PAID',usd(k.total_fees),'takers always pay','r_')
    + K('VERDICT',k.verdict||'—','markets '+num(k.markets),
        k.verdict==='WINNING'?'g':(k.verdict==='LOSING'?'r_':'a'));

  /* ---- lanes: WATCH -> GATE -> FIRE -> HOLD -> SETTLE ---- */
  const dec=(s.decisions||[]);
  const watch=dec.filter(d=>/SKIP_TIME|SKIP_PRICE|SKIP_SIZE|SKIP_AMBIG/.test(d.action)).slice(0,12)
    .map(d=>({key:'w'+d.id,cls:'skip',
      html:`<div class="top"><span class="d">${d.action}${d.count>1?' ×'+d.count:''}</span>
        <span class="d">${hhmm(d.ts)}</span></div>
        <div class="sub">${(d.reason||'').slice(0,44)}</div>`}));

  const gate=dec.filter(d=>/SPOT/.test(d.action)).slice(0,12)
    .map(d=>({key:'g'+d.id,cls:'skip',
      html:`<div class="top"><span class="pu">${d.action.replace('SKIP_','')}${d.count>1?' ×'+d.count:''}</span>
        <span class="d">${hhmm(d.ts)}</span></div>
        <div class="sub">${(d.reason||'').slice(0,44)}</div>`}));

  const fire=(s.orders||[]).slice(0,12).map(o=>({
    key:'o'+o.id, cls:(o.side==='UP'?'up':'dn'),
    html:`<div class="top"><span class="${o.side==='UP'?'g':'r_'}">${o.side} @ ${(o.price||0).toFixed(2)}</span>
      <span>${num(o.size)} sh</span></div>
      <div class="sub">cost ${usd((o.size||0)*(o.price||0))} · ${hhmm(o.ts)}</div>`}));

  const hold=(s.sim_positions||[]).map(p=>({
    key:'h'+p.market_slug+p.side, cls:(p.side==='UP'?'up':'dn'),
    html:`<div class="top"><span class="${p.side==='UP'?'g':'r_'}">${p.side}</span>
      <span>${num(p.shares)} sh @ ${num(p.avg_price,3)}</span></div>
      <div class="sub">cost ${usd(p.cost)} · ${p.fills} fills</div>
      <div class="sub">${p.pending?'<span class="a">awaiting resolution</span>':
        'mark '+num(p.mark_price,3)+' · unreal <span class="'+(p.unrealized>=0?'g':'r_')+'">'+usd(p.unrealized)+'</span>'}</div>`}));

  const settle=(s.settlements||[]).slice(0,12).map(x=>({
    key:'s'+x.market_slug, cls:(x.won?'win':'loss'),
    html:`<div class="top"><span class="d">…${(x.market_slug||'').slice(-8)}</span>
      <span class="${x.pnl>=0?'g':'r_'}" style="font-weight:700">${x.pnl>=0?'+':''}${usd(x.pnl)}</span></div>
      <div class="sub">${x.side} · ${x.fills} fills · ${x.won?'<span class="g">WON</span>':'<span class="r_">LOST</span>'}</div>
      <div class="sub">cost ${usd(x.cost,0)} → paid ${usd(x.payout,0)}</div>`}));

  $('kan').innerHTML =
      lane('t1','① WATCH','l1',watch,'window/price/size filters — before the gate')
    + lane('t2','② SPOT GATE','l2',gate,'BTC vs window open — the thesis')
    + lane('t3','③ FIRE','l3',fire,'FOK taken at the ask')
    + lane('t4','④ HOLD','l4',hold,'held to resolution — never sells')
    + lane('t5','⑤ SETTLE','l5',settle,'$1.00 if right, $0.00 if wrong');
}
tick(); setInterval(tick,2000);
</script>
"""
