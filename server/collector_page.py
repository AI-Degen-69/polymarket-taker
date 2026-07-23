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
  <div class="ex-head">📌 GATE_COLLECTOR — THE ONE QUESTION</div>
  <div class="ex-lead">Does the <b>Binance gate</b> actually make the bot smarter? The backtest
  claimed: just betting the <b>book-favourite</b> side wins <b>81%</b>; adding the Binance gate
  wins <b>96%</b>. We are collecting <b>live</b> data to confirm that or kill it. Nothing else
  to do but let it run.</div>

  <div class="ex-grid">
    <div class="ex-card">
      <div class="ex-h">📖 WORDS YOU'LL SEE</div>
      <div class="ex-b">
      &bull; <b>BOOK FAVOURITE</b> = which side (UP/DOWN) has the higher bid on Polymarket
      right now. "Book" = <b>CLOB</b> = the live order book / exchange. Higher bid = more
      people betting that side.<br>
      &bull; <b>BPS</b> = basis point = <b>0.01%</b>. 5 bps = 0.05%. It measures how much BTC moved
      on Binance since the window opened. |spot| &ge; 5 bps = a <b>real move</b>, not noise.<br>
      &bull; <b>GATE</b> = the rule: only count a window when Binance moved &ge;5 bps in the
      <b>same direction</b> as the book favourite.<br>
      &bull; <b>81&rarr;96</b> = backtest: book-favourite alone = 81% wins (<i>ungated</i>);
      book + gate = 96% (<i>gated</i>). The gate filters out weak/ambiguous moments.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">🔢 THE TILES (top row) — what each number means</div>
      <div class="ex-b">
      &bull; <b>BOOK ACC (ungated)</b> = % of resolved windows where the book-favourite side
      <b>WON</b>. <span class="g">Green = &ge;81%</span> (matches backtest). Below that = the naive
      signal is weaker than claimed.<br>
      &bull; <b>GATE ACC (gated)</b> = % of gate-fired windows where we'd have <b>WON</b>.
      <span class="g">Green = &ge;94%</span> (above the taker <b>FEE breakeven</b>). This is the
      number that matters.<br>
      &bull; <b>GATE GAP</b> = GATE ACC &minus; BOOK ACC. <span class="g">Green &amp; big = the gate
      earns its place.</span> <span class="r">Red / zero = the gate adds nothing.</span> Dream =
      as positive as possible.<br>
      &bull; <b>GATE COVERAGE</b> = % of windows where the gate even <b>fired</b> (|spot|&ge;5bps).
      <span class="d">White = just info, not a score.</span> Too low (&lt;15%) = gate rarely lets
      you trade.<br>
      &bull; <b>WINDOWS RESOLVED</b> = sample size. Need <b>~150</b> (min) to <b>~300</b> (solid)
      before trusting any %.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">🔥 WHAT WE WANT — THE TWO FLAMES</div>
      <div class="ex-b">We want <b>BOOK HEAT</b> and <b>GATE HEAT</b> BOTH 🔥 green:
      <br>&bull; <b>BOOK HEAT 🔥</b> = book acc is real (&ge;81%)<br>
      &bull; <b>GATE HEAT 🔥</b> = gate acc clears the fee line (&ge;94%) &rarr; the gate is
      <b>VALIDATED</b><br>
      Two flames = <b>KEEP THE GATE</b> (verdict LIVE). One or none = <b>PARKED</b> (gate not
      proven; don't rely on it). We reach it by letting it run ~25h to ~150-300 windows.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">🧭 SCENARIOS</div>
      <div class="ex-b">
      &bull; BOOK ~81% + GATE ~96% &rarr; gate is real, <b>KEEP IT</b>.<br>
      &bull; BOOK ~90%+ but GATE no better &rarr; gate adds nothing, <b>DROP IT</b>.<br>
      &bull; Both weak (&lt; breakeven) &rarr; whole signal is noise, rethink.<br>
      &bull; Coverage &lt;15% &rarr; gate fires too rarely to matter.</div>
    </div>
    <div class="ex-card">
      <div class="ex-h">⏳ WHAT IT TAKES</div>
      <div class="ex-b">~300 windows &asymp; <b>25 hours</b> of live collection. The collector runs
      24/7 in the same container, writing only to <code>COLLECTOR_DB</code> (never the bot's
      trades.db). No action from you — just watch the flames turn green.</div>
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
 .verdict{display:flex;gap:8px;align-items:center;padding:7px 12px;border-bottom:1px solid var(--bd);
          background:#0d1011;flex-wrap:wrap}
 .pill{border:1px solid var(--bd);border-radius:14px;padding:3px 12px;font-size:12px;font-weight:700;
       display:inline-flex;gap:6px;align-items:center}
 .pill.hot{color:var(--gn);border-color:var(--gn);background:#0f1c14}
 .pill.cold{color:var(--dim);border-color:var(--bd);background:transparent}
 .pill small{font-weight:400;color:var(--dim)}
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
 .explain{border-bottom:1px solid var(--bd);padding:10px 12px;background:#0d1011}
 .ex-head{color:var(--am);font-weight:700;letter-spacing:1.2px;font-size:12px;margin-bottom:6px}
 .ex-lead{color:var(--tx);font-size:12.5px;line-height:1.6;margin-bottom:8px}
 .ex-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:8px}
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
<div class="verdict" id="verdict"></div>
<div class="kan" id="kan"></div>
"""

_PAGE_TAIL = r"""
<div class="foot" id="foot"></div>
<script>
const $=x=>document.getElementById(x);
const pct=v=>v==null?'-':v.toFixed(1)+'%';
// Meaningful thresholds (green = meets the bar, NOT 100%).
// book good at >=81 (backtest), gate good at >=94 (fee breakeven).
const bookCls=v=>v==null?'':(v>=81?'g':(v>=70?'a':'r'));
const gateCls=v=>v==null?'':(v>=94?'g':(v>=90?'a':'r'));
const gapCls=v=>v==null?'':(v>0?'g':'r');

async function tick(){
  let s; try{ s=await (await fetch('/api/collector-state',{cache:'no-store'})).json(); }
  catch(e){ return; }
  $('clock').textContent=new Date().toLocaleTimeString();
  const st=s.stats||{}, w=s.windows||[];
  const n=st.n||0;
  const K=(name,v,sub,c)=>`<div class="k"><div class="n">${name}</div>
      <div class="v ${c||''}">${v}</div><div class="s">${sub||''}</div></div>`;
  const gap = (st.book_acc!=null && st.gate_acc!=null)
      ? (st.gate_acc - st.book_acc).toFixed(1)+' pts' : '—';
  $('kpis').innerHTML =
      K('WINDOWS RESOLVED', n, (n>=150?'sample OK':(n>=30?'building':'collecting')), n>=150?'g':(n>=30?'a':''))
    + K('BOOK ACC (ungated)', pct(st.book_acc), 'book side won', bookCls(st.book_acc))
    + K('GATE ACC (gated)', pct(st.gate_acc), 'gate side won', gateCls(st.gate_acc))
    + K('GATE GAP', gap, 'gate - book', gapCls((st.gate_acc||0)-(st.book_acc||0)))
    + K('GATE COVERAGE', pct(st.gate_coverage), '% gate fired', '')   // white = info only
    + K('HIT BOOK', st.hit_book||0, 'raw wins', 'g')
    + K('HIT GATE', st.hit_gate||0, 'gated wins', 'g')
    + K('OPEN', st.open||0, 'awaiting resolve', 'a');

  // Two-flames verdict strip.
  // book_acc is measured over ~all resolved windows (n), so it is stable by n>=30.
  // gate_acc is measured over ONLY the gated windows (gate_n); it needs a
  // reasonable gated base before the >=94% flame means anything. Floor it.
  const bookEnough = n>=30;
  const gateEnough = (st.gate_n||0) >= 20;   // ~20 gated windows before heat is meaningful
  const bookHot = bookEnough && st.book_acc!=null && st.book_acc>=81;
  const gateHot = gateEnough && st.gate_acc!=null && st.gate_acc>=94;
  const gapV = (st.gate_acc||0)-(st.book_acc||0);
  const pill=(hot,label,sub)=>`<span class="pill ${hot?'hot':'cold'}">${hot?'🔥':'❄️'} ${label}
      ${sub?`<small>${sub}</small>`:''}</span>`;
  $('verdict').innerHTML =
      (!bookEnough? `<span class="d" style="font-size:11px">collecting… need ~30 windows before heat is meaningful (${n} now)</span>`
       : (!gateEnough? `<span class="d" style="font-size:11px">gate still warming… need ~20 gated windows (${(st.gate_n||0)} now)</span>` : ''))
    + pill(bookHot, 'BOOK HEAT', bookEnough?`${pct(st.book_acc)}`:'')
    + pill(gateHot, 'GATE HEAT', gateEnough?`${pct(st.gate_acc)} · n=${(st.gate_n||0)}`:'')
    + `<span class="pill ${gapV>0?'hot':'cold'}">${gapV>0?'🔥':'❄️'} GATE GAP
       <small>${gapV>0?'+':''}${gapV.toFixed(1)} pts</small></span>`;

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
