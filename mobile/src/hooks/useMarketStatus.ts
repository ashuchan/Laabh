import { useMemo } from 'react';
import {
  MARKET_CLOSE_HOUR,
  MARKET_CLOSE_MIN,
  MARKET_OPEN_HOUR,
  MARKET_OPEN_MIN,
} from '../utils/constants';

interface MarketStatus {
  isOpen: boolean;
  label: string;
  minutesToClose: number | null;
  minutesToOpen: number | null;
}

function getISTTime(): Date {
  return new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
}

export function useMarketStatus(): MarketStatus {
  return useMemo(() => {
    const now = getISTTime();
    const day = now.getDay(); // 0=Sun, 6=Sat
    const isWeekday = day >= 1 && day <= 5;

    const openMins = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN;
    const closeMins = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN;
    const currentMins = now.getHours() * 60 + now.getMinutes();

    const isOpen = isWeekday && currentMins >= openMins && currentMins < closeMins;

    if (!isWeekday) {
      return { isOpen: false, label: 'Market Closed (Weekend)', minutesToClose: null, minutesToOpen: null };
    }

    if (isOpen) {
      const minutesToClose = closeMins - currentMins;
      const h = Math.floor(minutesToClose / 60);
      const m = minutesToClose % 60;
      return {
        isOpen: true,
        label: `Market Open — closes in ${h}h ${m}m`,
        minutesToClose,
        minutesToOpen: null,
      };
    }

    if (currentMins < openMins) {
      const minutesToOpen = openMins - currentMins;
      return {
        isOpen: false,
        label: `Market opens in ${minutesToOpen}m`,
        minutesToClose: null,
        minutesToOpen,
      };
    }

    return { isOpen: false, label: 'Market Closed', minutesToClose: null, minutesToOpen: null };
  }, []);
}
