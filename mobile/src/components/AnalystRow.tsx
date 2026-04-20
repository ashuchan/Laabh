import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { Analyst } from '../api/queries/analysts';
import { colors } from '../utils/colors';

export function AnalystRow({ analyst, rank }: { analyst: Analyst; rank: number }) {
  const score = analyst.credibility_score;
  const hitPct = (analyst.hit_rate * 100).toFixed(0);

  return (
    <View style={styles.row}>
      <Text style={styles.rank}>#{rank}</Text>
      <View style={styles.info}>
        <Text style={styles.name}>{analyst.name}</Text>
        {analyst.organization && (
          <Text style={styles.org}>{analyst.organization}</Text>
        )}
        <View style={styles.barBg}>
          <View style={[styles.barFill, { width: `${score * 100}%` }]} />
        </View>
      </View>
      <View style={styles.stats}>
        <Text style={styles.statMain}>{hitPct}%</Text>
        <Text style={styles.statLabel}>hit rate</Text>
        <Text style={styles.statSub}>{analyst.total_signals} signals</Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 12,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    gap: 10,
  },
  rank: { fontSize: 13, color: colors.textMuted, width: 28 },
  info: { flex: 1 },
  name: { fontSize: 14, color: colors.text, fontWeight: '600' },
  org: { fontSize: 12, color: colors.textSecondary },
  barBg: { height: 4, backgroundColor: colors.border, borderRadius: 2, marginTop: 6 },
  barFill: { height: 4, backgroundColor: colors.accent, borderRadius: 2 },
  stats: { alignItems: 'flex-end' },
  statMain: { fontSize: 16, color: colors.profit, fontWeight: '700' },
  statLabel: { fontSize: 10, color: colors.textMuted },
  statSub: { fontSize: 11, color: colors.textSecondary },
});
