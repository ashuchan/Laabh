import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React, { useEffect } from 'react';
import { StatusBar } from 'expo-status-bar';
import { TabNavigator } from './src/navigation/TabNavigator';
import { usePriceStore } from './src/stores/priceStore';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
});

export default function App() {
  const connect = usePriceStore((s) => s.connect);
  const disconnect = usePriceStore((s) => s.disconnect);

  useEffect(() => {
    connect();
    return () => disconnect();
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <StatusBar style="light" />
      <TabNavigator />
    </QueryClientProvider>
  );
}
