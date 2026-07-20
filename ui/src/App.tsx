import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { fetchState } from './api';
import type { Account, Book, Decision, Kpi, Order, Settlement, SimPosition, SimReport, SpotState, State } from './types';
import { fmtDur, fmtNum, fmtPx, fmtTime, fmtUsd } from './format';

const POLL_MS = 500;

export default function App() {
  const [state, setState] = useState<State | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState<number>(Date.now() / 1000);
  const [flash, setFlash] = useState<boolean>(false);
  const [lastFlashAt, setLastFlashAt] = useState<number>(0);
  const lastSeenOrderId = useRef<number>(-1);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await fetchState();
        if (cancelled) return;
        setState(s);
        setErr(null);
        const newest = s.orders.length ? s.orders[0].id : -1;
        if (lastSeenOrderId.current < 0) {
          lastSeenOrderId.current = newest;
        } else if (newest > lastSeenOrderId.current) {
          const o = s.orders[0];
          if (o && o.dry_run === 0 && (o.status === 'matched' || o.status === 'filled')) {
            setFlash(false);
            setTimeout(() => {
              setFlash(true);
              setLastFlashAt(Date.now());
            }, 0);
            setTimeout(() => setFlash(false), 5000);
          }
          lastSeenOrderId.current = newest;
        }
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 200);
    return () => clearInterval(id);
  }, []);

  const m = state?.market;
  const tRem = m ? Math.max(0, m.end_ts - now) : null;

  return (
    <>
      {flash && <div className="flash-screen" />}
      <div style={shellStyle}>
        <TopBar
          time={now}
          botRunning={state?.bot_running ?? false}
          botMode={state?.bot_mode ?? 'stopped'}
          riskState={state?.risk_state ?? 'OK'}
          err={err}
          lastFlashAt={lastFlashAt}
        />

        <div style={gridStyle}>
          <div style={colStack}>
            <AccountPanel account={state?.account} />

            <Panel title="STRATEGY · COPY BONEREAPER">
              <Row k="ENTRY BAND" v={`${fmtPx(state?.config.loser_floor)} – ${fmtPx(state?.config.max_entry_price)}`} mono />
              <Row k="WINDOW" v={`${state?.config.min_t_remaining_sec ?? '?'}–${state?.config.seconds_before_close ?? '?'}s`} mono />
              <Row k="FILLS / MKT" v={`≤ ${state?.config.max_entries_per_market ?? '?'}`} mono />
              <Row k="SIZE SCALE" v={`${state?.config.size_scale ?? 1}×`} mono />
              <Row k="GATE" v={`≥ ${state?.config.min_spot_offset_bps ?? '?'} bps`} mono />
            </Panel>

            <SpotPanel spot={state?.spot} />

            <SimPanel sim={state?.sim} />
          </div>

          <div style={colStack}>
            <KpiPanel kpi={state?.kpi} />

            <Panel
              title={m ? `LIVE MARKET · ${m.market_slug}` : 'NO LIVE MARKET'}
              titleHref={m ? `https://polymarket.com/event/${m.market_slug}` : undefined}
            >
              {m ? (
                <>
                  <Row
                    k="COUNTDOWN"
                    v={fmtDur(tRem)}
                    hi
                    big
                    colored={tRem != null && tRem < 60 ? 1 : -1}
                  />
                  <BookView
                    label="UP  "
                    book={state?.book_up}
                    cap={state?.config.max_entry_price ?? 0.95}
                    floor={state?.config.loser_floor ?? 0.85}
                  />
                  <BookView
                    label="DOWN"
                    book={state?.book_down}
                    cap={state?.config.max_entry_price ?? 0.95}
                    floor={state?.config.loser_floor ?? 0.85}
                  />
                </>
              ) : (
                <div style={{ color: 'var(--txt-dim)', padding: '6px 0' }}>
                  awaiting next 5-min BTC window<span className="caret">_</span>
                </div>
              )}
            </Panel>

            <Panel title="DECISION LOG · live" flex>
              <DecisionsTable decisions={state?.decisions ?? []} />
            </Panel>
          </div>

          <div style={colStack}>
            <Panel title={`OPEN POSITIONS · ${state?.account?.open_positions ?? 0}`}>
              <SimPositionsTable positions={state?.sim_positions ?? []} />
            </Panel>

            <Panel title="SETTLEMENTS · resolved" flex>
              <SettlementsTable settlements={state?.settlements ?? []} />
            </Panel>

            <Panel title="ORDERS · recent" flex>
              <OrdersTable orders={state?.orders ?? []} />
            </Panel>
          </div>
        </div>

        <BottomBar state={state} />
      </div>
    </>
  );
}

function TopBar({
  time, botRunning, botMode, riskState, err, lastFlashAt,
}: { time: number; botRunning: boolean; botMode: string; riskState: string; err: string | null; lastFlashAt: number }) {
  const blink = Date.now() - lastFlashAt < 5000;
  const riskOk = riskState === 'OK';
  const botLabel = !botRunning
    ? 'STOPPED'
    : !riskOk
      ? `LOCKED (${riskState})`
      : 'RUNNING';
  const botColor = !botRunning
    ? 'var(--red)'
    : !riskOk
      ? 'var(--amber)'
      : 'var(--green)';

  // mode chip: green for paper (safe), red for live (real money)
  const modeChip = botMode === 'live'
    ? { label: 'LIVE', bg: '#330000', fg: 'var(--red)', border: 'var(--red)' }
    : botMode === 'paper'
      ? { label: 'PAPER', bg: '#001a0d', fg: 'var(--green)', border: 'var(--green)' }
      : { label: 'OFFLINE', bg: 'transparent', fg: 'var(--txt-dim)', border: 'var(--border-hi)' };

  return (
    <div style={topBarStyle}>
      <span style={{ color: 'var(--amber)', fontWeight: 700 }}>POLY_HFT</span>
      <span style={{ color: 'var(--txt-dim)' }}> · </span>
      <span style={{ color: 'var(--txt-hi)' }}>BTC 5MIN</span>
      <span style={{
        marginLeft: 12,
        padding: '1px 8px',
        border: `1px solid ${modeChip.border}`,
        background: modeChip.bg,
        color: modeChip.fg,
        fontWeight: 700,
        letterSpacing: '1.5px',
        fontSize: 10,
      }}>{modeChip.label}</span>
      <span style={spacer} />
      <span style={{ color: botColor, fontWeight: 600 }}>
        ● BOT {botLabel}
      </span>
      <span style={{ color: 'var(--txt-dim)', margin: '0 12px' }}>|</span>
      <span style={{ color: err ? 'var(--red)' : 'var(--green)' }}>
        ● {err ? 'API ERR' : 'API OK'}
      </span>
      {blink && (
        <>
          <span style={{ color: 'var(--txt-dim)', margin: '0 12px' }}>|</span>
          <span style={{ color: 'var(--amber-bright)', fontWeight: 700 }}>◆ TRADE FIRED</span>
        </>
      )}
      <span style={spacer} />
      <span style={{ color: 'var(--txt)' }}>{fmtTime(time)}</span>
    </div>
  );
}

function BottomBar({ state }: { state: State | null }) {
  const errs = state?.errors || {};
  const errKeys = Object.keys(errs);
  return (
    <div style={bottomBarStyle}>
      <span style={{ color: 'var(--txt-dim)' }}>POLL 500ms</span>
      <span style={{ color: 'var(--txt-dim)', margin: '0 12px' }}>·</span>
      <span style={{ color: 'var(--txt-dim)' }}>
        DECISIONS {state?.decisions.length ?? 0} · ORDERS {state?.orders.length ?? 0}
      </span>
      <span style={spacer} />
      {errKeys.length > 0 && (
        <span style={{ color: 'var(--red)' }}>ERR: {errKeys.join(', ')}</span>
      )}
    </div>
  );
}

function Panel({
  title, children, flex, titleHref,
}: { title: string; children: ReactNode; flex?: boolean; titleHref?: string }) {
  return (
    <div style={{
      border: '1px solid var(--border)',
      background: 'var(--bg-panel)',
      display: 'flex',
      flexDirection: 'column',
      flex: flex ? 1 : 'unset',
      minHeight: 0,
    }}>
      <div style={panelTitleStyle}>
        {title}
        {titleHref && (
          <a
            href={titleHref}
            target="_blank"
            rel="noopener noreferrer"
            title="Open this market on Polymarket"
            style={{
              marginLeft: 10,
              color: 'var(--amber)',
              textDecoration: 'none',
              borderBottom: '1px dotted var(--amber)',
              fontWeight: 400,
            }}
          >
            OPEN ON POLYMARKET ↗
          </a>
        )}
      </div>
      <div style={{ padding: '6px 10px', flex: flex ? 1 : 'unset', overflow: 'auto' }}>
        {children}
      </div>
    </div>
  );
}

/**
 * Virtual paper account. CASH is debited the instant a fill happens and
 * credited back at resolution, so it can genuinely run dry — the bot logs
 * SKIP_NO_CASH rather than spending money it doesn't have. EQUITY is cash plus
 * open positions marked to the live book, so it moves tick-by-tick.
 */
function AccountPanel({ account: a }: { account?: Account }) {
  const pnl = a?.total_pnl ?? 0;
  const deployedPct =
    a && a.bankroll ? ((a.deployed / a.bankroll) * 100).toFixed(0) : '0';
  // Balance sheet only. All performance stats (P&L, win rate, fees, edge) live
  // in the KPI pane so there is exactly one home for each number.
  return (
    <Panel title="PAPER ACCOUNT">
      <Row k="EQUITY" v={fmtUsd(a?.equity)} hi big colored={pnl} />
      <Sep />
      <Row k="STARTING" v={fmtUsd(a?.bankroll)} dim mono />
      <Row k="CASH FREE" v={fmtUsd(a?.cash)} mono />
      <Row k="DEPLOYED" v={`${fmtUsd(a?.deployed)} (${deployedPct}%)`} mono />
    </Panel>
  );
}

/**
 * Binance spot gate. GATE reads OPEN when |offset| clears the threshold and the
 * bot may take the agreeing side, FLAT when BTC hasn't moved enough to call the
 * window, and NO FEED when the websocket is stale — which blocks trading
 * entirely rather than silently falling back to an ungated 81% hit rate.
 */
function SpotPanel({ spot }: { spot?: SpotState }) {
  if (spot && !spot.enabled) {
    return (
      <Panel title="SPOT GATE">
        <Row k="STATUS" v="DISABLED" dim />
      </Panel>
    );
  }
  const gate = spot?.gate ?? '—';
  const off = spot?.offset_bps;
  const gateColor = gate === 'OPEN' ? 1 : gate === 'FLAT' ? 0 : -1;

  return (
    <Panel title="SPOT GATE · BINANCE">
      <Row k="BTC/USDT" v={spot?.price != null ? `$${spot.price.toLocaleString()}` : '—'} mono hi />
      <Row
        k="VS WINDOW OPEN"
        v={off != null ? `${off > 0 ? '+' : ''}${off.toFixed(1)} bps` : '—'}
        colored={off ?? 0}
        mono
      />
      <Row k="THRESHOLD" v={`± ${spot?.threshold_bps ?? '?'} bps`} mono dim />
      <Sep />
      <Row k="IMPLIED SIDE" v={spot?.favored ?? '—'} mono hi />
      <Row k="GATE" v={gate} colored={gateColor} mono />
    </Panel>
  );
}

/**
 * Simulation scorecard. The bucket table is the point: a headline win rate is
 * meaningless on its own, because buying at 0.98 needs ~98.1% just to break
 * even after fees. EDGE = realized win rate minus that breakeven, in points.
 * Green means the bucket genuinely paid; red means it lost despite winning.
 */
/**
 * Bird's-eye quant scorecard. Everything here is MARKET-level — one bet per
 * market, not per fill — because fill-weighted stats flatter the result
 * (winning markets accumulate more fills than losing ones). The verdict banner
 * reads the Wilson CI against the payoff-implied breakeven: green only when the
 * whole interval clears the bar, amber while the sample is still ambiguous.
 */
function KpiPanel({ kpi: k }: { kpi?: Kpi }) {
  if (!k || !k.markets) {
    return (
      <Panel title="KPI · BIRD'S EYE">
        <Empty>no settled markets yet<span className="caret">_</span></Empty>
      </Panel>
    );
  }
  const pos = k.total_pnl >= 0;
  const vColor =
    k.verdict === 'WINNING' ? 'var(--green)'
    : k.verdict === 'LOSING' ? 'var(--red)'
    : 'var(--amber)';
  const vText =
    k.verdict === 'INCONCLUSIVE'
      ? `INCONCLUSIVE · ~${k.markets_to_conclusive} more markets`
      : k.verdict ?? '—';
  const pct = (x: number | null) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`);

  return (
    <Panel title="KPI · BIRD'S EYE">
      {/* verdict banner: win rate CI vs the breakeven the payoff demands */}
      <div style={{
        border: `1px solid ${vColor}`, color: vColor,
        padding: '5px 8px', marginBottom: 8, fontSize: 11, fontWeight: 700,
        letterSpacing: '0.5px', display: 'flex', justifyContent: 'space-between',
        fontFamily: 'var(--mono, ui-monospace, monospace)',
      }}>
        <span>{vText}</span>
        <span style={{ fontWeight: 400 }}>
          WR {pct(k.win_rate)} [{pct(k.ci_lo)}–{pct(k.ci_hi)}] vs {pct(k.breakeven_wr)}
        </span>
      </div>

      <Sparkline data={k.equity_curve} up={pos} />

      <div style={kpiGrid}>
        <Kpi label="NET P&L" value={`${pos ? '+' : ''}${fmtUsd(k.total_pnl)}`} color={pos ? 'var(--green)' : 'var(--red)'} big />
        <Kpi label="MARKETS" value={`${k.wins}W / ${k.losses}L`} sub={`${k.markets} total`} />
        <Kpi label="EXPECTANCY" value={fmtUsd(k.expectancy)} sub="per market" color={(k.expectancy ?? 0) >= 0 ? 'var(--green)' : 'var(--red)'} />
        <Kpi label="PROFIT FACTOR" value={k.profit_factor?.toFixed(2) ?? '—'} sub="gross W ÷ L" color={(k.profit_factor ?? 0) >= 1 ? 'var(--green)' : 'var(--red)'} />
        <Kpi label="AVG WIN" value={`+${fmtUsd(k.avg_win)}`} color="var(--green)" />
        <Kpi label="AVG LOSS" value={fmtUsd(k.avg_loss)} color="var(--red)" />
        <Kpi label="MAX DRAWDOWN" value={fmtUsd(-k.max_drawdown)} sub={k.max_drawdown_pct != null ? `${k.max_drawdown_pct.toFixed(1)}%` : ''} color="var(--red)" />
        <Kpi label="ROI ON RISK" value={pct(k.roi_on_cost)} sub="per $ deployed" color={(k.roi_on_cost ?? 0) >= 0 ? 'var(--green)' : 'var(--red)'} />
        <Kpi label="FEES PAID" value={fmtUsd(k.total_fees)} sub="cumulative" color="var(--red)" />
      </div>

      {/* Per-market P&L distribution. The tell of this strategy is the fat left
          tail: a wall of small wins and a few deep losses. */}
      <PnlHistogram bins={k.pnl_histogram} />
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: 'var(--txt-dim)', marginTop: 2 }}>
        <span>← losses ($/market)</span>
        <span>Sharpe {k.sharpe?.toFixed(2) ?? '—'}</span>
        <span>wins →</span>
      </div>

      {/* gate audit — only meaningful once spot_bps rows accumulate */}
      <div style={{ borderTop: '1px solid var(--border)', marginTop: 8, paddingTop: 6 }}>
        <div style={{ ...kpiLabelStyle, marginBottom: 4 }}>SPOT-GATE AUDIT</div>
        {k.gate.n_with_bps === 0 ? (
          <div style={{ color: 'var(--txt-dim)', fontSize: 10.5 }}>
            capturing bps on new fills — check back after ~20 markets
          </div>
        ) : (
          <div style={{ display: 'flex', gap: 16, fontSize: 11, fontFamily: 'var(--mono, ui-monospace, monospace)' }}>
            <span>≥{k.gate.split_bps}bps: <b style={{ color: 'var(--green)' }}>{pct(k.gate.strong_wr)}</b> <span style={{ color: 'var(--txt-dim)' }}>(n={k.gate.strong_n})</span></span>
            <span>&lt;{k.gate.split_bps}bps: <b style={{ color: 'var(--amber)' }}>{pct(k.gate.weak_wr)}</b> <span style={{ color: 'var(--txt-dim)' }}>(n={k.gate.weak_n})</span></span>
          </div>
        )}
      </div>
    </Panel>
  );
}

function Kpi({ label, value, sub, color, big }: { label: string; value: string; sub?: string; color?: string; big?: boolean }) {
  return (
    <div>
      <div style={kpiLabelStyle}>{label}</div>
      <div style={{
        color: color ?? 'var(--txt-hi)', fontWeight: 700,
        fontSize: big ? 17 : 13, fontFamily: 'var(--mono, ui-monospace, monospace)',
        fontVariantNumeric: 'tabular-nums', lineHeight: 1.2,
      }}>{value}</div>
      {sub && <div style={{ color: 'var(--txt-dim)', fontSize: 9.5 }}>{sub}</div>}
    </div>
  );
}

/** Tiny inline equity sparkline — area fill, emphasized endpoint. */
function Sparkline({ data, up }: { data: number[]; up: boolean }) {
  if (!data || data.length < 2) return null;
  const w = 260, h = 34, pad = 2;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (data.length - 1)) * (w - 2 * pad);
  const y = (v: number) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  const pts = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const stroke = up ? 'var(--green)' : 'var(--red)';
  const lastX = x(data.length - 1), lastY = y(data[data.length - 1]);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} style={{ display: 'block', marginBottom: 8 }} preserveAspectRatio="none">
      <polyline points={`${pad},${h - pad} ${pts} ${lastX},${h - pad}`} fill={stroke} opacity="0.12" stroke="none" />
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth="1.3" vectorEffect="non-scaling-stroke" />
      <circle cx={lastX} cy={lastY} r="2" fill={stroke} />
    </svg>
  );
}

/** Per-market P&L histogram — bars left (loss, red) to right (win, green). */
function PnlHistogram({ bins }: { bins: { label: string; count: number; neg: boolean }[] }) {
  if (!bins || !bins.length) return null;
  const max = Math.max(1, ...bins.map((b) => b.count));
  const H = 46;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: H }}>
        {bins.map((b) => (
          <div key={b.label} title={`${b.label}: ${b.count} markets`}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', height: '100%' }}>
            <div style={{
              height: `${(b.count / max) * 100}%`,
              minHeight: b.count ? 2 : 0,
              background: b.neg ? 'var(--red)' : 'var(--green)',
              opacity: b.count ? 0.85 : 0.15,
            }} />
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 2, marginTop: 1 }}>
        {bins.map((b) => (
          <span key={b.label} style={{ flex: 1, textAlign: 'center', fontSize: 7.5, color: 'var(--txt-dim)', overflow: 'hidden' }}>
            {b.count || ''}
          </span>
        ))}
      </div>
    </div>
  );
}

const kpiGrid: CSSProperties = {
  display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '8px 10px',
};
const kpiLabelStyle: CSSProperties = {
  color: 'var(--txt-dim)', fontSize: 9, letterSpacing: '0.6px', textTransform: 'uppercase',
};

function SimPanel({ sim }: { sim?: SimReport }) {
  const t = sim?.total ?? {};
  const buckets = sim?.buckets ?? {};
  const n = t.n ?? 0;

  // Only the per-entry-price breakdown lives here now -- the headline totals
  // moved to the KPI pane (which is MARKET-level; these buckets are per-FILL by
  // price, a different and complementary cut). Keeping both stopped the two
  // panels showing conflicting win-rate numbers.
  return (
    <Panel title="WIN RATE BY ENTRY PRICE · per fill">
      <div style={{ color: 'var(--txt-dim)', fontSize: 10, padding: '0 0 4px' }}>
        {n} resolved fills · WIN must beat NEED to profit at that price
      </div>
      <div style={{ display: 'flex', color: 'var(--txt-dim)', fontSize: '10px', padding: '2px 0' }}>
        <span style={{ flex: 1.3 }}>PRICE</span>
        <span style={{ flex: 0.7, textAlign: 'right' }}>N</span>
        <span style={{ flex: 1, textAlign: 'right' }}>WIN</span>
        <span style={{ flex: 1, textAlign: 'right' }}>NEED</span>
        <span style={{ flex: 1, textAlign: 'right' }}>EDGE</span>
      </div>
      {Object.entries(buckets).map(([label, b]) => {
        const edge = b.edge_pts;
        const color =
          b.n === 0 ? 'var(--txt-dim)'
            : edge == null ? 'var(--txt)'
              : edge > 0 ? 'var(--green)' : 'var(--red)';
        return (
          <div key={label} style={{ display: 'flex', padding: '1px 0', fontSize: '11px' }}>
            <span style={{ flex: 1.3, color: 'var(--txt-dim)' }}>{label}</span>
            <span style={{ flex: 0.7, textAlign: 'right', color: 'var(--txt)' }}>{b.n}</span>
            <span style={{ flex: 1, textAlign: 'right', color: 'var(--txt)' }}>
              {b.win_rate != null ? `${(b.win_rate * 100).toFixed(0)}%` : '—'}
            </span>
            <span style={{ flex: 1, textAlign: 'right', color: 'var(--txt-dim)' }}>
              {b.breakeven != null ? `${(b.breakeven * 100).toFixed(1)}%` : '—'}
            </span>
            <span style={{ flex: 1, textAlign: 'right', color, fontWeight: 700 }}>
              {edge != null ? `${edge > 0 ? '+' : ''}${(edge * 100).toFixed(1)}` : '—'}
            </span>
          </div>
        );
      })}
      {n === 0 && (
        <div style={{ color: 'var(--txt-dim)', padding: '6px 0', fontSize: '11px' }}>
          awaiting first resolved fill…
        </div>
      )}
    </Panel>
  );
}

function Row({
  k, v, hi, big, dim, mono, colored,
}: {
  k: string; v: ReactNode; hi?: boolean; big?: boolean;
  dim?: boolean; mono?: boolean; colored?: number | null;
}) {
  let color: string = hi ? 'var(--txt-hi)' : dim ? 'var(--txt-dim)' : 'var(--txt)';
  if (typeof colored === 'number') {
    color = colored > 0 ? 'var(--green)' : colored < 0 ? 'var(--red)' : color;
  }
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
      <span style={{ color: 'var(--txt-dim)', letterSpacing: '0.5px' }}>{k}</span>
      <span style={{
        color,
        fontFamily: mono ? 'inherit' : undefined,
        fontSize: big ? 16 : undefined,
        fontWeight: big ? 700 : 500,
      }}>{v}</span>
    </div>
  );
}

function Sep() {
  return <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />;
}

function BookView({
  label, book, cap, floor,
}: { label: string; book: Book | null | undefined; cap: number; floor: number }) {
  const ask = book?.best_ask ?? null;
  let askColor = 'var(--txt)';
  if (ask != null) {
    if (ask > cap) askColor = 'var(--txt-dim)';
    else if (ask > floor) askColor = 'var(--amber-bright)';
    else askColor = 'var(--txt-dim)';
  }
  // Bid and ask grouped side by side (no full-width spacer) so the eye reads
  // the spread at a glance instead of scanning across dead space.
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, padding: '5px 0', borderTop: '1px dashed var(--border)', fontSize: 15 }}>
      <span style={{ color: 'var(--txt-dim)', width: 46, fontSize: 12, letterSpacing: '0.5px' }}>{label}</span>
      <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 4 }}>
        <span style={{ color: 'var(--txt-dim)', fontSize: 10 }}>BID</span>
        <span style={{ color: 'var(--green)', fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>{fmtPx(book?.best_bid)}</span>
        <span style={{ color: 'var(--txt-dim)', fontSize: 11 }}>×{fmtNum(book?.bid_size, 0)}</span>
      </span>
      <span style={{ color: 'var(--border-hi)' }}>/</span>
      <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 4 }}>
        <span style={{ color: 'var(--txt-dim)', fontSize: 10 }}>ASK</span>
        <span style={{ color: askColor, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>{fmtPx(ask)}</span>
        <span style={{ color: 'var(--txt-dim)', fontSize: 11 }}>×{fmtNum(book?.ask_size, 0)}</span>
      </span>
    </div>
  );
}

// Defensive: a single malformed row must never blank the whole dashboard.
// A libsql column-name casing quirk once made `action` undefined here, and the
// resulting throw unmounted React entirely.
function actionColor(a?: string): string {
  if (!a) return 'var(--txt-dim)';
  if (a === 'BUY') return 'var(--green)';
  if (a.startsWith('SKIP')) return 'var(--txt-dim)';
  return 'var(--txt)';
}

function DecisionsTable({ decisions }: { decisions: Decision[] }) {
  if (!decisions.length) return <Empty>no decisions yet<span className="caret">_</span></Empty>;
  return (
    <table style={tableStyle}>
      <thead>
        <tr>
          <Th>TIME</Th>
          <Th>MARKET</Th>
          <Th>SIDE</Th>
          <Th right>T_REM</Th>
          <Th right>ASK</Th>
          <Th>ACTION</Th>
          <Th>REASON</Th>
        </tr>
      </thead>
      <tbody>
        {decisions.slice(0, 40).map((d) => (
          <tr key={d.id} style={{ background: d.action === 'BUY' ? 'rgba(0,255,127,0.04)' : undefined }}>
            <Td dim>{fmtTime(d.ts)}</Td>
            <Td dim>{(d.market_slug || '').replace('btc-updown-5m-', '…')}</Td>
            <Td>{d.side ?? '—'}</Td>
            <Td right>{fmtNum(d.t_remaining, 1)}s</Td>
            <Td right>{fmtPx(d.ask_price)}</Td>
            <Td color={actionColor(d.action)} bold>{d.action}</Td>
            <Td dim>{d.reason}</Td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OrdersTable({ orders }: { orders: Order[] }) {
  if (!orders.length) return <Empty>no orders yet<span className="caret">_</span></Empty>;
  return (
    <table style={tableStyle}>
      <thead>
        <tr>
          <Th>TIME</Th>
          <Th>MARKET</Th>
          <Th>SIDE</Th>
          <Th right>SIZE</Th>
          <Th right>PX</Th>
          <Th>STATUS</Th>
          <Th right>FILLED $</Th>
        </tr>
      </thead>
      <tbody>
        {orders.slice(0, 20).map((o) => {
          const ok = o.status === 'matched' || o.status === 'filled';
          return (
            <tr key={o.id}>
              <Td dim>{fmtTime(o.ts)}</Td>
              <Td dim>{(o.market_slug || '').replace('btc-updown-5m-', '…')}</Td>
              <Td>{o.side}</Td>
              <Td right>{fmtNum(o.size, 0)}</Td>
              <Td right>{fmtPx(o.price)}</Td>
              <Td color={ok ? 'var(--green)' : o.status === 'error' ? 'var(--red)' : 'var(--amber)'} bold>{o.status}</Td>
              <Td right>{fmtUsd(o.filled_size)}</Td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * Simulated open positions — what the bot is currently holding into resolution.
 * MARK is the live best bid for that side; positions in a window that is no
 * longer the live one are held at cost (shown dim) rather than guessed at.
 */
function SimPositionsTable({ positions }: { positions: SimPosition[] }) {
  if (!positions.length) return <Empty>no open positions<span className="caret">_</span></Empty>;
  return (
    <table style={tableStyle}>
      <thead>
        <tr>
          <Th>MARKET</Th>
          <Th>SIDE</Th>
          <Th right>SH</Th>
          <Th right>AVG</Th>
          <Th right>MARK</Th>
          <Th right>COST</Th>
          <Th right>UNREAL</Th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p, i) => (
          <tr key={i}>
            <Td dim>…{p.market_slug.slice(-10)}</Td>
            <Td bold color={p.side === 'UP' ? 'var(--green)' : 'var(--red)'}>{p.side}</Td>
            <Td right>{fmtNum(p.shares, 0)}</Td>
            <Td right>{fmtPx(p.avg_price)}</Td>
            {/* A position whose own 5-min window has closed has no live price.
                Showing a mark from whatever market is live NOW would be
                meaningless — it belongs to a different market entirely. */}
            <Td right dim>{p.pending ? '—' : fmtPx(p.mark_price)}</Td>
            <Td right>{fmtUsd(p.cost)}</Td>
            <Td right bold={!p.pending} color={p.pending ? 'var(--amber)' : (p.unrealized >= 0 ? 'var(--green)' : 'var(--red)')}>
              {p.pending
                ? 'AWAITING RESOLUTION'
                : `${p.unrealized >= 0 ? '+' : ''}${fmtUsd(p.unrealized)}`}
            </Td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Resolved windows, newest first — the actual outcome of each market. */
function SettlementsTable({ settlements }: { settlements: Settlement[] }) {
  if (!settlements.length) return <Empty>awaiting first settlement<span className="caret">_</span></Empty>;
  return (
    <table style={tableStyle}>
      <thead>
        <tr>
          <Th>TIME</Th>
          <Th>MARKET</Th>
          <Th>SIDE</Th>
          <Th right>FILLS</Th>
          <Th right>COST</Th>
          <Th right>PAID</Th>
          <Th right>P&L</Th>
        </tr>
      </thead>
      <tbody>
        {settlements.map((s, i) => (
          <tr key={i}>
            <Td dim>{fmtTime(s.resolved_ts)}</Td>
            <Td dim>…{s.market_slug.slice(-10)}</Td>
            <Td bold color={s.won ? 'var(--green)' : 'var(--red)'}>{s.side}</Td>
            <Td right>{s.fills}</Td>
            <Td right>{fmtUsd(s.cost)}</Td>
            <Td right>{fmtUsd(s.payout)}</Td>
            <Td right bold color={s.pnl >= 0 ? 'var(--green)' : 'var(--red)'}>
              {s.pnl >= 0 ? '+' : ''}{fmtUsd(s.pnl)}
            </Td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Th({ children, right }: { children: ReactNode; right?: boolean }) {
  return (
    <th style={{
      textAlign: right ? 'right' : 'left',
      padding: '4px 8px',
      color: 'var(--txt-dim)',
      fontWeight: 400,
      borderBottom: '1px solid var(--border)',
      fontSize: 10,
      letterSpacing: '0.8px',
      position: 'sticky',
      top: 0,
      background: 'var(--bg-panel)',
    }}>{children}</th>
  );
}

function Td({
  children, right, dim, color, bold,
}: { children: ReactNode; right?: boolean; dim?: boolean; color?: string; bold?: boolean }) {
  return (
    <td style={{
      textAlign: right ? 'right' : 'left',
      padding: '3px 8px',
      color: color || (dim ? 'var(--txt-dim)' : 'var(--txt)'),
      fontWeight: bold ? 700 : 400,
      borderBottom: '1px solid rgba(255,255,255,0.02)',
      whiteSpace: 'nowrap',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      maxWidth: 240,
    }}>{children}</td>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div style={{ color: 'var(--txt-dim)', padding: '8px 4px' }}>{children}</div>;
}

const shellStyle: CSSProperties = { height: '100vh', display: 'grid', gridTemplateRows: '28px 1fr 22px' };
const topBarStyle: CSSProperties = {
  display: 'flex', alignItems: 'center', padding: '0 12px',
  borderBottom: '1px solid var(--border-hi)', background: 'var(--bg-panel)',
  fontSize: 11, letterSpacing: '0.5px',
};
const bottomBarStyle: CSSProperties = {
  display: 'flex', alignItems: 'center', padding: '0 12px',
  borderTop: '1px solid var(--border-hi)', background: 'var(--bg-panel)',
  fontSize: 10, letterSpacing: '0.5px',
};
const gridStyle: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '300px 1fr 420px',
  gap: 6, padding: 6, height: '100%', overflow: 'hidden',
};
const colStack: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6, minHeight: 0 };
const panelTitleStyle: CSSProperties = {
  background: 'var(--border)', color: 'var(--amber)',
  padding: '3px 10px', fontSize: 10, letterSpacing: '1.2px', fontWeight: 600,
};
const spacer: CSSProperties = { flex: 1 };
const tableStyle: CSSProperties = { width: '100%', borderCollapse: 'collapse', fontSize: 11 };
