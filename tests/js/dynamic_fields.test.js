/*
 * Unit tests for the pure client-side dynamic-field logic in
 * app/static/js/status_code.js — the "heart" of the dynamic-parameters feature
 * (pattern generation + envelope-token substitution). No browser/DOM needed:
 * we shim `window` and load the script, which also exports for Node.
 *
 * Run: `node --test tests/js/`  (Node 18+; no dependencies).
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');

// The script is a browser IIFE that attaches to `window`; give it a shim before
// requiring. `window.crypto` stays undefined so uuidv4 uses the Math.random
// fallback (still a valid v4 uuid), which is fine for these assertions.
global.window = {};
const { generateDynamicValue, dynamicFields } = require('../../app/static/js/status_code.js');

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

test('generate: {uuid} and {uuid_upper}', () => {
    assert.match(generateDynamicValue('{uuid}'), UUID_RE);
    const up = generateDynamicValue('{uuid_upper}');
    assert.match(up, /^[0-9A-F-]+$/);
    assert.match(up.toLowerCase(), UUID_RE);
});

test('generate: {rand:N} and {hex:N} honour the count', () => {
    assert.match(generateDynamicValue('{rand:6}'), /^\d{6}$/);
    assert.match(generateDynamicValue('{hex:8}'), /^[0-9a-f]{8}$/);
});

test('generate: invalid/missing count leaves the token verbatim', () => {
    // Regression for the silent-empty-string issue: a typo must be visible.
    assert.strictEqual(generateDynamicValue('{rand:abc}'), '{rand:abc}');
    assert.strictEqual(generateDynamicValue('{rand:0}'), '{rand:0}');
    assert.strictEqual(generateDynamicValue('{rand}'), '{rand}');
    assert.strictEqual(generateDynamicValue('{hex}'), '{hex}');
});

test('generate: count over the cap is rejected (no runaway loop)', () => {
    // Guards against {rand:999999999} freezing the tab; over-cap -> verbatim.
    assert.strictEqual(generateDynamicValue('{rand:65}'), '{rand:65}');
    assert.strictEqual(generateDynamicValue('{hex:1000}'), '{hex:1000}');
    assert.match(generateDynamicValue('{rand:64}'), /^\d{64}$/); // boundary ok
});

test('generate: {date:FORMAT} formats the given instant (local zone)', () => {
    const now = new Date(2024, 0, 5, 9, 3, 7, 42); // 2024-01-05 09:03:07.042 local
    assert.strictEqual(
        generateDynamicValue('{date:YYYY-MM-DDTHH:mm:ss.SSS}', now),
        '2024-01-05T09:03:07.042',
    );
    assert.strictEqual(generateDynamicValue('{date:YY/MM/DD}', now), '24/01/05');
});

test('generate: bare {date} and unknown tokens are left verbatim', () => {
    assert.strictEqual(generateDynamicValue('{date}'), '{date}');
    assert.strictEqual(generateDynamicValue('{foo}'), '{foo}');
    assert.strictEqual(generateDynamicValue('X-{unknown}-Y'), 'X-{unknown}-Y');
});

test('generate: literal text around tokens is preserved', () => {
    const out = generateDynamicValue('PRE-{rand:3}-POST');
    assert.match(out, /^PRE-\d{3}-POST$/);
});

test('generate: {seq} increments across calls', () => {
    const a = Number(generateDynamicValue('{seq}'));
    const b = Number(generateDynamicValue('{seq}'));
    assert.strictEqual(b, a + 1);
});

test('generate: {timestamp} / {timestamp_ms} reflect the instant', () => {
    const now = new Date(1700000000000);
    assert.strictEqual(generateDynamicValue('{timestamp}', now), '1700000000');
    assert.strictEqual(generateDynamicValue('{timestamp_ms}', now), '1700000000000');
});

test('buildContext: fields use patterns, defaults fill the gaps', () => {
    const ctx = dynamicFields.buildContext({ rqUID: 'RQ-{rand:4}' }, null);
    assert.match(ctx.rqUID, /^RQ-\d{4}$/);
    assert.match(ctx.operUID, UUID_RE); // default {uuid}
    assert.ok(ctx.rqTm && ctx.channelDateTime);
});

test('buildContext: sharedOperuid pins operUID, else it is fresh', () => {
    const shared = dynamicFields.buildContext({}, 'SHARED-1');
    assert.strictEqual(shared.operUID, 'SHARED-1');
    const a = dynamicFields.buildContext({}, null);
    const b = dynamicFields.buildContext({}, null);
    assert.notStrictEqual(a.operUID, b.operUID);
    // Empty string is treated as "no shared value" -> fresh uuid.
    assert.match(dynamicFields.buildContext({}, '').operUID, UUID_RE);
});

test('substitute: replaces dynamic tokens case-insensitively', () => {
    const ctx = { rqUID: 'R1', operUID: 'O1', rqTm: 'T1', channelDateTime: 'C1' };
    assert.strictEqual(dynamicFields.substitute('{{rqUID}}', ctx), 'R1');
    assert.strictEqual(dynamicFields.substitute('{{ RQUID }}', ctx), 'R1');
    assert.strictEqual(dynamicFields.substitute('a {{operUID}} b', ctx), 'a O1 b');
});

test('substitute: leaves references and unknown tokens untouched', () => {
    const ctx = { rqUID: 'R1' };
    assert.strictEqual(dynamicFields.substitute('{{ $1.transferId }}', ctx), '{{ $1.transferId }}');
    assert.strictEqual(dynamicFields.substitute('{{other}}', ctx), '{{other}}');
});

test('resolveHeaders: drops disabled, keeps slim {key,value}, substitutes', () => {
    const ctx = { rqUID: 'R1' };
    const out = dynamicFields.resolveHeaders([
        { key: 'RqUID', value: '{{rqUID}}', mode: 'dynamic', original: 'x', disabled: false },
        { key: 'X-Off', value: '{{rqUID}}', disabled: true },
        { key: 'Content-Type', value: 'application/json' },
    ], ctx);
    assert.deepStrictEqual(out, [
        { key: 'RqUID', value: 'R1' },
        { key: 'Content-Type', value: 'application/json' },
    ]);
});

// ---- resolveBody: the dual-regex core (references + dynamic tokens) ----

test('resolveBody: substitutes a dynamic token inside a JSON string', () => {
    const ctx = { rqUID: 'abc-123' };
    const out = dynamicFields.resolveBody('{"id": "{{rqUID}}"}', { isJson: true, ctx: ctx });
    assert.strictEqual(out.resolved, '{"id": "abc-123"}');
    assert.deepStrictEqual(out.unresolved, []);
    assert.strictEqual(out.refs, 0);
    assert.ok(JSON.parse(out.resolved).id === 'abc-123'); // still valid JSON
});

test('resolveBody: a value with quotes stays valid JSON (encodeRef in-string)', () => {
    const ctx = { operUID: 'a"b\\c' };
    const out = dynamicFields.resolveBody('{"op": "{{operUID}}"}', { isJson: true, ctx: ctx });
    assert.strictEqual(JSON.parse(out.resolved).op, 'a"b\\c');
});

test('resolveBody: dynamic token in a JSON value position is quoted', () => {
    // Token not inside a string -> encoded as a JSON string literal.
    const ctx = { rqTm: '2024-01-05T09:03:07' };
    const out = dynamicFields.resolveBody('{"t": {{rqTm}}}', { isJson: true, ctx: ctx });
    assert.strictEqual(out.resolved, '{"t": "2024-01-05T09:03:07"}');
    assert.strictEqual(JSON.parse(out.resolved).t, '2024-01-05T09:03:07');
});

test('resolveBody: resolves $N.path references via resolveRef', () => {
    const out = dynamicFields.resolveBody('{"from": "{{ $1.transferId }}"}', {
        isJson: true,
        resolveRef: (step, path) => (step === 1 && path === 'transferId') ? 'TRF-7' : undefined,
    });
    assert.strictEqual(out.resolved, '{"from": "TRF-7"}');
    assert.strictEqual(out.refs, 1);
    assert.deepStrictEqual(out.unresolved, []);
});

test('resolveBody: unresolved reference is reported and left verbatim', () => {
    const out = dynamicFields.resolveBody('{"x": "{{ $1.missing }}"}', {
        isJson: true,
        resolveRef: () => undefined,
    });
    assert.strictEqual(out.resolved, '{"x": "{{ $1.missing }}"}');
    assert.deepStrictEqual(out.unresolved, ['{{ $1.missing }}']);
    assert.match(out.html, /reference unresolved/);
});

test('resolveBody: unknown bareword tokens are left untouched', () => {
    const out = dynamicFields.resolveBody('{"x": "{{notDynamic}}"}', { isJson: true, ctx: {} });
    assert.strictEqual(out.resolved, '{"x": "{{notDynamic}}"}');
    assert.strictEqual(out.refs, 0);
});

test('resolveBody: non-JSON body substitutes raw (no quoting)', () => {
    const ctx = { rqUID: 'R1' };
    const out = dynamicFields.resolveBody('id={{rqUID}}', { isJson: false, ctx: ctx });
    assert.strictEqual(out.resolved, 'id=R1');
});
