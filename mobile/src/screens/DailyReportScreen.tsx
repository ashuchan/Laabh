import React, { useState } from 'react';
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { useDailyReport } from '../api/queries/reports';
import { colors } from '../utils/colors';
import { formatINR } from '../utils/formatters';

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function shiftDate(iso: string, deltaDays: number): string {
  const d = new Date(iso + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

export function DailyReportScreen() {
  const [date, setDate] = useState<string>(todayISO());
  const { data, refetch, isLoading, isError } = useDailyReport(date);

  const trading = data?.trading;
  const chain = data?.chain_health;
  const llm = data?.llm_activity;
  const pipeline = data?.pipeline_completeness;
  const pnlColor = (trading?.day_pnl ?? 0) >= 0 ? colors.profit : colors.loss;

  return (
    <ScrollView
      style={styles.screen}
      refreshControl={<RefreshControl refreshing={isLoading} onRefresh={refetch} tintColor={colors.text} />}
    >
      {/* Date selector */}
      <View style={styles.dateRow}>
        <TouchableOpacity onPress={() => setDate(shiftDate(date, -1))} style={styles.dateBtn}>
          <Text style={styles.dateBtnText}>‹</Text>
        </TouchableOpacity>
        <Text style={styles.dateLabel}>{date}</Text>
        <TouchableOpacity
          onPress={() => setDate(shiftDate(date, +1))}
          style={[styles.dateBtn, date === todayISO() && styles.dateBtnDisabled]}
          disabled={date === todayISO()}
        >
          <Text style={styles.dateBtnText}>›</Text>
        </TouchableOpacity>
      </View>

      {isError && (
        <View style={styles.errorCard}>
          <Text style={styles.errorText}>Failed to load report</Text>
        </View>
      )}

      {/* Surprises (top) */}
      {data?.surprises && data.surprises.length > 0 && (
        <View style={styles.surpriseCard}>
          <Text style={styles.surpriseTitle}>⚠️ Surprises</Text>
          {data.surprises.map((s, i) => (
            <Text key={i} style={styles.surpriseItem}>
              • {s}
            </Text>
          ))}
        </View>
      )}

      {/* Pipeline */}
      {pipeline && (
        <Section title="Pipeline">
          <View style={styles.row}>
            <Stat label="Ran" value={`${pipeline.ran}/${pipeline.total_scheduled}`} />
            <Stat
              label="Skipped"
              value={String(pipeline.skipped.length)}
              color={pipeline.skipped.length > 0 ? colors.hold : colors.profit}
            />
          </View>
          {pipeline.skipped.length > 0 && (
            <View style={styles.pillRow}>
              {pipeline.skipped.map((s) => (
                <View key={s} style={styles.skipPill}>
                  <Text style={styles.skipPillText}>{s}</Text>
                </View>
              ))}
            </View>
          )}
        </Section>
      )}

      {/* Trading */}
      {trading && (
        <Section title="Trading">
          <View style={styles.row}>
            <Stat
              label="Day P&L"
              value={`${trading.day_pnl >= 0 ? '+' : ''}${formatINR(trading.day_pnl, false)}`}
              color={pnlColor}
            />
            <Stat label="Filled" value={String(trading.filled)} />
          </View>
          <View style={styles.row}>
            <Stat label="Proposed" value={String(trading.proposed)} />
            <Stat label="Scaled out" value={String(trading.scaled_out)} />
          </View>
          <View style={styles.row}>
            <Stat label="Target" value={String(trading.closed_target)} color={colors.profit} />
            <Stat label="Stop" value={String(trading.closed_stop)} color={colors.loss} />
            <Stat label="Time" value={String(trading.closed_time)} color={colors.textSecondary} />
          </View>

          {trading.decision_quality.length > 0 && (
            <>
              <Text style={styles.subTitle}>Decision Quality</Text>
              {trading.decision_quality.map((d, i) => {
                const dPnl = d.final_pnl;
                const dColor =
                  dPnl == null ? colors.textSecondary : dPnl >= 0 ? colors.profit : colors.loss;
                return (
                  <View key={`${d.symbol}-${i}`} style={styles.decisionRow}>
                    <View style={styles.decisionHeader}>
                      <Text style={styles.decisionSymbol}>{d.symbol}</Text>
                      <Text style={[styles.decisionPnl, { color: dColor }]}>
                        {dPnl == null ? 'n/a' : `${dPnl >= 0 ? '+' : ''}${formatINR(dPnl, false)}`}
                      </Text>
                    </View>
                    <Text style={styles.decisionMeta}>
                      {d.strategy} · {d.status}
                    </Text>
                    {d.thesis_excerpt && (
                      <Text style={styles.decisionThesis} numberOfLines={3}>
                        {d.thesis_excerpt}
                      </Text>
                    )}
                  </View>
                );
              })}
            </>
          )}
        </Section>
      )}

      {/* Chain ingestion */}
      {chain && (
        <Section title="Data Ingestion">
          <View style={styles.row}>
            <Stat label="Attempts" value={String(chain.total)} />
            <Stat
              label="OK %"
              value={chain.total ? `${chain.ok_pct.toFixed(1)}%` : '—'}
              color={chain.ok_pct >= 95 ? colors.profit : chain.ok_pct >= 80 ? colors.hold : colors.loss}
            />
          </View>
          <View style={styles.row}>
            <Stat label="Fallback %" value={`${chain.fallback_pct.toFixed(1)}%`} color={colors.hold} />
            <Stat
              label="Missed %"
              value={`${chain.missed_pct.toFixed(1)}%`}
              color={chain.missed_pct > 5 ? colors.loss : colors.textSecondary}
            />
            <Stat
              label="NSE share"
              value={`${chain.nse_share_pct.toFixed(1)}%`}
              color={chain.nse_share_pct >= 80 ? colors.profit : colors.hold}
            />
          </View>
          {chain.issues.length > 0 && (
            <View style={{ marginTop: 8 }}>
              {chain.issues.map((iss) => (
                <Text key={iss.type} style={styles.issueLine}>
                  {iss.type}: {iss.count} ({iss.filed} filed)
                </Text>
              ))}
            </View>
          )}
        </Section>
      )}

      {/* LLM */}
      {llm && (
        <Section title="LLM Activity">
          <View style={styles.row}>
            <Stat label="Calls" value={String(llm.total_rows)} />
            <Stat label="Cost (USD)" value={`$${llm.estimated_cost_usd.toFixed(4)}`} />
          </View>
          <View style={styles.row}>
            <Stat label="Tokens in" value={llm.total_tokens_in.toLocaleString()} />
            <Stat label="Tokens out" value={llm.total_tokens_out.toLocaleString()} />
          </View>
          {llm.callers.length > 0 && (
            <>
              <Text style={styles.subTitle}>By caller</Text>
              {llm.callers.map((c) => (
                <View key={c.caller} style={styles.callerRow}>
                  <Text style={styles.callerName}>{c.caller}</Text>
                  <Text style={styles.callerStats}>
                    {c.row_count} calls · {c.tokens_in.toLocaleString()}/{c.tokens_out.toLocaleString()} tok
                    {c.p95_latency_ms != null ? ` · p95 ${c.p95_latency_ms}ms` : ''}
                  </Text>
                </View>
              ))}
            </>
          )}
        </Section>
      )}

      {/* Candidates */}
      {data?.candidates && Object.keys(data.candidates).length > 0 && (
        <Section title="F&O Candidates">
          <View style={styles.row}>
            {[1, 2, 3].map((p) => (
              <Stat
                key={p}
                label={`Phase ${p}`}
                value={String(data.candidates[`phase${p}`] ?? 0)}
              />
            ))}
          </View>
        </Section>
      )}

      {/* VIX */}
      {data?.vix_stats && data.vix_stats.tick_count != null && (
        <Section title="VIX">
          <View style={styles.row}>
            <Stat label="Avg" value={data.vix_stats.avg_value?.toFixed(2) ?? '—'} />
            <Stat label="Min" value={data.vix_stats.min_value?.toFixed(2) ?? '—'} />
            <Stat label="Max" value={data.vix_stats.max_value?.toFixed(2) ?? '—'} />
          </View>
        </Section>
      )}

      {/* Source health summary */}
      {data?.source_health && data.source_health.length > 0 && (
        <Section title="Source Health">
          {data.source_health.map((s) => (
            <View key={s.source} style={styles.healthRow}>
              <Text style={styles.healthSource}>{s.source}</Text>
              <Text
                style={[
                  styles.healthStatus,
                  { color: s.status === 'healthy' ? colors.profit : colors.loss },
                ]}
              >
                {s.status}
                {s.consecutive_errors > 0 ? ` (${s.consecutive_errors} err)` : ''}
              </Text>
            </View>
          ))}
        </Section>
      )}

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      <View style={styles.card}>{children}</View>
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
    marginBottom: 14,
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
  dateBtnDisabled: { opacity: 0.3 },
  dateBtnText: { color: colors.text, fontSize: 22, fontWeight: '600' },
  dateLabel: { color: colors.text, fontSize: 14, fontWeight: '600' },

  errorCard: {
    backgroundColor: colors.lossLight,
    padding: 12,
    borderRadius: 8,
    marginBottom: 12,
  },
  errorText: { color: colors.loss, fontSize: 13 },

  surpriseCard: {
    backgroundColor: '#3a2a1a',
    borderColor: colors.hold,
    borderWidth: 1,
    borderRadius: 10,
    padding: 12,
    marginBottom: 14,
  },
  surpriseTitle: { color: colors.hold, fontSize: 13, fontWeight: '700', marginBottom: 6 },
  surpriseItem: { color: colors.text, fontSize: 12, marginTop: 2 },

  section: { marginBottom: 14 },
  sectionTitle: {
    fontSize: 13,
    color: colors.textSecondary,
    fontWeight: '600',
    marginBottom: 6,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  card: {
    backgroundColor: colors.surface,
    borderRadius: 12,
    padding: 14,
    borderWidth: 1,
    borderColor: colors.border,
    gap: 10,
  },
  row: { flexDirection: 'row', justifyContent: 'space-between', gap: 10 },
  stat: { flex: 1 },
  statLabel: { fontSize: 11, color: colors.textSecondary },
  statValue: { fontSize: 15, color: colors.text, fontWeight: '600', marginTop: 2 },

  subTitle: {
    fontSize: 12,
    color: colors.textSecondary,
    fontWeight: '600',
    marginTop: 10,
    marginBottom: 4,
  },

  pillRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 4 },
  skipPill: {
    backgroundColor: colors.surfaceElevated,
    borderRadius: 12,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  skipPillText: { fontSize: 10, color: colors.hold },

  decisionRow: {
    backgroundColor: colors.surfaceElevated,
    borderRadius: 8,
    padding: 10,
    marginTop: 6,
  },
  decisionHeader: { flexDirection: 'row', justifyContent: 'space-between' },
  decisionSymbol: { color: colors.text, fontSize: 13, fontWeight: '700' },
  decisionPnl: { fontSize: 13, fontWeight: '700' },
  decisionMeta: { color: colors.textSecondary, fontSize: 11, marginTop: 2 },
  decisionThesis: { color: colors.textSecondary, fontSize: 11, marginTop: 6, lineHeight: 16 },

  callerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 4,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  callerName: { fontSize: 12, color: colors.text, fontWeight: '600', flex: 1 },
  callerStats: { fontSize: 10, color: colors.textSecondary, flex: 2, textAlign: 'right' },

  issueLine: { color: colors.hold, fontSize: 12 },

  healthRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 4,
  },
  healthSource: { fontSize: 12, color: colors.text, fontWeight: '600' },
  healthStatus: { fontSize: 12, fontWeight: '600' },
});
