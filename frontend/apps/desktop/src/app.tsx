import React, { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReactQueryDevtools } from '@tanstack/react-query-devtools';
import { RouterProvider } from '@tanstack/react-router';
import { ApiClientProvider, usePriceStore } from '@laabh/shared';
import { apiClient, WS_URL } from './lib/client';
import { router } from './router';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      refetchOnWindowFocus: true,
      staleTime: 30_000,
    },
  },
});

function PriceStoreConnector() {
  const connect = usePriceStore((s) => s.connect);
  const disconnect = usePriceStore((s) => s.disconnect);

  useEffect(() => {
    connect(WS_URL);
    return () => disconnect();
  }, [connect, disconnect]);

  return null;
}

export default function App() {
  return (
    <ApiClientProvider client={apiClient}>
      <QueryClientProvider client={queryClient}>
        <PriceStoreConnector />
        <RouterProvider router={router} />
        {import.meta.env.DEV && <ReactQueryDevtools initialIsOpen={false} />}
      </QueryClientProvider>
    </ApiClientProvider>
  );
}
