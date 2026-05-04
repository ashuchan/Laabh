import React, { useState } from 'react';
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { useSignalPerformance } from '../api/queries/reports';
import { colors } from '../utils/colors';
import { formatIST, formatPct } from '../utils/formatters';

const WINDOWS = [7, 30, 90, 365] as const;
type WindowDays = typeof WINDOWS[number];

export function SignalPerformanceScreen() {
  const [days, setDays] = useState<WindowDays>(30);
  const { data, refetch, isLoading } = useSignalPerformance({ days });

  return (
    <View style={styles.screen}>
      {/* Window selector */}
      <View style={styles.periods}>
        {WINDOWS.map((d) => (
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

      {/* Summary card */}
      {data && (
        <View style={styles.summaryCard}>
          <View style={styles.summaryRow}>
            <Stat label="Total" value={String(data.total)} />
            <Stat label="Resolved" value={String(data.resolved)} />
            <Stat
              label="Hit rate"
              value={data.resolved ? `${(data.hit_rate * 100).toFixed(0)}%` : '—'}
              color={
                data.hit_rate >= 0.6 ? colors.profit : data.hit_rate >= 0.4 ? colors.hold : colors.loss
              }
            />
          </View>
          <View style={styles.summaryRow}>
            <Stat label="Hits" value={String(data.hits)} color={colors.profit} />
            <Stat label="Misses" value={String(data.misses)} color={colors.loss} />
            <Stat
              label="Avg P&L"
              value={data.avg_pnl_pct != null ? formatPct(data.avg_pnl_pct) : '—'}
              color={
                data.avg_pnl_pct == null
                  ? colors.textSecondary
                  : data.avg_pnl_pct >= 0
                    ? colors.profit
                    : colors.loss
              }
            />
          </View>
        </View>
      )}

      <Text style={styles.listTitle}>Recent Signals</Text>

      <ScrollView
        contentContainerStyle={{ paddingBottom: 40 }}
        refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} tintColor={colors.text} />}
      >
        {data?.rows.length === 0 && (
          <Text style={styles.empty}>No signals in window</Text>
        )}

        {data?.rows.map((s) => {
          const pnl = s.outcome_pnl_pct;
          const pnlColor = pnl == null ? colors.textSecondary : pnl >= 0 ? colors.profit : colors.loss;
          const actionColor =
            s.action === 'BUY' ? colors.buy : s.action === 'SELL' ? colors.sell : colors.hold;
          return (
            <View key={s.id} style={styles.row}>
              <View style={styles.rowLeft}>
                <View style={styles.rowHeader}>
                  <Text style={styles.symbol}>
                    {s.symbol ?? s.instrument_id.slice(0, 8)}
                  </Text>
                  <View style={[styles.actionTag, { borderColor: actionColor }]}>
                    <Text style={[styles.actionText, { color: actionColor }]}>{s.action}</Text>
                  </View>
                </View>
                <Text style={styles.meta}>
                  {s.analyst_name_raw ?? 'unknown'} · conv {s.convergence_score}
                  {s.confidence != null ? ` · conf ${(s.confidence * 100).toFixed(0)}%` : ''}
                </Text>
                <Text style={styles.metaSmall}>
                  {formatIST(s.signal_date)} · {s.status}
                </Text>
              </View>
              <View style={styles.rowRight}>
                <Text style={[styles.pnl, { color: pnlColor }]}>
                  {pnl != null ? formatPct(pnl) : 'pending'}
                </Text>
                {s.entry_price != null && s.target_price != null && (
                  <Text style={styles.target}>
                    {s.entry_price.toFixed(0)} → {s.target_price.toFixed(0)}
                  </Text>
                )}
              </View>
            </View>
          );
        })}
      </ScrollView>
    </View>
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
  periods: { flexDirection: 'row', gap: 8, marginBottom: 12 },
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

  summaryCard: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    padding: 14,
    marginBottom: 14,
    gap: 12,
  },
  summaryRow: { flexDirection: 'row', gap: 8 },
  stat: { flex: 1 },
  statLabel: { fontSize: 11, color: colors.textSecondary },
  statValue: { fontSize: 16, color: colors.text, fontWeight: '600', marginTop: 2 },

  listTitle: {
    fontSize: 13,
    color: colors.textSecondary,
    fontWeight: '600',
    marginBottom: 8,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  empty: { textAlign: 'center', color: colors.textMuted, marginTop: 40 },

  row: {
    flexDirection: 'row',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    padding: 12,
    marginBottom: 8,
  },
  rowLeft: { flex: 1 },
  rowRight: { alignItems: 'flex-end', justifyContent: 'center' },
  rowHeader: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  symbol: { color: colors.text, fontSize: 14, fontWeight: '700' },
  actionTag: { borderWidth: 1, borderRadius: 4, paddingHorizontal: 6, paddingVertical: 1 },
  actionText: { fontSize: 9, fontWeight: '700' },
  meta: { color: colors.textSecondary, fontSize: 11, marginTop: 4 },
  metaSmall: { color: colors.textMuted, fontSize: 10, marginTop: 2 },
  pnl: { fontSize: 14, fontWeight: '700' },
  target: { color: colors.textMuted, fontSize: 10, marginTop: 4 },
});
