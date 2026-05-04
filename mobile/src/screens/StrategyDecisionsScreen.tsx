import React, { useState } from 'react';
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { useStrategyDecisions } from '../api/queries/reports';
import { colors } from '../utils/colors';
import { formatINR, formatIST } from '../utils/formatters';

type DecisionFilter = 'ALL' | 'morning_allocation' | 'intraday_action' | 'eod_squareoff';

const TYPE_LABEL: Record<Exclude<DecisionFilter, 'ALL'>, string> = {
  morning_allocation: 'Morning',
  intraday_action: 'Intraday',
  eod_squareoff: 'Square-off',
};

const TYPE_COLOR: Record<Exclude<DecisionFilter, 'ALL'>, string> = {
  morning_allocation: colors.medium,
  intraday_action: colors.accent,
  eod_squareoff: colors.hold,
};

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

export function StrategyDecisionsScreen() {
  const [date, setDate] = useState<string>(todayISO());
  const [filter, setFilter] = useState<DecisionFilter>('ALL');

  const { data, refetch, isLoading } = useStrategyDecisions({
    date,
    decision_type: filter === 'ALL' ? undefined : filter,
  });

  const filtered = data ?? [];

  return (
    <View style={styles.screen}>
      {/* Date selector */}
      <View style={styles.dateRow}>
        <TouchableOpacity
          onPress={() => {
            const d = new Date(date + 'T00:00:00Z');
            d.setUTCDate(d.getUTCDate() - 1);
            setDate(d.toISOString().slice(0, 10));
          }}
          style={styles.dateBtn}
        >
          <Text style={styles.dateBtnText}>‹</Text>
        </TouchableOpacity>
        <Text style={styles.dateLabel}>{date}</Text>
        <TouchableOpacity
          disabled={date === todayISO()}
          onPress={() => {
            const d = new Date(date + 'T00:00:00Z');
            d.setUTCDate(d.getUTCDate() + 1);
            setDate(d.toISOString().slice(0, 10));
          }}
          style={[styles.dateBtn, date === todayISO() && { opacity: 0.3 }]}
        >
          <Text style={styles.dateBtnText}>›</Text>
        </TouchableOpacity>
      </View>

      {/* Type filter */}
      <View style={styles.filters}>
        {(['ALL', 'morning_allocation', 'intraday_action', 'eod_squareoff'] as DecisionFilter[]).map(
          (f) => (
            <TouchableOpacity
              key={f}
              style={[styles.chip, filter === f && styles.chipActive]}
              onPress={() => setFilter(f)}
            >
              <Text style={[styles.chipText, filter === f && styles.chipTextActive]}>
                {f === 'ALL' ? 'All' : TYPE_LABEL[f]}
              </Text>
            </TouchableOpacity>
          ),
        )}
      </View>

      <ScrollView
        contentContainerStyle={{ paddingBottom: 40 }}
        refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} tintColor={colors.text} />}
      >
        {filtered.length === 0 && (
          <Text style={styles.empty}>No strategy decisions for this filter</Text>
        )}

        {filtered.map((d) => {
          const color = TYPE_COLOR[d.decision_type] ?? colors.medium;
          return (
            <View key={d.id} style={styles.card}>
              <View style={styles.cardHeader}>
                <View style={[styles.typeTag, { borderColor: color }]}>
                  <Text style={[styles.typeText, { color }]}>
                    {TYPE_LABEL[d.decision_type] ?? d.decision_type}
                  </Text>
                </View>
                <Text style={styles.timeStamp}>{formatIST(d.as_of)}</Text>
              </View>

              <View style={styles.metaRow}>
                <Stat label="Executed" value={String(d.actions_executed)} color={colors.profit} />
                <Stat
                  label="Skipped"
                  value={String(d.actions_skipped)}
                  color={d.actions_skipped > 0 ? colors.hold : colors.textSecondary}
                />
                {d.budget_available != null && (
                  <Stat label="Budget" value={formatINR(d.budget_available, false)} />
                )}
                {d.risk_profile && <Stat label="Risk" value={d.risk_profile} />}
              </View>

              {d.llm_reasoning && (
                <View style={styles.reasoningBlock}>
                  <Text style={styles.reasoningLabel}>LLM Reasoning</Text>
                  <Text style={styles.reasoning}>{d.llm_reasoning}</Text>
                  {d.llm_model && (
                    <Text style={styles.modelTag}>model: {d.llm_model}</Text>
                  )}
                </View>
              )}
            </View>
          );
        })}
      </ScrollView>
    </View>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={[styles.statValue, color ? { color } : null]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },

  dateRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 12,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  dateBtn: {
    paddingHorizontal: 16,
    paddingVertical: 4,
    borderRadius: 6,
    backgroundColor: colors.surfaceElevated,
  },
  dateBtnText: { color: colors.text, fontSize: 22, fontWeight: '600' },
  dateLabel: { color: colors.text, fontSize: 14, fontWeight: '600' },

  filters: { flexDirection: 'row', gap: 8, marginBottom: 12, flexWrap: 'wrap' },
  chip: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: colors.border,
  },
  chipActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  chipText: { fontSize: 11, color: colors.textSecondary },
  chipTextActive: { color: colors.text, fontWeight: '600' },

  empty: { textAlign: 'center', color: colors.textMuted, marginTop: 40 },

  card: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    padding: 14,
    marginBottom: 10,
  },
  cardHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 10,
  },
  typeTag: {
    borderWidth: 1,
    borderRadius: 4,
    paddingHorizontal: 8,
    paddingVertical: 2,
  },
  typeText: { fontSize: 11, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.5 },
  timeStamp: { color: colors.textMuted, fontSize: 11 },

  metaRow: {
    flexDirection: 'row',
    gap: 10,
    marginBottom: 10,
    flexWrap: 'wrap',
  },
  stat: { flex: 1, minWidth: '22%' },
  statLabel: { fontSize: 10, color: colors.textSecondary },
  statValue: { fontSize: 13, color: colors.text, fontWeight: '600', marginTop: 2 },

  reasoningBlock: {
    backgroundColor: colors.surfaceElevated,
    borderRadius: 8,
    padding: 10,
    marginTop: 4,
  },
  reasoningLabel: {
    color: colors.textSecondary,
    fontSize: 10,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    marginBottom: 4,
  },
  reasoning: { color: colors.text, fontSize: 12, lineHeight: 18 },
  modelTag: { color: colors.textMuted, fontSize: 10, marginTop: 6 },
});
