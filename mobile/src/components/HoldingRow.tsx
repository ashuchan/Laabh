import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { Holding } from '../api/queries/portfolio';
import { colors } from '../utils/colors';
import { formatINR, formatPct } from '../utils/formatters';

export function HoldingRow({ holding }: { holding: Holding }) {
  const pnl = holding.pnl ?? 0;
  const pnlColor = pnl >= 0 ? colors.profit : colors.loss;

  return (
    <View style={styles.row}>
      <View style={styles.left}>
        <Text style={styles.symbol}>{holding.instrument_id.slice(0, 8)}</Text>
        <Text style={styles.qty}>{holding.quantity} qty @ {formatINR(holding.avg_buy_price)}</Text>
      </View>
      <View style={styles.right}>
        {holding.current_price && (
          <Text style={styles.ltp}>{formatINR(holding.current_price)}</Text>
        )}
        <Text style={[styles.pnl, { color: pnlColor }]}>
          {pnl >= 0 ? '+' : ''}{formatINR(pnl, false)} ({formatPct(holding.pnl_pct ?? 0)})
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 12,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  left: { flex: 1 },
  right: { alignItems: 'flex-end' },
  symbol: { fontSize: 14, color: colors.text, fontWeight: '600' },
  qty: { fontSize: 12, color: colors.textSecondary, marginTop: 2 },
  ltp: { fontSize: 14, color: colors.text, fontWeight: '600' },
  pnl: { fontSize: 12, marginTop: 2 },
});
