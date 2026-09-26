/**
 * Resilient WebSocket subscription to a job's progress events.
 *
 * Real-time flow
 * --------------
 *   1. Connect to /ws/jobs/{id}?after_seq={lastSeq}. On the first connection lastSeq = 0,
 *      so the server first replays the whole history, then streams live events.
 *   2. Every event carries a strictly increasing `seq`. We remember the highest one we
 *      handed to the UI and silently drop anything <= lastSeq: replays after a
 *      reconnect can never render an event twice.
 *   3. On an unexpected close (network drop, server restart, laptop sleep) we reconnect
 *      with exponential backoff + jitter, passing lastSeq so the server only sends
 *      what we missed. The UI sees one continuous, gap-free stream.
 *   4. Close codes the server uses on purpose are final (see backend/app/api/websocket.py):
 *      1000 job finished, 1008 policy violation, 4404 unknown job: do not retry.
 *      1013 means "you lagged": retry immediately, history fills the gap.
 */

const TERMINAL_TYPES = new Set(["job.succeeded", "job.failed", "job.cancelled"]);
const FINAL_CLOSE_CODES = new Set([1000, 1008, 4404]);
const CLOSE_LAGGED = 1013;
const MAX_RETRIES = 8;
const MAX_BACKOFF_MS = 10_000;

/**
 * @param {string} jobId
 * @param {{ onEvent: (event: object) => void, onConnectionChange: (state: string) => void }} handlers
 * @returns {{ close: () => void }}
 */
export function openJobStream(jobId, { onEvent, onConnectionChange }) {
  let socket = null;
  let lastSeq = 0;
  let retries = 0;
  let finished = false; // terminal event received
  let stopped = false; // caller closed the stream (e.g. a new deployment started)
  let retryTimer = null;

  const connect = () => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${scheme}://${window.location.host}/ws/jobs/${encodeURIComponent(jobId)}?after_seq=${lastSeq}`;
    onConnectionChange(retries === 0 ? "connecting" : "reconnecting");
    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
      retries = 0;
      onConnectionChange("live");
    });

    socket.addEventListener("message", (message) => {
      // Server frames are still untrusted input: parse defensively and validate the
      // fields we rely on before handing the event to the UI.
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      if (typeof event?.seq !== "number" || typeof event.type !== "string") return;
      if (event.seq <= lastSeq) return; // duplicate from a replay
      lastSeq = event.seq;
      if (TERMINAL_TYPES.has(event.type)) finished = true;
      onEvent(event);
    });

    socket.addEventListener("close", (closeEvent) => {
      if (stopped) return;
      if (finished || FINAL_CLOSE_CODES.has(closeEvent.code)) {
        onConnectionChange(finished ? "closed" : "rejected");
        return;
      }
      if (retries >= MAX_RETRIES) {
        onConnectionChange("failed");
        return;
      }
      const backoff = closeEvent.code === CLOSE_LAGGED ? 0 : Math.min(500 * 2 ** retries, MAX_BACKOFF_MS);
      const jitter = Math.random() * 250; // spreads reconnects after a server restart
      retries += 1;
      onConnectionChange("reconnecting");
      retryTimer = window.setTimeout(connect, backoff + jitter);
    });
  };

  connect();

  return {
    close() {
      stopped = true;
      window.clearTimeout(retryTimer);
      socket?.close(1000, "client closed");
    },
  };
}
