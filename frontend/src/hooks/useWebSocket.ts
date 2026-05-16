// websocket hook — connects to /ws/<projectId> and streams server-sent events.
//
// reconnects automatically with exponential backoff (1s → 30s cap).
// heartbeat messages are filtered out before hitting state to avoid unnecessary renders.
// event buffer is capped at MAX_EVENTS to prevent memory growth on long runs.

import { useEffect, useRef, useState, useCallback } from 'react';

export interface WSEvent {
  type: string;
  data?: Record<string, unknown>;
}

const MAX_EVENTS = 500;
const MAX_RECONNECT_DELAY = 30_000;

export function useWebSocket(projectId: string | undefined) {
  const [events, setEvents]       = useState<WSEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef              = useRef<WebSocket | null>(null);
  const reconnectTimerRef  = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef         = useRef(0);

  // track intentional teardown so onclose doesn't fire a reconnect after unmount
  const dismountedRef = useRef(false);

  // clear buffered events whenever we switch projects
  useEffect(() => {
    setEvents([]);
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;

    dismountedRef.current = false;

    function connect() {
      // don't open a second socket if one is already connecting or open
      if (wsRef.current && (wsRef.current.readyState === WebSocket.CONNECTING ||
                             wsRef.current.readyState === WebSocket.OPEN)) {
        return;
      }

      // use wss: on https pages so browsers don't block mixed-content
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/${projectId}`);

      ws.onopen = () => {
        setConnected(true);
        attemptRef.current = 0; // reset backoff counter on successful connection
      };

      ws.onclose = () => {
        setConnected(false);
        if (!dismountedRef.current) {
          // exponential backoff: 1s, 2s, 4s, ... capped at MAX_RECONNECT_DELAY
          const delay = Math.min(1000 * 2 ** attemptRef.current, MAX_RECONNECT_DELAY);
          attemptRef.current += 1;
          reconnectTimerRef.current = setTimeout(connect, delay);
        }
      };

      ws.onmessage = (e) => {
        try {
          const event: WSEvent = JSON.parse(e.data);
          if (event.type !== 'heartbeat') {
            setEvents((prev) => {
              const next = [...prev, event];
              // trim oldest events when buffer exceeds cap
              return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
            });
          }
        } catch {
          // ignore malformed messages
        }
      };

      wsRef.current = ws;
    }

    connect();

    return () => {
      dismountedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [projectId]);

  const clearEvents = useCallback(() => setEvents([]), []);

  return { events, connected, clearEvents };
}
