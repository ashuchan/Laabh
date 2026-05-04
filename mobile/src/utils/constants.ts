export const BACKEND_URL = process.env.EXPO_PUBLIC_BACKEND_URL ?? 'http://192.168.1.100:8000';
export const WS_URL = BACKEND_URL.replace(/^http/, 'ws') + '/ws/prices';

export const MARKET_OPEN_HOUR = 9;
export const MARKET_OPEN_MIN = 15;
export const MARKET_CLOSE_HOUR = 15;
export const MARKET_CLOSE_MIN = 30;

export const STALE_TIMES = {
  portfolio: 30_000,    // 30 seconds
  prices: 5_000,        // 5 seconds
  signals: 60_000,      // 1 minute
  analysts: 300_000,    // 5 minutes
  reports: 120_000,     // 2 minutes
  health: 30_000,       // 30 seconds
};
