"""Gate-collector dashboard: forward book-favoured-side vs spot-gate audit.

Read-only view of COLLECTOR_DB (written by strategy.collect_gate). Shows the
live ungated-vs-gated accuracy building up window by window, the raw snapshot
table, and a kanban-style flow of where each recent window sits in its life
cycle:

    WATCH  -> window discovered, not yet at t_rem=120s
    GATE   -> spot offset measured (the gate input)
    FIRE   -> book + spot snapshot taken at t_rem=120s
    HOLD   -> window closed, awaiting gamma resolution
    SETTLE -> winner known, hit_book / hit_gate computed

Server-rendered so it needs no rebuild of the React SPA; it polls
/api/collector-state every 3s.
"""
from __future__ import annotations

import os

_EXPLAINER = r"""
<div class="explain">
  <div class="ex-head">WHAT IS THIS PAGE · GATE_COLLECTOR</div>
  <div class="ex-grid">
    <div class="ex-card">
      <div class="ex-h">🎯 OBJECTIVE</div>
      <div class="ex-b">Settle the open question from the gate retest: does the
      <b>book-favoured-side + Binance spot-gate</b> combo actually predict the
      winner ~96% of the time <b>forward</b>, on live data? The backtest claimed
      81&rarr;96 but could only measure the spot signal (77&rarr;93); the CLOB
      book is live-only and was never tested. This page collects it window by
      window until the sample is big enough to call.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">👁 WHAT WE WATCH</div>
      <div class="ex-b">Every 5-min BTC window: at <b>t-120s</b> (the moment the
      bot may enter) we snapshot the <b>CLOB order book</b> for both sides
      (UP/DOWN bid+ask) and the <b>Binance spot move</b> vs the window open
      (spot_bps). We then record the <b>real winner</b> once Polymarket resolves.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">🔬 WHAT WE INVESTIGATE</div>
      <div class="ex-b">Two signals, measured against the real outcome:<br>
      &bull; <b>hit_book</b> — did the book-favoured side win? (ungated, ~81% in backtest)<br>
      &bull; <b>hit_gate</b> — did BOTH the book AND a &ge;5bps Binance move agree, and win? (gated, ~96% in backtest)</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">📊 RESULTS WE EXPECT</div>
      <div class="ex-b">The two bars at the top tell the story:<br>
      &bull; <b>BOOK ACC (ungated)</b> should sit near <b>~81%</b>.<br>
      &bull; <b>GATE ACC (gated)</b> should sit near <b>~96%</b>.<br>
      &bull; <b>GATE COVERAGE</b> = % of windows the &ge;5bps gate actually fires on (~25-40%).</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">⚖️ HOW A VERDICT LOOKS</div>
      <div class="ex-b"><b>LIVE / KEEP GATE:</b> GATE ACC &ge; ~94% (clears the taker fee breakeven)
      with a CI that excludes 90%.<br>
      <b>PARKED / DROP GATE:</b> GATE ACC &le; breakeven, or no lift over BOOK ACC.<br>
      <b>INCONCLUSIVE:</b> not enough resolved windows yet (need ~150-300).</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">🧭 POSSIBLE SCENARIOS</div>
      <div class="ex-b">
      &bull; <b>Reproduces backtest:</b> BOOK ~81%, GATE ~96% &rarr; gate is real, keep it.<br>
      &bull; <b>Book strong, gate flat:</b> BOOK ~90%+, GATE no better &rarr; gate adds nothing; drop it.<br>
      &bull; <b>Both weak:</b> BOOK &lt; fee breakeven &rarr; the whole signal is noise; strategy rethink.<br>
      &bull; <b>Coverage too low:</b> gate fires &lt;15% of windows &rarr; rarely usable even if accurate.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">🔎 INDICATORS TO WATCH</div>
      <div class="ex-b">
      &bull; <b>WINDOWS RESOLVED</b> climbing past ~150 (sample becoming real).<br>
      &bull; <b>OPEN</b> lane draining as windows resolve.<br>
      &bull; <b>SETTLE</b> cards: green border = hit_gate &check;, red = miss &cross;.<br>
      &bull; A stable gap <b>GATE ACC &minus; BOOK ACC</b> of ~10-15 pts = the gate is earning its place.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">⏳ WHAT IT TAKES TO GET A VERDICT</div>
      <div class="ex-b">~300 windows &asymp; <b>25 hours</b> of live collection. The collector runs
      24/7 in the same container, writing only to <code>COLLECTOR_DB</code> (never the
      bot's trades.db). No action needed from you — just let it run and watch the
      bars converge.</div>
    </div>
  </div>
</div>
"""

_PAGE_HEAD = r"""
<style>
 :root{--bg:#0a0c0d;--pan:#121618;--pan2:#161b1e;--bd:#232a2e;--tx:#d6dbd8;
       --dim:#79847f;--am:#eda92c;--gn:#46c46a;--rd:#e2564f;--bl:#5b9bd5;--pu:#9b7fd4}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--tx);
      font:14.5px ui-monospace,SFMono-Regular,Menlo,monospace}
 .bar{display:flex;align-items:center;gap:12px;padding:7px 14px;
      border-bottom:1px solid var(--bd);background:var(--pan)}
 .bar b{color:var(--am);letter-spacing:1.4px;font-size:16px}
 .nav{display:flex;gap:6px;margin-left:10px}
 .nav a{color:var(--dim);text-decoration:none;font-size:12px;padding:3px 10px;
         border:1px solid var(--bd);border-radius:4px}
 .nav a.cur{color:var(--bg);background:var(--am);border-color:var(--am);font-weight:700}
 .chip{border:1px solid var(--gn);color:var(--gn);padding:2px 9px;font-size:11.5px;letter-spacing:1.4px}
 .foot{display:flex;gap:18px;align-items:center;padding:6px 14px;
       border-top:1px solid var(--bd);background:var(--pan);color:var(--dim);font-size:11px;flex-wrap:wrap}
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
       gap:7px;padding:9px 12px;border-bottom:1px solid var(--bd)}
 .k{border:1px solid var(--bd);background:var(--pan);padding:6px 9px}
 .k .n{color:var(--dim);font-size:10.5px;letter-spacing:.8px}
 .k .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
 .kan{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;padding:10px 12px;align-items:start}
 @media(max-width:1250px){.kan{grid-template-columns:repeat(2,1fr)}}
 .lane{border:1px solid var(--bd);background:var(--pan);display:flex;flex-direction:column;min-height:150px}
 .lane h3{margin:0;padding:8px 11px;font-size:11.5px;letter-spacing:1.3px;
          border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;font-weight:700}
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
 .up{border-left-color:var(--gn)} .dn{border-left-color:var(--rd)}
 .win{border-left-color:var(--gn)} .loss{border-left-color:var(--rd)}
 .g{color:var(--gn)}.r{color:var(--rd)}.a{color:var(--am)}.d{color:var(--dim)}
 table{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:8px}
 th{color:var(--dim);text-align:right;font-weight:400;padding:3px 4px;border-bottom:1px solid var(--bd)}
 th:first-child,td:first-child{text-align:left}
 td{padding:3px 4px;text-align:right;font-variant-numeric:tabular-nums}
 .explain{border-bottom:1px solid var(--bd);padding:10px 12px;background:#0d1011}
 .ex-head{color:var(--am);font-weight:700;letter-spacing:1.2px;font-size:12px;margin-bottom:8px}
 .ex-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px}
 .ex-card{border:1px solid var(--bd);background:var(--pan);padding:8px 10px;border-radius:5px}
 .ex-h{color:var(--am);font-weight:700;font-size:11.5px;margin-bottom:4px;letter-spacing:.5px}
 .ex-b{color:var(--tx);font-size:12px;line-height:1.6}
 .ex-b code{color:var(--bl);background:#10141a;padding:0 4px;border-radius:3px}
</style>

<div class="bar">
  <b>GATE_COLLECTOR</b><span class="d">·</span><span>BTC 5MIN</span>
  <span class="chip">READ-ONLY</span>
  <span class="nav">
    <a href="/">LIVE</a>
    <a href="/kanban">KANBAN</a>
    <a href="/collector" class="cur">COLLECTOR</a>
  </span>
  <span style="flex:1"></span>
  <span id="clock" class="d"></span>
</div>
""" + _EXPLAINER + r"""
<div class="kpis" id="kpis"></div>
<div class="kan" id="kan"></div>
"""

_PAGE_TAIL = r"""
<div class="foot" id="foot"></div>
<script>
const $=x=>document.getElementById(x);
const usd=v=>v==null?'-':'$'+Number(v).toFixed(2);
const pct=v=>v==null?'-':v.toFixed(1)+'%';
const cls=v=>v==null?'':(v>=0?'g':'r');
const hhmm=t=>t?new Date(t*1000).toLocaleTimeString():'-';

async function tick(){
  let s; try{ s=await (await fetch('/api/collector-state',{cache:'no-store'})).json(); }
  catch(e){ return; }
  $('clock').textContent=new Date().toLocaleTimeString();
  const st=s.stats||{}, w=s.windows||[];
  const K=(n,v,sub,c)=>`<div class="k"><div class="n">${n}</div>
      <div class="v ${c||''}">${v}</div><div class="s">${sub||''}</div></div>`;
  const gap = (st.book_acc!=null && st.gate_acc!=null)
      ? (st.gate_acc - st.book_acc).toFixed(1)+' pts' : '—';
  $('kpis').innerHTML =
      K('WINDOWS RESOLVED', st.n||0, 'in sample', '')
    + K('BOOK ACC (ungated)', pct(st.book_acc), 'favoured side hit', cls((st.book_acc||0)-50))
    + K('GATE ACC (gated)', pct(st.gate_acc), '|spot|>=5bps hit', cls((st.gate_acc||0)-90))
    + K('GATE GAP', gap, 'gate - book', cls((st.gate_acc||0)-(st.book_acc||0)))
    + K('GATE COVERAGE', pct(st.gate_coverage), '% windows gate-eligible', '')
    + K('HIT BOOK', st.hit_book||0, 'raw wins', 'g')
    + K('HIT GATE', st.hit_gate||0, 'gated wins', 'g')
    + K('OPEN', st.open||0, 'awaiting resolution', 'a');

  // Flow lanes by status.
  const lane=(id,title,c,cards)=>`<div class="lane ${c}"><h3><span>${title}</span>
      <span class="cnt">${cards.length}</span></h3>
      <div class="body">${cards.join('')||'<div class="d" style="padding:6px">—</div>'}</div></div>`;
  const card=x=>`<div class="card ${x.winner==='UP'?'up':'dn'} ${x.status==='RESOLVED'?(x.hit_gate?'win':'loss'):''}">
      <div class="top"><span class="d">…${x.market_slug?x.market_slug.slice(-10):''}</span>
        <span class="${x.winner?(x.winner==='UP'?'g':'r'):'d'}">${x.winner||(x.status==='RESOLVED'?'?':'PENDING')}</span></div>
      <div class="sub">book ${x.book_favored||'-'} · spot ${x.spot_favored||'-'}
        (${x.spot_bps==null?'-':x.spot_bps.toFixed(1)+'bps'})</div>
      ${x.status==='RESOLVED'?`<div class="sub">book ${x.hit_book?'✓':'✗'} · gate ${x.hit_gate?'✓':'✗'}</div>`:''}
    </div>`;
  const watch=w.filter(x=>x.status==='OPEN'&&!x.snap_ts);
  const gate =w.filter(x=>x.status==='OPEN'&&x.snap_ts&&x.spot_bps==null);
  const fire =w.filter(x=>x.status==='OPEN'&&x.snap_ts&&x.spot_bps!=null);
  const settle=w.filter(x=>x.status==='RESOLVED');
  $('kan').innerHTML =
      lane('t1','① WATCH','l1',watch.map(card))
    + lane('t2','② GATE','l2',gate.map(card))
    + lane('t3','③ FIRE (snapshot)','l3',fire.map(card))
    + lane('t4','④ HOLD','l4',[])
    + lane('t5','⑤ SETTLE','l5',settle.slice(0,40).map(card));

  // Footer w/ deploy meta.
  let meta={}; try{ meta=await (await fetch('/api/meta',{cache:'no-store'})).json(); }catch(e){}
  $('foot').innerHTML = s.present===false
    ? `<span class="a">collector DB not present yet — collector may still be starting (${s.db})</span>`
    : `<span>collector db: ${s.db}</span>`
    + (meta.deploy_sha?`<span>sha: ${meta.deploy_sha}</span>`:'')
    + (meta.railway_deploy_id?`<span>railway: ${meta.railway_deploy_id.slice(0,8)}</span>`:'');
}
tick(); setInterval(tick,3000);
</script>
"""

PAGE = _PAGE_HEAD + _PAGE_TAIL
