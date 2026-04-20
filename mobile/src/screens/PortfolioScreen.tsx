import React, { useState } from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { useHoldings, usePortfolio, usePortfolioHistory } from '../api/queries/portfolio';
import { HoldingRow } from '../components/HoldingRow';
import { PortfolioChart } from '../components/PortfolioChart';
import { colors } from '../utils/colors';
import { formatINR, formatPct } from '../utils/formatters';

const PERIODS = [7, 30, 90, 365] as const;

export function PortfolioScreen() {
  const [days, setDays] = useState<number>(30);
  const { data: portfolio, refetch, isLoading } = usePortfolio();
  const { data: holdings } = useHoldings();
  const { data: history } = usePortfolioHistory(days);

  return (
    <ScrollView
      style={styles.screen}
      refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} />}
    >
      {portfolio && (
        <View style={styles.summaryCard}>
          <View style={styles.row}>
            <Stat label="Total Value" value={formatINR(portfolio.current_value + portfolio.current_cash)} />
            <Stat label="Invested" value={formatINR(portfolio.invested_value)} />
          </View>
          <View style={styles.row}>
            <Stat
              label="P&L"
              value={`${portfolio.total_pnl >= 0 ? '+' : ''}${formatINR(portfolio.total_pnl)}`}
              color={(portfolio.total_pnl ?? 0) >= 0 ? colors.profit : colors.loss}
            />
            <Stat label="Win Rate" value={`${(portfolio.win_rate * 100).toFixed(0)}%`} />
          </View>
        </View>
      )}

      {/* Period selector */}
      <View style={styles.periods}>
        {PERIODS.map((d) => (
          <TouchableOpacity
            key={d}
            style={[styles.periodBtn, days === d && styles.periodBtnActive]}
            onPress={() => setDays(d)}
          >
            <Text style={[styles.periodText, days === d && styles.periodTextActive]}>
              {d === 365 ? '1Y' : `${d}D`}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {history && <PortfolioChart snapshots={history} />}

      {/* Holdings */}
      <Text style={styles.sectionTitle}>Holdings ({holdings?.length ?? 0})</Text>
      {holdings?.map((h) => (
        <HoldingRow key={h.id} holding={h} />
      ))}
    </ScrollView>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={[styles.statValue, color ? { color } : null]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },
  summaryCard: {
    backgroundColor: colors.surface,
    borderRadius: 12,
    padding: 16,
    marginBottom: 16,
    borderWidth: 1,
    borderColor: colors.border,
    gap: 12,
  },
  row: { flexDirection: 'row', justifyContent: 'space-between' },
  stat: {},
  statLabel: { fontSize: 11, color: colors.textSecondary },
  statValue: { fontSize: 16, color: colors.text, fontWeight: '600', marginTop: 2 },
  periods: {
    flexDirection: 'row',
    gap: 8,
    marginBottom: 12,
  },
  periodBtn: {
    paddingHorizontal: 14,
    paddingVertical: 6,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: colors.border,
  },
  periodBtnActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  periodText: { fontSize: 12, color: colors.textSecondary },
  periodTextActive: { color: colors.text, fontWeight: '600' },
  sectionTitle: { fontSize: 14, color: colors.textSecondary, fontWeight: '600', marginTop: 16, marginBottom: 8 },
});
