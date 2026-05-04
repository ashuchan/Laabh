import React, { useState } from 'react';
import {
  Modal,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { FNOCandidate, useFNOBanList, useFNOCandidates, useVIXHistory } from '../api/queries/fno';
import { colors } from '../utils/colors';

type Phase = 1 | 2 | 3;

function asNumber(v: number | string | null | undefined): number | null {
  if (v == null) return null;
  const n = typeof v === 'string' ? parseFloat(v) : v;
  return Number.isFinite(n) ? n : null;
}

export function FNOCandidatesScreen() {
  const [phase, setPhase] = useState<Phase>(3);
  const [passedOnly, setPassedOnly] = useState<boolean>(false);
  const [selected, setSelected] = useState<FNOCandidate | null>(null);

  const { data, refetch, isLoading } = useFNOCandidates({ phase, passed_only: passedOnly, limit: 200 });
  const { data: vix } = useVIXHistory(1);
  const { data: bans } = useFNOBanList();

  const latestVix = vix?.[0];
  const banCount = bans?.length ?? 0;

  return (
    <View style={styles.screen}>
      {/* Top bar: VIX + ban count */}
      <View style={styles.topBar}>
        <View style={styles.topStat}>
          <Text style={styles.topStatLabel}>VIX</Text>
          <Text
            style={[
              styles.topStatValue,
              latestVix?.regime === 'high' && { color: colors.loss },
              latestVix?.regime === 'low' && { color: colors.profit },
            ]}
          >
            {latestVix ? Number(latestVix.vix_value).toFixed(2) : '—'}
          </Text>
          {latestVix?.regime && <Text style={styles.topStatMeta}>{latestVix.regime}</Text>}
        </View>
        <View style={styles.topStat}>
          <Text style={styles.topStatLabel}>F&O Ban</Text>
          <Text style={[styles.topStatValue, banCount > 0 && { color: colors.hold }]}>
            {banCount}
          </Text>
          <Text style={styles.topStatMeta}>active</Text>
        </View>
      </View>

      {/* Phase chips */}
      <View style={styles.filters}>
        {([1, 2, 3] as Phase[]).map((p) => (
          <TouchableOpacity
            key={p}
            style={[styles.chip, phase === p && styles.chipActive]}
            onPress={() => setPhase(p)}
          >
            <Text style={[styles.chipText, phase === p && styles.chipTextActive]}>
              Phase {p}
            </Text>
          </TouchableOpacity>
        ))}
        <TouchableOpacity
          style={[styles.chip, passedOnly && styles.chipActive]}
          onPress={() => setPassedOnly((v) => !v)}
        >
          <Text style={[styles.chipText, passedOnly && styles.chipTextActive]}>Passed only</Text>
        </TouchableOpacity>
      </View>

      <ScrollView
        refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} tintColor={colors.text} />}
        contentContainerStyle={{ paddingBottom: 40 }}
      >
        {data?.length === 0 && (
          <Text style={styles.empty}>No candidates for phase {phase}</Text>
        )}

        {data?.map((c) => (
          <TouchableOpacity key={c.id} style={styles.row} onPress={() => setSelected(c)}>
            <View style={styles.rowLeft}>
              <Text style={styles.symbol}>{c.symbol ?? c.instrument_id.slice(0, 8)}</Text>
              <View style={styles.metaRow}>
                {c.iv_regime && <Tag label={c.iv_regime} color={colors.medium} />}
                {c.oi_structure && <Tag label={c.oi_structure} color={colors.accent} />}
                {c.passed_liquidity === false && <Tag label="illiquid" color={colors.loss} />}
                {c.technical_pass === false && <Tag label="tech fail" color={colors.loss} />}
              </View>
            </View>
            <View style={styles.rowRight}>
              {(() => {
                const composite = asNumber(c.composite_score);
                const conv = asNumber(c.convergence_score);
                return (
                  <>
                    {composite != null && (
                      <Text style={styles.scoreMain}>{composite.toFixed(2)}</Text>
                    )}
                    {conv != null && (
                      <Text style={styles.scoreSub}>conv {conv.toFixed(2)}</Text>
                    )}
                  </>
                );
              })()}
            </View>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* Detail modal */}
      <Modal
        visible={!!selected}
        animationType="slide"
        transparent
        onRequestClose={() => setSelected(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setSelected(null)}>
          <Pressable style={styles.modalCard} onPress={() => {}}>
            <ScrollView>
              <View style={styles.modalHeader}>
                <Text style={styles.modalSymbol}>
                  {selected?.symbol ?? selected?.instrument_id?.slice(0, 8)}
                </Text>
                <TouchableOpacity onPress={() => setSelected(null)}>
                  <Text style={styles.modalClose}>✕</Text>
                </TouchableOpacity>
              </View>
              <Text style={styles.modalSub}>
                Phase {selected?.phase} · run {selected?.run_date}
              </Text>

              <ScoreGrid c={selected} />

              {selected?.llm_thesis && (
                <View style={styles.thesisBlock}>
                  <Text style={styles.thesisTitle}>LLM Thesis</Text>
                  <Text style={styles.thesisBody}>{selected.llm_thesis}</Text>
                </View>
              )}

              {selected?.llm_decision && (
                <View style={styles.thesisBlock}>
                  <Text style={styles.thesisTitle}>LLM Decision</Text>
                  <Text style={styles.thesisBody}>{selected.llm_decision}</Text>
                </View>
              )}

              <Text style={styles.modalFooter}>
                config: {selected?.config_version ?? 'n/a'}
              </Text>
            </ScrollView>
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

function ScoreGrid({ c }: { c: FNOCandidate | null }) {
  if (!c) return null;
  const items: Array<[string, number | null]> = [
    ['Composite', asNumber(c.composite_score)],
    ['Convergence', asNumber(c.convergence_score)],
    ['News', asNumber(c.news_score)],
    ['Sentiment', asNumber(c.sentiment_score)],
    ['FII/DII', asNumber(c.fii_dii_score)],
    ['Macro', asNumber(c.macro_align_score)],
  ];
  const liq: Array<[string, string | null]> = [
    ['ATM OI', c.atm_oi != null ? c.atm_oi.toLocaleString() : null],
    ['ATM spread', asNumber(c.atm_spread_pct) != null ? `${asNumber(c.atm_spread_pct)!.toFixed(2)}%` : null],
    ['Avg vol 5d', c.avg_volume_5d != null ? c.avg_volume_5d.toLocaleString() : null],
  ];

  return (
    <View>
      <Text style={styles.scoreSection}>Scores</Text>
      <View style={styles.scoreGrid}>
        {items.map(([label, val]) => (
          <View key={label} style={styles.scoreCell}>
            <Text style={styles.scoreCellLabel}>{label}</Text>
            <Text style={styles.scoreCellValue}>{val != null ? val.toFixed(2) : '—'}</Text>
          </View>
        ))}
      </View>

      <Text style={styles.scoreSection}>Liquidity</Text>
      <View style={styles.scoreGrid}>
        {liq.map(([label, val]) => (
          <View key={label} style={styles.scoreCell}>
            <Text style={styles.scoreCellLabel}>{label}</Text>
            <Text style={styles.scoreCellValue}>{val ?? '—'}</Text>
          </View>
        ))}
      </View>
    </View>
  );
}

function Tag({ label, color }: { label: string; color: string }) {
  return (
    <View style={[styles.tag, { borderColor: color }]}>
      <Text style={[styles.tagText, { color }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },

  topBar: {
    flexDirection: 'row',
    gap: 10,
    marginBottom: 12,
  },
  topStat: {
    flex: 1,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    padding: 12,
  },
  topStatLabel: { color: colors.textSecondary, fontSize: 11 },
  topStatValue: { color: colors.text, fontSize: 22, fontWeight: '700', marginTop: 2 },
  topStatMeta: { color: colors.textMuted, fontSize: 11, marginTop: 2 },

  filters: { flexDirection: 'row', gap: 8, marginBottom: 12, flexWrap: 'wrap' },
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

  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    padding: 12,
    marginBottom: 8,
  },
  rowLeft: { flex: 1 },
  rowRight: { alignItems: 'flex-end' },
  symbol: { color: colors.text, fontSize: 14, fontWeight: '700' },
  metaRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 4, marginTop: 4 },
  scoreMain: { color: colors.text, fontSize: 16, fontWeight: '700' },
  scoreSub: { color: colors.textMuted, fontSize: 11, marginTop: 2 },

  tag: { borderWidth: 1, borderRadius: 4, paddingHorizontal: 6, paddingVertical: 1 },
  tagText: { fontSize: 9, fontWeight: '600' },

  modalBackdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.7)', justifyContent: 'flex-end' },
  modalCard: {
    backgroundColor: colors.surface,
    borderTopLeftRadius: 16,
    borderTopRightRadius: 16,
    padding: 16,
    maxHeight: '85%',
  },
  modalHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  modalSymbol: { color: colors.text, fontSize: 20, fontWeight: '700' },
  modalSub: { color: colors.textSecondary, fontSize: 12, marginBottom: 14 },
  modalClose: { color: colors.text, fontSize: 22, paddingHorizontal: 8 },

  scoreSection: {
    color: colors.textSecondary,
    fontSize: 11,
    fontWeight: '600',
    textTransform: 'uppercase',
    marginTop: 12,
    marginBottom: 6,
    letterSpacing: 0.5,
  },
  scoreGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  scoreCell: {
    width: '31%',
    backgroundColor: colors.surfaceElevated,
    borderRadius: 8,
    padding: 8,
  },
  scoreCellLabel: { color: colors.textSecondary, fontSize: 10 },
  scoreCellValue: { color: colors.text, fontSize: 13, fontWeight: '600', marginTop: 2 },

  thesisBlock: { marginTop: 16 },
  thesisTitle: {
    color: colors.textSecondary,
    fontSize: 11,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    marginBottom: 6,
  },
  thesisBody: { color: colors.text, fontSize: 13, lineHeight: 19 },

  modalFooter: { color: colors.textMuted, fontSize: 10, marginTop: 16, marginBottom: 20 },
});
