import { create } from 'zustand';
import { WS_URL } from '../utils/constants';

interface PriceData {
  ltp: number;
  change_pct: number;
  updatedAt: number;
}

interface PriceStore {
  prices: Record<string, PriceData>;
  wsStatus: 'connecting' | 'open' | 'closed';
  updatePrice: (symbol: string, ltp: number, change_pct: number) => void;
  subscribe: (symbols: string[]) => void;
  connect: () => void;
  disconnect: () => void;
}

let ws: WebSocket | null = null;
let pendingSubscriptions: string[] = [];

export const usePriceStore = create<PriceStore>((set, get) => ({
  prices: {},
  wsStatus: 'closed',

  updatePrice: (symbol, ltp, change_pct) =>
    set((state) => ({
      prices: { ...state.prices, [symbol]: { ltp, change_pct, updatedAt: Date.now() } },
    })),

  subscribe: (symbols: string[]) => {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: 'subscribe', symbols }));
    } else {
      pendingSubscriptions = [...new Set([...pendingSubscriptions, ...symbols])];
    }
  },

  connect: () => {
    if (ws && ws.readyState !== WebSocket.CLOSED) return;
    set({ wsStatus: 'connecting' });
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      set({ wsStatus: 'open' });
      if (pendingSubscriptions.length > 0) {
        ws!.send(JSON.stringify({ action: 'subscribe', symbols: pendingSubscriptions }));
        pendingSubscriptions = [];
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.symbol) {
          get().updatePrice(msg.symbol, msg.ltp, msg.change_pct);
        }
      } catch {}
    };

    ws.onclose = () => {
      set({ wsStatus: 'closed' });
      // Auto-reconnect after 5 seconds
      setTimeout(() => get().connect(), 5_000);
    };

    ws.onerror = () => ws?.close();
  },

  disconnect: () => {
    ws?.close();
    ws = null;
    set({ wsStatus: 'closed' });
  },
}));
