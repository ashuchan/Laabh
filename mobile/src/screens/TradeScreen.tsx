import React, { useState } from 'react';
import {
  Alert,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { useExecuteTrade } from '../api/mutations/trade';
import { usePortfolio } from '../api/queries/portfolio';
import { colors } from '../utils/colors';
import { formatINR } from '../utils/formatters';

type OrderType = 'MARKET' | 'LIMIT' | 'STOP_LOSS';
type TradeType = 'BUY' | 'SELL';

const PCT_BUTTONS = [10, 25, 50, 100];

export function TradeScreen({ route }: any) {
  const signal = route?.params?.signal;
  const prefilledInstrumentId = signal?.instrument_id ?? '';

  const [instrumentId, setInstrumentId] = useState(prefilledInstrumentId);
  const [tradeType, setTradeType] = useState<TradeType>('BUY');
  const [orderType, setOrderType] = useState<OrderType>('MARKET');
  const [quantity, setQuantity] = useState('');
  const [limitPrice, setLimitPrice] = useState('');

  const { data: portfolio } = usePortfolio();
  const { mutate: executeTrade, isPending } = useExecuteTrade();

  function handlePctButton(pct: number) {
    if (!portfolio) return;
    // Rough quantity calculation — use current cash / 1000 as a placeholder price
    const cash = portfolio.current_cash;
    const approxPrice = 1000;
    const qty = Math.floor((cash * pct) / 100 / approxPrice);
    setQuantity(String(Math.max(1, qty)));
  }

  function handleSubmit() {
    const qty = parseInt(quantity, 10);
    if (!instrumentId || isNaN(qty) || qty <= 0) {
      Alert.alert('Error', 'Please fill in all required fields');
      return;
    }

    Alert.alert(
      'Confirm Trade',
      `${tradeType} ${qty} shares (${orderType})`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Execute',
          onPress: () =>
            executeTrade(
              {
                instrument_id: instrumentId,
                trade_type: tradeType,
                order_type: orderType,
                quantity: qty,
                limit_price: limitPrice ? parseFloat(limitPrice) : undefined,
                signal_id: signal?.id,
              },
              {
                onSuccess: () => Alert.alert('Success', 'Order placed!'),
                onError: (e: any) =>
                  Alert.alert('Error', e.response?.data?.detail ?? 'Trade failed'),
              }
            ),
        },
      ]
    );
  }

  return (
    <ScrollView style={styles.screen}>
      {/* Instrument ID input */}
      <Text style={styles.label}>Instrument ID</Text>
      <TextInput
        style={styles.input}
        value={instrumentId}
        onChangeText={setInstrumentId}
        placeholder="Instrument UUID"
        placeholderTextColor={colors.textMuted}
      />

      {/* BUY / SELL toggle */}
      <View style={styles.row}>
        {(['BUY', 'SELL'] as TradeType[]).map((t) => (
          <TouchableOpacity
            key={t}
            style={[
              styles.typeBtn,
              tradeType === t && { backgroundColor: t === 'BUY' ? colors.buy : colors.sell },
            ]}
            onPress={() => setTradeType(t)}
          >
            <Text style={styles.typeBtnText}>{t}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* Order type */}
      <View style={styles.row}>
        {(['MARKET', 'LIMIT', 'STOP_LOSS'] as OrderType[]).map((o) => (
          <TouchableOpacity
            key={o}
            style={[styles.orderTypeBtn, orderType === o && styles.orderTypeBtnActive]}
            onPress={() => setOrderType(o)}
          >
            <Text style={[styles.orderTypeText, orderType === o && { color: colors.text }]}>{o}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* Quantity */}
      <Text style={styles.label}>Quantity</Text>
      <TextInput
        style={styles.input}
        value={quantity}
        onChangeText={setQuantity}
        keyboardType="numeric"
        placeholder="0"
        placeholderTextColor={colors.textMuted}
      />
      <View style={styles.pctRow}>
        {PCT_BUTTONS.map((pct) => (
          <TouchableOpacity key={pct} style={styles.pctBtn} onPress={() => handlePctButton(pct)}>
            <Text style={styles.pctBtnText}>{pct}%</Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* Limit price (shown only for non-market orders) */}
      {orderType !== 'MARKET' && (
        <>
          <Text style={styles.label}>Limit / Trigger Price (₹)</Text>
          <TextInput
            style={styles.input}
            value={limitPrice}
            onChangeText={setLimitPrice}
            keyboardType="decimal-pad"
            placeholder="0.00"
            placeholderTextColor={colors.textMuted}
          />
        </>
      )}

      {/* Available cash */}
      {portfolio && (
        <Text style={styles.cashInfo}>
          Available cash: {formatINR(portfolio.current_cash)}
        </Text>
      )}

      <TouchableOpacity
        style={[styles.submitBtn, isPending && styles.submitBtnDisabled]}
        onPress={handleSubmit}
        disabled={isPending}
      >
        <Text style={styles.submitBtnText}>
          {isPending ? 'Placing...' : 'Execute Trade'}
        </Text>
      </TouchableOpacity>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background, padding: 16 },
  label: { fontSize: 12, color: colors.textSecondary, marginBottom: 6, marginTop: 12 },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    color: colors.text,
    fontSize: 14,
  },
  row: { flexDirection: 'row', gap: 10, marginTop: 12 },
  typeBtn: {
    flex: 1,
    paddingVertical: 12,
    borderRadius: 8,
    alignItems: 'center',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
  },
  typeBtnText: { fontSize: 14, fontWeight: '700', color: colors.text },
  orderTypeBtn: {
    flex: 1,
    paddingVertical: 8,
    borderRadius: 6,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: colors.border,
  },
  orderTypeBtnActive: { borderColor: colors.primary, backgroundColor: colors.primary + '33' },
  orderTypeText: { fontSize: 11, color: colors.textSecondary },
  pctRow: { flexDirection: 'row', gap: 8, marginTop: 8 },
  pctBtn: {
    flex: 1,
    paddingVertical: 6,
    borderRadius: 6,
    alignItems: 'center',
    backgroundColor: colors.surfaceElevated,
  },
  pctBtnText: { fontSize: 12, color: colors.textSecondary },
  cashInfo: { fontSize: 12, color: colors.textSecondary, marginTop: 16, textAlign: 'center' },
  submitBtn: {
    backgroundColor: colors.primary,
    borderRadius: 10,
    paddingVertical: 14,
    alignItems: 'center',
    marginTop: 20,
    marginBottom: 40,
  },
  submitBtnDisabled: { opacity: 0.5 },
  submitBtnText: { color: colors.text, fontSize: 16, fontWeight: '700' },
});
