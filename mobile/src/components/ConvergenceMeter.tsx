import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors } from '../utils/colors';

export function ConvergenceMeter({ score, max = 5 }: { score: number; max?: number }) {
  return (
    <View style={styles.row}>
      {Array.from({ length: max }).map((_, i) => (
        <View
          key={i}
          style={[
            styles.dot,
            { backgroundColor: i < score ? colors.accent : colors.border },
          ]}
        />
      ))}
      <Text style={styles.label}>{score}/{max}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  label: { fontSize: 11, color: colors.textSecondary, marginLeft: 4 },
});
