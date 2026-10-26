import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createPageSession} from '../../soulbot/web/static/js/page-session.js';

function deferred() {
 let resolve;
 const promise = new Promise(done => {resolve = done;});
 return {promise, resolve};
}

test('duplicate submit during config saving does not issue a second generation', async () => {
 const page = createPageSession({isConnected:true});
 const saved = deferred();
 let calls = 0;
 const submit = () => page.run(async signal => {
  await saved.promise;
  if (page.active() && !signal.aborted) calls++;
 });
 const first = submit();
 await submit();
 assert.equal(page.busy, true);
 saved.resolve();
 await first;
 assert.equal(calls, 1);
 assert.equal(page.busy, false);
 page.dispose();
});

test('old page finalization cannot clear or cancel the new page request', async () => {
 const old = createPageSession({isConnected:true});
 const oldReply = deferred(), newReply = deferred();
 let oldSignal, newSignal;
 const oldRequest = old.run(async signal => {oldSignal = signal;await oldReply.promise;});
 old.dispose();
 assert.equal(oldSignal.aborted, true);
 const next = createPageSession({isConnected:true});
 const nextRequest = next.run(async signal => {newSignal = signal;await newReply.promise;});
 oldReply.resolve();
 await oldRequest;
 assert.equal(next.busy, true);
 assert.equal(newSignal.aborted, false);
 next.cancel();
 assert.equal(newSignal.aborted, true);
 newReply.resolve();
 await nextRequest;
 assert.equal(next.busy, false);
 next.dispose();
});

test('leaving while config saves prevents any later generation on the departed page', async () => {
 const anchor = {isConnected:true};
 const page = createPageSession(anchor);
 const saved = deferred();
 let generated = false;
 const request = page.run(async signal => {
  await saved.promise;
  if (page.active() && !signal.aborted) generated = true;
 });
 anchor.isConnected = false;
 page.dispose();
 saved.resolve();
 await request;
 assert.equal(generated, false);
 assert.equal(page.active(), false);
});
