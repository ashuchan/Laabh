export const MARKET_OPEN_HOUR = 9;
export const MARKET_OPEN_MIN = 15;
export const MARKET_CLOSE_HOUR = 15;
export const MARKET_CLOSE_MIN = 30;

export const STALE_TIMES = {
  portfolio: 30_000,
  prices: 5_000,
  signals: 60_000,
  analysts: 300_000,
  reports: 120_000,
  health: 30_000,
} as const;
