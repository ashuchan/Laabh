import { useEffect, useRef } from 'react';
import * as Notifications from 'expo-notifications';
import { useNotificationStore } from '../stores/notificationStore';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: true,
  }),
});

export function useNotifications(onNotification?: (data: Record<string, unknown>) => void) {
  const { increment } = useNotificationStore();
  const listenerRef = useRef<Notifications.Subscription | null>(null);

  useEffect(() => {
    // Register for push notifications
    (async () => {
      const { status } = await Notifications.requestPermissionsAsync();
      if (status !== 'granted') return;
    })();

    listenerRef.current = Notifications.addNotificationReceivedListener((notification) => {
      increment();
      const data = notification.request.content.data as Record<string, unknown>;
      onNotification?.(data);
    });

    return () => {
      listenerRef.current?.remove();
    };
  }, []);
}
