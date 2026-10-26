// Each mounted page owns its requests; callbacks must check active() after awaits.
export function createPageSession(anchor) {
 let disposed = false, current = null;
 const timers = new Set();
 const active = () => !disposed && anchor.isConnected;
 return {
  active,
  get busy() { return current !== null; },
  cancel() { current?.abort(); },
  dispose() {
   disposed = true;
   current?.abort();
   for (const timer of timers) clearInterval(timer);
   timers.clear();
  },
  async run(task) {
   if (!active() || current) return;
   const request = new AbortController();
   current = request;
   try { return await task(request.signal); }
   finally { if (current === request) current = null; }
  },
  repeat(task, milliseconds) {
   if (!active()) return;
   let pending = false;
   const timer = setInterval(async () => {
    if (!active() || pending) return;
    pending = true;
    try { await task(); } catch { /* Next tick retries a read-only status request. */ }
    finally { pending = false; }
   }, milliseconds);
   timers.add(timer);
  },
 };
}
