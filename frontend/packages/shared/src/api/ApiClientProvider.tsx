import React, { createContext, useContext } from 'react';
import type { AxiosInstance } from 'axios';

const Ctx = createContext<AxiosInstance | null>(null);

export function ApiClientProvider({
  client,
  children,
}: {
  client: AxiosInstance;
  children: React.ReactNode;
}) {
  return <Ctx.Provider value={client}>{children}</Ctx.Provider>;
}

export function useApiClient(): AxiosInstance {
  const c = useContext(Ctx);
  if (!c) throw new Error('ApiClientProvider missing from tree');
  return c;
}
