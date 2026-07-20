export interface Wallet {
  eoa: string;
  deposit: string;
  balance_pusd: number | null;
  value_usd: number | null;
}

export interface Market {
  condition_id: string;
  market_slug: string;
  up_token: string;
  down_token: string;
  start_ts: number;
  end_ts: number;
  tick_size: number;
  neg_risk: boolean;
  t_remaining: number;
}

export interface Book {
  best_bid: number | null;
  bid_size: number;
  best_ask: number | null;
  ask_size: number;
}

export interface Position {
  conditionId: string;
  title?: string;
  outcome?: string;
  size?: number;
  curPrice?: number;
  realizedPnl?: number;
  redeemable?: boolean;
  [k: string]: unknown;
}

export interface Decision {
  id: number;
  ts: number;
  market_slug: string;
  side: string | null;
  t_remaining: number;
  ask_price: number | null;
  ask_size: number | null;
  action: string;
  reason: string;
  dry_run: number;
}

export interface Order {
  id: number;
  ts: number;
  market_slug: string;
  condition_id: string;
  token_id: string;
  side: string;
  size: number;
  price: number;
  order_id: string | null;
  status: string;
  filled_size: number;
  error: string | null;
  dry_run: number;
}

export interface PnL {
  realized_usd: number;
  wins: number;
  losses: number;
  pending: number;
}

export interface Config {
  max_entry_price: number;
  loser_floor: number;
  seconds_before_close: number;
  min_t_remaining_sec: number;
  max_entries_per_market: number;
  size_scale: number;
  sim_only: boolean;
  use_spot_gate: boolean;
  min_spot_offset_bps: number;
  max_open_positions: number;
  max_daily_loss_usd: number;
}

export interface SimBucket {
  n: number;
  wins: number;
  cost: number;
  fees: number;
  pnl: number;
  win_rate: number | null;
  breakeven: number | null;
  edge_pts: number | null;
}

export interface SimTotal {
  n?: number;
  wins?: number;
  cost?: number;
  fees?: number;
  pnl?: number;
  shares?: number;
  win_rate?: number | null;
  pnl_bps_of_cost?: number | null;
  pending?: number;
}

export interface SimReport {
  total: SimTotal;
  buckets: Record<string, SimBucket>;
}

export interface SpotState {
  enabled: boolean;
  healthy?: boolean;
  price?: number | null;
  threshold_bps?: number;
  offset_bps?: number | null;
  favored?: string | null;
  gate?: string;
}

export interface Account {
  bankroll: number;
  cash: number;
  deployed: number;
  realized_pnl: number;
  open_value: number;
  equity: number;
  total_pnl: number;
  return_pct: number;
  open_positions: number;
  fills_resolved: number;
  wins: number;
  total_fees: number;
}

export interface SimPosition {
  market_slug: string;
  condition_id: string;
  side: string;
  shares: number;
  cost: number;
  fees: number;
  fills: number;
  avg_price: number;
  mark_price: number | null;
  mark_source: string;
  value: number;
  unrealized: number;
  pending?: boolean;
  closed_secs_ago?: number | null;
}

export interface Settlement {
  market_slug: string;
  side: string;
  shares: number;
  cost: number;
  fees: number;
  fills: number;
  payout: number;
  won: boolean;
  pnl: number;
  resolved_ts: number;
}

export interface Kpi {
  markets: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  total_pnl: number;
  avg_win: number;
  avg_loss: number;
  expectancy: number | null;
  roi_on_cost: number | null;
  profit_factor: number | null;
  breakeven_wr: number | null;
  sharpe: number | null;
  max_drawdown: number;
  max_drawdown_pct: number | null;
  ci_lo: number | null;
  ci_hi: number | null;
  verdict: 'LOSING' | 'WINNING' | 'INCONCLUSIVE' | null;
  open_deployed: number;
  equity_curve: number[];
  gate: {
    n_with_bps: number;
    strong_n: number;
    strong_wr: number | null;
    weak_n: number;
    weak_wr: number | null;
    split_bps: number;
  };
  markets_to_conclusive: number;
}

export interface State {
  now: number;
  bot_running: boolean;
  bot_mode: string;       // 'paper' | 'live' | 'stopped' | 'unknown'
  risk_state: string;
  wallet: Wallet;
  market: Market | null;
  book_up: Book | null;
  book_down: Book | null;
  positions: Position[];
  pnl: PnL;
  config: Config;
  sim: SimReport;
  spot: SpotState;
  account: Account;
  kpi: Kpi;
  sim_positions: SimPosition[];
  settlements: Settlement[];
  decisions: Decision[];
  orders: Order[];
  errors: Record<string, string>;
}
