const test = require('node:test');
const assert = require('node:assert/strict');
const { bounds, clamp } = require('../../workbench/static/sidebar.js');
test('sidebar leaves 480px of content at tablet widths', () => {
  for (const viewport of [768, 900, 1280, 1920]) {
    for (const wanted of [0, 184, 224, 360, 2000]) {
      const actual = clamp(wanted, viewport);
      assert.ok(actual >= 184 && actual <= 360);
      assert.ok(viewport - actual >= 480);
    }
  }
});
test('narrow desktop bounds are ordered; invalid preferences have a safe default', () => {
  assert.deepEqual(bounds(601), { min:184, max:184 });
  assert.equal(clamp(NaN,1280),224);
  assert.equal(clamp(Infinity,1280),224);
  assert.equal(clamp(360,768),288);
  assert.equal(clamp(224,601),184);
});
