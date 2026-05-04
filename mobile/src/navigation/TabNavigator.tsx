import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { NavigationContainer } from '@react-navigation/native';
import { createStackNavigator } from '@react-navigation/stack';
import React from 'react';
import { Text } from 'react-native';
import { AnalystsScreen } from '../screens/AnalystsScreen';
import { DailyReportScreen } from '../screens/DailyReportScreen';
import { FNOCandidatesScreen } from '../screens/FNOCandidatesScreen';
import { HomeScreen } from '../screens/HomeScreen';
import { PortfolioScreen } from '../screens/PortfolioScreen';
import { ReportsHubScreen } from '../screens/ReportsHubScreen';
import { SettingsScreen } from '../screens/SettingsScreen';
import { SignalPerformanceScreen } from '../screens/SignalPerformanceScreen';
import { SignalsScreen } from '../screens/SignalsScreen';
import { StrategyDecisionsScreen } from '../screens/StrategyDecisionsScreen';
import { SystemHealthScreen } from '../screens/SystemHealthScreen';
import { TradeScreen } from '../screens/TradeScreen';
import { WatchlistScreen } from '../screens/WatchlistScreen';
import { colors } from '../utils/colors';

const Tab = createBottomTabNavigator();
const Stack = createStackNavigator();

const stackOptions = {
  headerStyle: { backgroundColor: colors.surface },
  headerTintColor: colors.text,
  headerTitleStyle: { fontWeight: '600' as const },
};

function HomeStack() {
  return (
    <Stack.Navigator screenOptions={stackOptions}>
      <Stack.Screen name="HomeMain" component={HomeScreen} options={{ title: 'Laabh' }} />
      <Stack.Screen name="Trade" component={TradeScreen} options={{ title: 'Trade' }} />
    </Stack.Navigator>
  );
}

function SignalsStack() {
  return (
    <Stack.Navigator screenOptions={stackOptions}>
      <Stack.Screen name="SignalsMain" component={SignalsScreen} options={{ title: 'Signals' }} />
      <Stack.Screen name="Trade" component={TradeScreen} options={{ title: 'Trade' }} />
    </Stack.Navigator>
  );
}

function ReportsStack() {
  return (
    <Stack.Navigator screenOptions={stackOptions}>
      <Stack.Screen name="ReportsHub" component={ReportsHubScreen} options={{ title: 'Reports' }} />
      <Stack.Screen
        name="DailyReport"
        component={DailyReportScreen}
        options={{ title: 'Daily Report' }}
      />
      <Stack.Screen
        name="FNOCandidates"
        component={FNOCandidatesScreen}
        options={{ title: 'F&O Candidates' }}
      />
      <Stack.Screen
        name="StrategyDecisions"
        component={StrategyDecisionsScreen}
        options={{ title: 'Strategy Decisions' }}
      />
      <Stack.Screen
        name="SignalPerformance"
        component={SignalPerformanceScreen}
        options={{ title: 'Signal Performance' }}
      />
      <Stack.Screen
        name="SystemHealth"
        component={SystemHealthScreen}
        options={{ title: 'System Health' }}
      />
      <Stack.Screen name="Analysts" component={AnalystsScreen} options={{ title: 'Analysts' }} />
      <Stack.Screen name="Settings" component={SettingsScreen} options={{ title: 'Settings' }} />
    </Stack.Navigator>
  );
}

export function TabNavigator() {
  return (
    <NavigationContainer>
      <Tab.Navigator
        screenOptions={({ route }) => ({
          headerShown: false,
          tabBarStyle: { backgroundColor: colors.surface, borderTopColor: colors.border },
          tabBarActiveTintColor: colors.primary,
          tabBarInactiveTintColor: colors.textMuted,
          tabBarIcon: ({ color, size }) => {
            const icons: Record<string, string> = {
              Home: '🏠',
              Signals: '📊',
              Trade: '💱',
              Watchlist: '👁',
              Portfolio: '💼',
              Reports: '📑',
            };
            return <Text style={{ fontSize: size - 4 }}>{icons[route.name] ?? '•'}</Text>;
          },
        })}
      >
        <Tab.Screen name="Home" component={HomeStack} />
        <Tab.Screen name="Signals" component={SignalsStack} />
        <Tab.Screen name="Trade" component={TradeScreen} />
        <Tab.Screen name="Watchlist" component={WatchlistScreen} />
        <Tab.Screen name="Portfolio" component={PortfolioScreen} />
        <Tab.Screen name="Reports" component={ReportsStack} />
      </Tab.Navigator>
    </NavigationContainer>
  );
}
