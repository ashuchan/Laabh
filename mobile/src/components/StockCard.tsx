import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors } from '../utils/colors';
import { formatINR, formatPct } from '../utils/formatters';

interface Props {
  symbol: string;
  companyName: string;
  ltp: number | null;
  changePct: number | null;
  signalCount?: number;
}

export function StockCard({ symbol, companyName, ltp, changePct, signalCount }: Props) {
  const isPositive = (changePct ?? 0) >= 0;
  const changeColor = isPositive ? colors.profit : colors.loss;

  return (
    <View style={styles.card}>
      <Text style={styles.symbol}>{symbol}</Text>
      <Text style={styles.name} numberOfLines={1}>{companyName}</Text>
      {ltp != null && <Text style={styles.ltp}>{formatINR(ltp)}</Text>}
      {changePct != null && (
        <Text style={[styles.change, { color: changeColor }]}>
          {formatPct(changePct)}
        </Text>
      )}
      {signalCount != null && signalCount > 0 && (
        <View style={styles.badge}>
          <Text style={styles.badgeText}>{signalCount}</Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: 8,
    padding: 12,
    width: 130,
    borderWidth: 1,
    borderColor: colors.border,
  },
  symbol: { fontSize: 13, color: colors.text, fontWeight: '700' },
  name: { fontSize: 11, color: colors.textSecondary, marginTop: 2 },
  ltp: { fontSize: 15, color: colors.text, fontWeight: '600', marginTop: 6 },
  change: { fontSize: 12, marginTop: 2 },
  badge: {
    position: 'absolute',
    top: 8,
    right: 8,
    backgroundColor: colors.primary,
    borderRadius: 10,
    paddingHorizontal: 6,
    paddingVertical: 2,
  },
  badgeText: { fontSize: 10, color: colors.text, fontWeight: '700' },
});
