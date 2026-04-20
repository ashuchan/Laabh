import React from 'react';
import { RefreshControl, ScrollView, StyleSheet } from 'react-native';
import { useAnalystLeaderboard } from '../api/queries/analysts';
import { AnalystRow } from '../components/AnalystRow';
import { colors } from '../utils/colors';

export function AnalystsScreen() {
  const { data: analysts, refetch, isLoading } = useAnalystLeaderboard();

  return (
    <ScrollView
      style={styles.screen}
      refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} />}
    >
      {analysts?.map((a, i) => (
        <AnalystRow key={a.id} analyst={a} rank={i + 1} />
      ))}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, paddingHorizontal: 16 },
});
