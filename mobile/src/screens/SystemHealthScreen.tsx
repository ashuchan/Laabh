import React, { useState } from 'react';
import {
  Alert,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { useResolveChainIssue } from '../api/queries/fno';
import { useChainIssues, useSourceHealth, useTierCoverage } from '../api/queries/reports';
import { colors } from '../utils/colors';
import { timeAgo } from '../utils/formatters';

type IssueFilter = 'open' | 'resolved' | 'all';
type TierFilter = 'all' | 1 | 2;

export function SystemHealthScreen() {
  const [issueFilter, setIssueFilter] = useState<IssueFilter>('open');
  const [tierFilter, setTierFilter] = useState<TierFilter>('all');
  const [onlyDegraded, setOnlyDegraded] = useState<boolean>(true);

  const { data: sources, refetch: refetchSources, isLoading: loadingSources } = useSourceHealth();
  const {
    data: tiers,
    refetch: refetchTiers,
    isLoading: loadingTiers,
  } = useTierCoverage({
    tier: tierFilter === 'all' ? undefined : tierFilter,
    only_degraded: onlyDegraded,
    limit: 100,
  });
  const {
    data: issues,
    refetch: refetchIssues,
    isLoading: loadingIssues,
  } = useChainIssues(issueFilter);

  const { mutate: resolveIssue, isPending: resolving } = useResolveChainIssue();

  const refreshAll = () => {
    refetchSources();
    refetchTiers();
    refetchIssues();
  };

  const isLoading = loadingSources || loadingTiers || loadingIssues;

  function handleResolve(id: string) {
    Alert.alert('Resolve issue?', 'Mark this chain issue as resolved.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Resolve',
        onPress: () =>
          resolveIssue(id, {
            onError: (e: any) =>
              Alert.alert('Error', e.response?.data?.detail ?? 'Failed to resolve'),
          }),
      },
    ]);
  }

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={{ paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refreshAll} tintColor={colors.text} />}
    >
      {/* Source health */}
      <Text style={styles.sectionTitle}>Source Health</Text>
      {sources && sources.length > 0 ? (
        sources.map((s) => {
          const isHealthy = s.status === 'healthy';
          return (
            <View key={s.source} style={styles.sourceCard}>
              <View style={styles.sourceHeader}>
                <Text style={styles.sourceName}>{s.source}</Text>
                <View
                  style={[
                    styles.statusPill,
                    { backgroundColor: isHealthy ? colors.profitLight : colors.lossLight },
                  ]}
                >
                  <Text
                    style={[
                      styles.statusPillText,
                      { color: isHealthy ? colors.profit : colors.loss },
                    ]}
                  >
                    {s.status}
                  </Text>
                </View>
              </View>
              <View style={styles.sourceMeta}>
                {s.consecutive_errors > 0 && (
                  <Text style={[styles.metaItem, { color: colors.loss }]}>
                    {s.consecutive_errors} consecutive errors
                  </Text>
                )}
                {s.last_success_at && (
                  <Text style={styles.metaItem}>
                    last ok: {timeAgo(s.last_success_at)}
                  </Text>
                )}
                {s.last_error && (
                  <Text style={styles.errorText} numberOfLines={2}>
                    {s.last_error}
                  </Text>
                )}
              </View>
            </View>
          );
        })
      ) : (
        <Text style={styles.empty}>No source health data</Text>
      )}

      {/* Tier coverage */}
      <Text style={styles.sectionTitle}>Coverage by Tier (last 60m)</Text>
      <View style={styles.filters}>
        {(['all', 1, 2] as TierFilter[]).map((t) => (
          <TouchableOpacity
            key={String(t)}
            style={[styles.chip, tierFilter === t && styles.chipActive]}
            onPress={() => setTierFilter(t)}
          >
            <Text style={[styles.chipText, tierFilter === t && styles.chipTextActive]}>
              {t === 'all' ? 'All' : `Tier ${t}`}
            </Text>
          </TouchableOpacity>
        ))}
        <TouchableOpacity
          style={[styles.chip, onlyDegraded && styles.chipActive]}
          onPress={() => setOnlyDegraded((v) => !v)}
        >
          <Text style={[styles.chipText, onlyDegraded && styles.chipTextActive]}>
            Degraded only
          </Text>
        </TouchableOpacity>
      </View>

      {tiers && tiers.length > 0 ? (
        tiers.map((row) => {
          const rate = row.success_rate_1h;
          const rateColor =
            rate == null ? colors.textMuted : rate >= 95 ? colors.profit : rate >= 80 ? colors.hold : colors.loss;
          return (
            <View key={`${row.symbol}-${row.tier}`} style={styles.tierRow}>
              <View style={{ flex: 1 }}>
                <Text style={styles.tierSymbol}>
                  {row.symbol}
                  <Text style={styles.tierTag}>  T{row.tier}</Text>
                </Text>
                <Text style={styles.tierMeta}>
                  {row.last_status ?? 'no data'} · sources:{' '}
                  {Object.entries(row.source_breakdown)
                    .map(([k, v]) => `${k}:${v}`)
                    .join(' / ') || 'n/a'}
                </Text>
              </View>
              <Text style={[styles.tierRate, { color: rateColor }]}>
                {rate != null ? `${rate.toFixed(1)}%` : 'n/a'}
              </Text>
            </View>
          );
        })
      ) : (
        <Text style={styles.empty}>
          {onlyDegraded ? 'No degraded instruments' : 'No coverage data'}
        </Text>
      )}

      {/* Chain issues */}
      <View style={styles.issuesHeader}>
        <Text style={styles.sectionTitle}>Chain Issues</Text>
      </View>
      <View style={styles.filters}>
        {(['open', 'resolved', 'all'] as IssueFilter[]).map((f) => (
          <TouchableOpacity
            key={f}
            style={[styles.chip, issueFilter === f && styles.chipActive]}
            onPress={() => setIssueFilter(f)}
          >
            <Text style={[styles.chipText, issueFilter === f && styles.chipTextActive]}>
              {f}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {issues && issues.length > 0 ? (
        issues.map((iss) => (
          <View key={iss.id} style={styles.issueCard}>
            <View style={styles.issueHeader}>
              <Text style={styles.issueSource}>{iss.source}</Text>
              <Text style={styles.issueType}>{iss.issue_type}</Text>
              <Text style={styles.issueWhen}>
                {iss.detected_at ? timeAgo(iss.detected_at) : 'unknown'}
              </Text>
            </View>
            <Text style={styles.issueMsg} numberOfLines={3}>
              {iss.error_message}
            </Text>
            <View style={styles.issueFooter}>
              {iss.github_issue_url ? (
                <Text style={styles.issueLink}>📎 GitHub issue filed</Text>
              ) : (
                <Text style={styles.issueLinkMuted}>not filed</Text>
              )}
              {iss.resolved_at == null ? (
                <TouchableOpacity
                  style={[styles.resolveBtn, resolving && { opacity: 0.4 }]}
                  disabled={resolving}
                  onPress={() => handleResolve(iss.id)}
                >
                  <Text style={styles.resolveBtnText}>Resolve</Text>
                </TouchableOpacity>
              ) : (
                <Text style={styles.resolvedTag}>
                  resolved {timeAgo(iss.resolved_at)}
                </Text>
              )}
            </View>
          </View>
        ))
      ) : (
        <Text style={styles.empty}>No {issueFilter} issues</Text>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },

  sectionTitle: {
    fontSize: 13,
    color: colors.textSecondary,
    fontWeight: '600',
    marginTop: 14,
    marginBottom: 8,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  empty: { textAlign: 'center', color: colors.textMuted, paddingVertical: 16 },

  sourceCard: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    padding: 12,
    marginBottom: 8,
  },
  sourceHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  sourceName: { color: colors.text, fontSize: 14, fontWeight: '700' },
  statusPill: { paddingHorizontal: 10, paddingVertical: 3, borderRadius: 12 },
  statusPillText: { fontSize: 11, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.5 },
  sourceMeta: { marginTop: 8, gap: 4 },
  metaItem: { color: colors.textSecondary, fontSize: 11 },
  errorText: { color: colors.loss, fontSize: 11, marginTop: 4 },

  filters: { flexDirection: 'row', gap: 8, marginBottom: 10, flexWrap: 'wrap' },
  chip: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: colors.border,
  },
  chipActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  chipText: { fontSize: 11, color: colors.textSecondary },
  chipTextActive: { color: colors.text, fontWeight: '600' },

  tierRow: {
    flexDirection: 'row',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    paddingVertical: 8,
    paddingHorizontal: 12,
    marginBottom: 6,
  },
  tierSymbol: { color: colors.text, fontSize: 13, fontWeight: '700' },
  tierTag: { color: colors.textMuted, fontSize: 10, fontWeight: '500' },
  tierMeta: { color: colors.textSecondary, fontSize: 10, marginTop: 2 },
  tierRate: { fontSize: 14, fontWeight: '700', alignSelf: 'center' },

  issuesHeader: { marginTop: 4 },
  issueCard: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    padding: 12,
    marginBottom: 8,
  },
  issueHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 6 },
  issueSource: { color: colors.text, fontSize: 12, fontWeight: '700' },
  issueType: { color: colors.hold, fontSize: 11 },
  issueWhen: { color: colors.textMuted, fontSize: 10, marginLeft: 'auto' },
  issueMsg: { color: colors.textSecondary, fontSize: 11, lineHeight: 16 },
  issueFooter: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginTop: 8,
  },
  issueLink: { color: colors.medium, fontSize: 11 },
  issueLinkMuted: { color: colors.textMuted, fontSize: 11 },
  resolveBtn: {
    backgroundColor: colors.primary,
    paddingHorizontal: 14,
    paddingVertical: 6,
    borderRadius: 6,
  },
  resolveBtnText: { color: colors.text, fontSize: 11, fontWeight: '700' },
  resolvedTag: { color: colors.profit, fontSize: 11 },
});
