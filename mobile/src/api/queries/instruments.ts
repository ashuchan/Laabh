import { useQuery } from '@tanstack/react-query';
import client from '../client';

export interface Instrument {
  id: string;
  symbol: string;
  exchange: string;
  company_name: string;
  sector: string | null;
  market_cap_cr: number | null;
  is_fno: boolean;
  is_index: boolean;
}

export interface InstrumentPrice {
  instrument_id: string;
  ltp: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  as_of: string | null;
}

export function useInstruments(q?: string) {
  return useQuery<Instrument[]>({
    queryKey: ['instruments', q],
    queryFn: () => client.get(`/instruments/?q=${q ?? ''}`).then((r) => r.data),
    staleTime: 600_000,
  });
}

export function useInstrumentPrice(instrumentId: string) {
  return useQuery<InstrumentPrice>({
    queryKey: ['price', instrumentId],
    queryFn: () => client.get(`/instruments/${instrumentId}/price`).then((r) => r.data),
    staleTime: 5_000,
    refetchInterval: 5_000,
  });
}
