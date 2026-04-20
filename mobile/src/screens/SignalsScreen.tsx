import React, { useState } from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { useSignals } from '../api/queries/signals';
import { SignalCard } from '../components/SignalCard';
import { colors } from '../utils/colors';

type ActionFilter = 'ALL' | 'BUY' | 'SELL' | 'HOLD';

export function SignalsScreen({ navigation }: any) {
  const [actionFilter, setActionFilter] = useState<ActionFilter>('ALL');

  const { data: signals, refetch, isLoading } = useSignals({
    status: 'active',
    action: actionFilter === 'ALL' ? undefined : actionFilter,
  });

  return (
    <View style={styles.screen}>
      {/* Filter chips */}
      <View style={styles.filters}>
        {(['ALL', 'BUY', 'SELL', 'HOLD'] as ActionFilter[]).map((f) => (
          <TouchableOpacity
            key={f}
            style={[styles.chip, actionFilter === f && styles.chipActive]}
            onPress={() => setActionFilter(f)}
          >
            <Text style={[styles.chipText, actionFilter === f && styles.chipTextActive]}>{f}</Text>
          </TouchableOpacity>
        ))}
      </View>

      <ScrollView
        refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} />}
      >
        {signals?.length === 0 && (
          <Text style={styles.empty}>No signals found</Text>
        )}
        {signals?.map((s) => (
          <SignalCard
            key={s.id}
            signal={s}
            onTrade={() => navigation.navigate('Trade', { signal: s })}
          />
        ))}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },
  filters: { flexDirection: 'row', gap: 8, marginBottom: 12 },
  chip: {
    paddingHorizontal: 14,
    paddingVertical: 6,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: colors.border,
  },
  chipActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  chipText: { fontSize: 12, color: colors.textSecondary },
  chipTextActive: { color: colors.text, fontWeight: '600' },
  empty: { textAlign: 'center', color: colors.textMuted, marginTop: 40 },
});
