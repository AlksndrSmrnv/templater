/*
 * Unit tests for the pure helpers in app/static/js/tls_certs.js —
 * validateMaterial (shape/normalization of a preset connection) and
 * arrayBufferToBase64 (JKS file encoding). No browser/DOM/sessionStorage needed:
 * we shim `window` and load the script, which also exports for Node.
 *
 * Run: `node --test tests/js/`  (Node 18+; no dependencies).
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');

// Browser IIFE that attaches to `window`; shim it before requiring. storage()
// tolerates the absent sessionStorage, so the pure helpers load fine.
global.window = {};
const { validateMaterial, arrayBufferToBase64 } = require('../../app/static/js/tls_certs.js');

test('validateMaterial: PEM needs both cert and key', () => {
    assert.strictEqual(validateMaterial({ kind: 'pem', cert: 'c' }), null);
    assert.strictEqual(validateMaterial({ kind: 'pem', key: 'k' }), null);
    assert.deepStrictEqual(
        validateMaterial({ kind: 'pem', cert: 'c', key: 'k' }),
        { kind: 'pem', cert: 'c', key: 'k', password: '', verify: false }
    );
});

test('validateMaterial: trims and preserves password/verify', () => {
    assert.deepStrictEqual(
        validateMaterial({ kind: 'PEM', cert: '  c  ', key: '\nk\n', password: 'p', verify: true }),
        { kind: 'pem', cert: 'c', key: 'k', password: 'p', verify: true }
    );
});

test('validateMaterial: JKS needs the blob', () => {
    assert.strictEqual(validateMaterial({ kind: 'jks' }), null);
    assert.deepStrictEqual(
        validateMaterial({ kind: 'jks', jks: 'YWJj' }),
        { kind: 'jks', jks: 'YWJj', password: '', verify: false }
    );
});

test('validateMaterial: rejects junk', () => {
    assert.strictEqual(validateMaterial(null), null);
    assert.strictEqual(validateMaterial('nope'), null);
    assert.strictEqual(validateMaterial({ kind: 'bogus', cert: 'c', key: 'k' }), null);
});

test('arrayBufferToBase64: encodes bytes', () => {
    assert.strictEqual(arrayBufferToBase64(new Uint8Array([97, 98, 99]).buffer), 'YWJj');
    assert.strictEqual(arrayBufferToBase64(new Uint8Array([]).buffer), '');
});

test('arrayBufferToBase64: handles > 32KB without overflow', () => {
    const big = new Uint8Array(70000).fill(65); // 'A'
    const out = arrayBufferToBase64(big.buffer);
    // Round-trips back to the same length via Node's Buffer.
    assert.strictEqual(Buffer.from(out, 'base64').length, 70000);
});
