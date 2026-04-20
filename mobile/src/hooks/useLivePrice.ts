import { useEffect } from 'react';
import { usePriceStore } from '../stores/priceStore';

/**
 * Subscribe to real-time price for a symbol via the WebSocket price store.
 * Returns current price data or null if no data yet.
 */
export function useLivePrice(symbol: string) {
  const { prices, subscribe, connect } = usePriceStore();

  useEffect(() => {
    connect();
    subscribe([symbol]);
  }, [symbol]);

  return prices[symbol] ?? null;
}
