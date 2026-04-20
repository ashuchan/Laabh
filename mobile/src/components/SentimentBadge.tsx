import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors } from '../utils/colors';

type Action = 'BUY' | 'SELL' | 'HOLD' | 'WATCH';

const ACTION_COLORS: Record<Action, string> = {
  BUY: colors.buy,
  SELL: colors.sell,
  HOLD: colors.hold,
  WATCH: colors.watch,
};

export function SentimentBadge({ action }: { action: Action }) {
  const bg = ACTION_COLORS[action] ?? colors.textMuted;
  return (
    <View style={[styles.badge, { backgroundColor: bg + '33', borderColor: bg }]}>
      <Text style={[styles.text, { color: bg }]}>{action}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
    borderWidth: 1,
  },
  text: {
    fontSize: 11,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
});
