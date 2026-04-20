import React from 'react';
import { Dimensions, StyleSheet, Text, View } from 'react-native';
import { Snapshot } from '../api/queries/portfolio';
import { colors } from '../utils/colors';

const WIDTH = Dimensions.get('window').width - 32;
const HEIGHT = 120;

interface Props {
  snapshots: Snapshot[];
}

export function PortfolioChart({ snapshots }: Props) {
  if (snapshots.length < 2) {
    return (
      <View style={styles.empty}>
        <Text style={styles.emptyText}>Not enough data for chart</Text>
      </View>
    );
  }

  // Simple SVG-less chart using View widths (replace with react-native-wagmi-charts in prod)
  const values = snapshots.map((s) => s.cumulative_pnl_pct ?? 0).reverse();
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  const barWidth = WIDTH / values.length;

  return (
    <View style={styles.container}>
      {values.map((v, i) => {
        const heightPct = ((v - min) / range) * HEIGHT;
        const isPositive = v >= 0;
        return (
          <View
            key={i}
            style={[
              styles.bar,
              {
                width: barWidth - 1,
                height: Math.max(2, heightPct),
                backgroundColor: isPositive ? colors.chart.portfolio : colors.loss,
              },
            ]}
          />
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    height: HEIGHT,
    backgroundColor: colors.surface,
    borderRadius: 8,
    padding: 4,
    overflow: 'hidden',
  },
  bar: { borderRadius: 1 },
  empty: {
    height: HEIGHT,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderRadius: 8,
  },
  emptyText: { color: colors.textMuted, fontSize: 12 },
});
