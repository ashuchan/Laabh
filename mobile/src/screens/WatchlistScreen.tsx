import React, { useState } from 'react';
import { ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { useRemoveFromWatchlist } from '../api/mutations/watchlist';
import { useWatchlistItems, useWatchlists } from '../api/queries/watchlist';
import { colors } from '../utils/colors';

export function WatchlistScreen() {
  const { data: watchlists } = useWatchlists();
  const [activeId, setActiveId] = useState<string | null>(null);

  const selectedId = activeId ?? watchlists?.[0]?.id ?? null;
  const { data: items, refetch } = useWatchlistItems(selectedId ?? '');
  const { mutate: removeItem } = useRemoveFromWatchlist(selectedId ?? '');

  return (
    <View style={styles.screen}>
      {/* Watchlist tabs */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.tabs}>
        {watchlists?.map((w) => (
          <TouchableOpacity
            key={w.id}
            style={[styles.tab, selectedId === w.id && styles.tabActive]}
            onPress={() => setActiveId(w.id)}
          >
            <Text style={[styles.tabText, selectedId === w.id && styles.tabTextActive]}>
              {w.name}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* Items list */}
      <ScrollView>
        {items?.map((item) => (
          <View key={item.id} style={styles.itemRow}>
            <View style={styles.itemInfo}>
              <Text style={styles.instrumentId}>{item.instrument_id.slice(0, 12)}...</Text>
              {item.target_buy_price && (
                <Text style={styles.target}>Target buy: ₹{item.target_buy_price}</Text>
              )}
              {item.price_alert_above && (
                <Text style={styles.alert}>Alert above: ₹{item.price_alert_above}</Text>
              )}
            </View>
            <TouchableOpacity
              style={styles.removeBtn}
              onPress={() => removeItem(item.id)}
            >
              <Text style={styles.removeBtnText}>✕</Text>
            </TouchableOpacity>
          </View>
        ))}
        {items?.length === 0 && (
          <Text style={styles.empty}>No items in this watchlist</Text>
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  tabs: { paddingHorizontal: 16, paddingVertical: 10, flexGrow: 0 },
  tab: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 20,
    marginRight: 8,
    borderWidth: 1,
    borderColor: colors.border,
  },
  tabActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  tabText: { fontSize: 13, color: colors.textSecondary },
  tabTextActive: { color: colors.text, fontWeight: '600' },
  itemRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  itemInfo: { flex: 1 },
  instrumentId: { fontSize: 13, color: colors.text, fontWeight: '600' },
  target: { fontSize: 11, color: colors.profit, marginTop: 2 },
  alert: { fontSize: 11, color: colors.hold, marginTop: 2 },
  removeBtn: { padding: 8 },
  removeBtnText: { color: colors.loss, fontSize: 16 },
  empty: { textAlign: 'center', color: colors.textMuted, marginTop: 40, padding: 16 },
});
