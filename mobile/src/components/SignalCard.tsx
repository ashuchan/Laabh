import React from 'react';
import { StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { Signal } from '../api/queries/signals';
import { colors } from '../utils/colors';
import { formatINR, formatPct, timeAgo } from '../utils/formatters';
import { ConvergenceMeter } from './ConvergenceMeter';
import { SentimentBadge } from './SentimentBadge';

interface Props {
  signal: Signal;
  onTrade?: () => void;
}

export function SignalCard({ signal, onTrade }: Props) {
  const conf = signal.confidence ? `${(signal.confidence * 100).toFixed(0)}%` : null;

  return (
    <View style={styles.card}>
      <View style={styles.header}>
        <SentimentBadge action={signal.action as any} />
        <View style={styles.flex} />
        <ConvergenceMeter score={signal.convergence_score} />
        <Text style={styles.time}>{timeAgo(signal.signal_date)}</Text>
      </View>

      {signal.analyst_name_raw && (
        <Text style={styles.analyst}>{signal.analyst_name_raw}</Text>
      )}

      <View style={styles.prices}>
        {signal.entry_price && (
          <PriceTag label="Entry" value={signal.entry_price} />
        )}
        {signal.target_price && (
          <PriceTag label="Target" value={signal.target_price} color={colors.profit} />
        )}
        {signal.stop_loss && (
          <PriceTag label="SL" value={signal.stop_loss} color={colors.loss} />
        )}
        {conf && <Text style={styles.conf}>Conf: {conf}</Text>}
      </View>

      {signal.reasoning && (
        <Text style={styles.reasoning} numberOfLines={2}>{signal.reasoning}</Text>
      )}

      {onTrade && (
        <TouchableOpacity style={styles.tradeBtn} onPress={onTrade}>
          <Text style={styles.tradeBtnText}>Trade</Text>
        </TouchableOpacity>
      )}
    </View>
  );
}

function PriceTag({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <View style={styles.priceTag}>
      <Text style={styles.priceLabel}>{label}</Text>
      <Text style={[styles.priceValue, color ? { color } : null]}>
        {formatINR(value)}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: 10,
    padding: 14,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: colors.border,
  },
  header: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 8 },
  flex: { flex: 1 },
  time: { fontSize: 11, color: colors.textSecondary },
  analyst: { fontSize: 12, color: colors.textSecondary, marginBottom: 8 },
  prices: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginBottom: 8 },
  priceTag: { alignItems: 'center' },
  priceLabel: { fontSize: 10, color: colors.textMuted },
  priceValue: { fontSize: 13, color: colors.text, fontWeight: '600' },
  conf: { fontSize: 12, color: colors.textSecondary, alignSelf: 'center' },
  reasoning: { fontSize: 12, color: colors.textSecondary, lineHeight: 18 },
  tradeBtn: {
    marginTop: 10,
    backgroundColor: colors.primary,
    borderRadius: 6,
    paddingVertical: 8,
    alignItems: 'center',
  },
  tradeBtnText: { color: colors.text, fontSize: 13, fontWeight: '600' },
});
