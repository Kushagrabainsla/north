// One request in flight, and only the newest value waiting: what a slider dragged across its
// range, or a microphone sending what it hears, needs from the network.

/**
 * A sender with one request in flight: while one is on its way, a newer value replaces any
 * waiting one, and is sent when it returns. A slider dragged across its range sends its last
 * position, not a queue of every position it passed through. A waiting value that *keeps* says
 * must arrive (a beat) is replaced only by another such value, never by the ordinary ones after it.
 */
export function latestWins<T, R>(
  send: (value: T) => Promise<R>, settled: (result: R) => void, failed: (error: unknown) => void,
  keeps: (value: T) => boolean = () => false,
) {
  let busy = false;
  let waiting: { value: T } | null = null;
  const run = async (value: T) => {
    busy = true;
    try {
      settled(await send(value));
    } catch (error) {
      failed(error);
    } finally {
      busy = false;
      const next = waiting;
      waiting = null;
      if (next) void run(next.value);
    }
  };
  return (value: T) => {
    if (!busy) void run(value);
    else if (!waiting || !keeps(waiting.value) || keeps(value)) waiting = { value };
  };
}
