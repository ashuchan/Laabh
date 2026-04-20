import { create } from 'zustand';
import { BACKEND_URL } from '../utils/constants';

interface SettingsStore {
  backendUrl: string;
  pushEnabled: boolean;
  defaultWatchlistId: string | null;
  setBackendUrl: (url: string) => void;
  setPushEnabled: (enabled: boolean) => void;
  setDefaultWatchlistId: (id: string) => void;
}

export const useSettingsStore = create<SettingsStore>((set) => ({
  backendUrl: BACKEND_URL,
  pushEnabled: true,
  defaultWatchlistId: null,
  setBackendUrl: (backendUrl) => set({ backendUrl }),
  setPushEnabled: (pushEnabled) => set({ pushEnabled }),
  setDefaultWatchlistId: (defaultWatchlistId) => set({ defaultWatchlistId }),
}));
