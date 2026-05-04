import React from 'react';
import { ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { colors } from '../utils/colors';

const ITEMS: Array<{
  key: string;
  title: string;
  description: string;
  icon: string;
  route: string;
}> = [
  {
    key: 'daily',
    title: 'Daily Report',
    description: 'Pipeline, ingestion, LLM cost, trading P&L, decision quality',
    icon: '📊',
    route: 'DailyReport',
  },
  {
    key: 'fno',
    title: 'F&O Candidates',
    description: 'Phase funnel · scores · LLM thesis · VIX · ban list',
    icon: '🎯',
    route: 'FNOCandidates',
  },
  {
    key: 'strategy',
    title: 'Strategy Decisions',
    description: 'Morning · intraday · square-off LLM reasoning',
    icon: '🧠',
    route: 'StrategyDecisions',
  },
  {
    key: 'signals',
    title: 'Signal Performance',
    description: 'Hit rate, P&L per signal across windows',
    icon: '📈',
    route: 'SignalPerformance',
  },
  {
    key: 'health',
    title: 'System Health',
    description: 'Source status · tier coverage · open chain issues',
    icon: '⚙️',
    route: 'SystemHealth',
  },
  {
    key: 'analysts',
    title: 'Analyst Leaderboard',
    description: 'Credibility, hit rate, signal volume',
    icon: '🏆',
    route: 'Analysts',
  },
];

export function ReportsHubScreen({ navigation }: any) {
  return (
    <ScrollView style={styles.screen} contentContainerStyle={{ paddingBottom: 40 }}>
      <Text style={styles.subtitle}>Reports & Diagnostics</Text>
      {ITEMS.map((item) => (
        <TouchableOpacity
          key={item.key}
          style={styles.item}
          onPress={() => navigation.navigate(item.route)}
        >
          <Text style={styles.icon}>{item.icon}</Text>
          <View style={{ flex: 1 }}>
            <Text style={styles.title}>{item.title}</Text>
            <Text style={styles.description}>{item.description}</Text>
          </View>
          <Text style={styles.chevron}>›</Text>
        </TouchableOpacity>
      ))}

      <View style={styles.divider} />

      <TouchableOpacity style={styles.item} onPress={() => navigation.navigate('Settings')}>
        <Text style={styles.icon}>⚙️</Text>
        <View style={{ flex: 1 }}>
          <Text style={styles.title}>Settings</Text>
          <Text style={styles.description}>Backend URL, notifications</Text>
        </View>
        <Text style={styles.chevron}>›</Text>
      </TouchableOpacity>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },
  subtitle: {
    color: colors.textSecondary,
    fontSize: 11,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    marginBottom: 10,
  },
  item: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    padding: 14,
    marginBottom: 8,
    gap: 12,
  },
  icon: { fontSize: 24 },
  title: { color: colors.text, fontSize: 15, fontWeight: '600' },
  description: { color: colors.textSecondary, fontSize: 11, marginTop: 2 },
  chevron: { color: colors.textMuted, fontSize: 22, fontWeight: '300' },
  divider: {
    height: 1,
    backgroundColor: colors.border,
    marginVertical: 12,
  },
});
