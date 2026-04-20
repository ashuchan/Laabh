export const colors = {
  background: '#0a0a0a',
  surface: '#161616',
  surfaceElevated: '#1e1e1e',
  border: '#2a2a2a',

  text: '#f0f0f0',
  textSecondary: '#888',
  textMuted: '#555',

  // Green for profit/bullish — never reversed
  profit: '#26a69a',
  profitLight: '#1a3a38',
  // Red for loss/bearish — never reversed
  loss: '#ef5350',
  lossLight: '#3a1a1a',

  buy: '#26a69a',
  sell: '#ef5350',
  hold: '#ff9800',
  watch: '#5c6bc0',

  primary: '#5c6bc0',
  accent: '#42a5f5',

  critical: '#ef5350',
  high: '#ff9800',
  medium: '#42a5f5',
  low: '#888',

  chart: {
    portfolio: '#5c6bc0',
    benchmark: '#888',
    candleUp: '#26a69a',
    candleDown: '#ef5350',
    volume: '#2a2a2a',
  },
} as const;

export type Color = keyof typeof colors;
