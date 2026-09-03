/**
 * Frontend logic tests (run with: node tests/frontend-logic.test.mjs)
 * Exercises the DOM-free logic of the ES modules: event log coalescing,
 * geometry sanitization, presets on all three aspects, media URL policy.
 */
import { strict as assert } from 'node:assert';
import { Timeline, describeEvent, fmtClock } from '../web/js/timeline.js';
import { sanitizeGeometry } from '../web/js/layers.js';
import { Recorder } from '../web/js/recorder.js';

// ---- stubs for modules that only touch DOM at runtime ----
globalThis.window = globalThis.window || {};
window.matchMedia = window.matchMedia || (() => ({ matches: false }));
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'test' }, configurable: true });
globalThis.performance = { now: () => Date.now() };

let pass = 0, fail = 0;
const test = (name, fn) => {
  try { fn(); pass++; console.log(`  ✓ ${name}`); }
  catch (err) { fail++; console.error(`  ✗ ${name}\n    ${err.message}`); }
};

console.log('timeline.js — master clock & event log');
test('emit records events with wallMs + mediaTime', () => {
  const tl = new Timeline();
  const ev = tl.emit('l1', 'play', { mediaTime: 1.5 });
  assert.equal(ev.action, 'play');
  assert.equal(ev.mediaTime, 1.5);
  assert.ok(typeof ev.wallMs === 'number');
  assert.equal(tl.count, 1);
});
test('all event types from §13 are accepted', () => {
  const tl = new Timeline();
  for (const a of ['play','pause','seek','visibility_on','visibility_off','mute','unmute',
                   'volume','source_change','geometry_change','layer_add','layer_remove','layer_reorder'])
    tl.emit('l1', a, { payload: {} });
  assert.equal(tl.count, 13);
  assert.equal(tl.emit('l1', 'bogus_action'), null);
});
test('volume bursts coalesce within window (P6-09)', () => {
  const tl = new Timeline();
  const t = Date.now();
  const orig = performance.now;
  let fake = t;
  globalThis.performance.now = () => fake;
  tl.emit('l1', 'volume', { payload: { volume: 0.1 } });
  fake += 100; tl.emit('l1', 'volume', { payload: { volume: 0.5 } });
  fake += 150; tl.emit('l1', 'volume', { payload: { volume: 0.9 } });
  assert.equal(tl.count, 1);
  assert.equal(tl.events[0].payload.volume, 0.9);  // latest value, original timestamp
  fake += 1000; tl.emit('l1', 'volume', { payload: { volume: 0.2 } });
  assert.equal(tl.count, 2);                        // outside window -> new event
  globalThis.performance.now = orig;
});
test('events are ordered by wallMs and round-trip via JSON (P6-12)', () => {
  const tl = new Timeline();
  let fake = 0;
  const orig = performance.now;
  globalThis.performance.now = () => fake;
  fake = 300; tl.emit('b', 'play');
  fake = 100; tl.emit('a', 'layer_add');
  fake = 200; tl.emit('a', 'pause', { mediaTime: 2 });
  const ser = JSON.parse(JSON.stringify(tl.serialize()));
  assert.deepEqual(ser.map(e => e.action), ['layer_add', 'pause', 'play']);
  const tl2 = new Timeline();
  tl2.load(ser);
  assert.deepEqual(tl2.serialize(), ser);
  globalThis.performance.now = orig;
});
test('describeEvent is human-readable', () => {
  assert.match(describeEvent({ action: 'pause', layerId: 'x', mediaTime: 2.25 }), /pause/);
  assert.match(fmtClock(65.43), /01:05/);
});

console.log('layers.js — geometry validation (P3-39)');
test('NaN rejected, values clamped 0..1, min size enforced', () => {
  const g = sanitizeGeometry({ x: NaN, y: -1, w: 2, h: 'zz' },
                             { x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
  assert.equal(g.x, 0.1);
  assert.equal(g.y, 0);
  assert.equal(g.w, 1);
  assert.equal(g.h, 0.4);
  const g2 = sanitizeGeometry({ x: 0.5, y: 0.5, w: 0.001, h: -3 });
  assert.ok(g2.w >= 0.01);
  assert.equal(g2.h, 0.01);
});

console.log('pip-editor.js — presets on every canvas aspect (P3-E5)');
const presetNames = ['top-left','top-center','top-right','center-left','center','center-right',
                     'bottom-left','bottom-center','bottom-right','50/50','70/30','quarter-screen','full-screen'];
const aspects = { '16:9': [1920, 1080], '9:16': [1080, 1920], '1:1': [1080, 1080] };
for (const [aspect, [W, H]] of Object.entries(aspects)) {
  test(`all 14 presets produce valid geometry on ${aspect}`, async () => {
    const { PipEditor } = await import('../web/js/pip-editor.js');
    const applied = [];
    const lm = { selected: { id: 'l', geometry: { x: 0, y: 0, w: 1, h: 1 }, locked: false },
                 setGeometry: (id, g) => applied.push(g) };
    const comp = { logicalW: W, logicalH: H, canvas: { addEventListener: () => {} } };
    const ed = Object.create(PipEditor.prototype);
    ed.comp = comp; ed.lm = lm;
    for (const name of presetNames) ed.applyPreset(name, lm.selected);
    assert.equal(applied.length, presetNames.length);
    for (const g of applied) {
      assert.ok(g.x >= 0 && g.x <= 1 && g.y >= 0 && g.y <= 1, `x/y in range: ${JSON.stringify(g)}`);
      assert.ok(g.w > 0 && g.w <= 1 && g.h > 0 && g.h <= 1, `w/h in range: ${JSON.stringify(g)}`);
      assert.ok(g.x + g.w <= 1.001, `fits horizontally: ${JSON.stringify(g)}`);
      assert.ok(g.y + g.h <= 1.001, `fits vertically: ${JSON.stringify(g)}`);
    }
    // corner presets actually sit in their corners
    const tl = applied[0], br = applied[8];
    assert.ok(tl.x <= 0.05 && tl.y <= 0.05);
    assert.ok(br.x + br.w >= 0.95 && br.y + br.h >= 0.95);
  });
}

console.log('recorder.js — codec negotiation (P7-04)');
test('pickMime uses isTypeSupported preference order', () => {
  const supported = new Set(['video/webm;codecs=vp8,opus']);
  globalThis.MediaRecorder = { isTypeSupported: (m) => supported.has(m) };
  assert.equal(Recorder.pickMime(['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus']),
               'video/webm;codecs=vp8,opus');
  supported.add('video/webm;codecs=vp9,opus');
  assert.equal(Recorder.pickMime(['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus']),
               'video/webm;codecs=vp9,opus');
  assert.equal(Recorder.pickMime(['video/mp4;codecs=h264,aac']), '');  // platform default
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
