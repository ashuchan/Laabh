import React from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { usePortfolio, usePortfolioHistory } from '../api/queries/portfolio';
import { useActiveSignals } from '../api/queries/signals';
import { PortfolioChart } from '../components/PortfolioChart';
import { SignalCard } from '../components/SignalCard';
import { useMarketStatus } from '../hooks/useMarketStatus';
import { colors } from '../utils/colors';
import { formatINR, formatPct } from '../utils/formatters';

export function HomeScreen({ navigation }: any) {
  const { data: portfolio, refetch: refetchPortfolio, isLoading } = usePortfolio();
  const { data: history } = usePortfolioHistory(30);
  const { data: signals } = useActiveSignals(3);
  const market = useMarketStatus();

  const pnlColor = (portfolio?.total_pnl ?? 0) >= 0 ? colors.profit : colors.loss;

  return (
    <ScrollView
      style={styles.screen}
      refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetchPortfolio} />}
    >
      {/* Market status banner */}
      <View style={[styles.marketBanner, { backgroundColor: market.isOpen ? colors.profitLight : colors.surfaceElevated }]}>
        <Text style={[styles.marketLabel, { color: market.isOpen ? colors.profit : colors.textSecondary }]}>
          {market.label}
        </Text>
      </View>

      {/* Portfolio card */}
      {portfolio && (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>Portfolio</Text>
          <Text style={styles.totalValue}>
            {formatINR(portfolio.current_value + portfolio.current_cash)}
          </Text>
          <Text style={[styles.pnl, { color: pnlColor }]}>
            {portfolio.total_pnl >= 0 ? '+' : ''}
            {formatINR(portfolio.total_pnl)} ({formatPct(portfolio.total_pnl_pct)})
          </Text>
          <Text style={styles.dayPnl}>
            Today: {portfolio.day_pnl >= 0 ? '+' : ''}{formatINR(portfolio.day_pnl)}
          </Text>
        </View>
      )}

      {/* 30-day chart */}
      {history && history.length > 0 && (
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>30-Day Performance</Text>
          <PortfolioChart snapshots={history} />
        </View>
      )}

      {/* Top signals */}
      {signals && signals.length > 0 && (
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Top Signals Today</Text>
          {signals.map((s) => (
            <SignalCard
              key={s.id}
              signal={s}
              onTrade={() => navigation.navigate('Trade', { signal: s })}
            />
          ))}
        </View>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },
  marketBanner: { borderRadius: 8, padding: 10, marginBottom: 16 },
  marketLabel: { fontSize: 13, fontWeight: '600', textAlign: 'center' },
  card: {
    backgroundColor: colors.surface,
    borderRadius: 12,
    padding: 16,
    marginBottom: 16,
    borderWidth: 1,
    borderColor: colors.border,
  },
  cardTitle: { fontSize: 12, color: colors.textSecondary, marginBottom: 4 },
  totalValue: { fontSize: 28, color: colors.text, fontWeight: '700' },
  pnl: { fontSize: 16, fontWeight: '600', marginTop: 4 },
  dayPnl: { fontSize: 13, color: colors.textSecondary, marginTop: 4 },
  section: { marginBottom: 16 },
  sectionTitle: { fontSize: 14, color: colors.textSecondary, marginBottom: 10, fontWeight: '600' },
});
