/*
 * window.tlsCerts — session-only store for a preset's TLS client-cert
 * «connection» (certificate + key, or a JKS keystore, + password).
 *
 * Secrets are kept ONLY in sessionStorage (cleared when the tab/browser closes)
 * and are never sent to the preset CRUD or the database. At send time the panels
 * read the material for a template's preset and hand it to the backend for one
 * real mTLS request (see app/services/rest_sender.py) — the browser itself
 * cannot do client-cert TLS via fetch.
 *
 * Stored shape (per preset id):
 *   { kind:"pem", cert:"<PEM>", key:"<PEM>", password:"", verify:false }
 *   { kind:"jks", jks:"<base64>", password:"", verify:false }
 *
 * The pure helpers (validateMaterial / arrayBufferToBase64) are exported for
 * Node tests; the storage methods are no-ops when sessionStorage is unavailable.
 */
(function () {
    'use strict';

    var PREFIX = 'tm:tls:';

    function storage() {
        try {
            return window.sessionStorage;
        } catch (e) {
            return null;
        }
    }

    // Base64-encode raw bytes (used for JKS files read as ArrayBuffer). Chunked
    // so large keystores don't blow the argument limit of String.fromCharCode.
    function arrayBufferToBase64(buffer) {
        var bytes = new Uint8Array(buffer);
        var binary = '';
        var chunk = 0x8000;
        for (var i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(
                null, bytes.subarray(i, Math.min(i + chunk, bytes.length))
            );
        }
        return btoa(binary);
    }

    // Normalize + validate an editor-built material object. Returns a clean copy
    // ready to store, or null when required parts are missing (→ treat as no
    // connection). Passwords/verify are optional; PEM needs cert+key, JKS the blob.
    function validateMaterial(material) {
        if (!material || typeof material !== 'object') return null;
        var kind = String(material.kind || '').trim().toLowerCase();
        var password = material.password == null ? '' : String(material.password);
        var verify = !!material.verify;
        if (kind === 'pem') {
            var cert = String(material.cert || '').trim();
            var key = String(material.key || '').trim();
            if (!cert || !key) return null;
            return { kind: 'pem', cert: cert, key: key, password: password, verify: verify };
        }
        if (kind === 'jks') {
            var jksB64 = String(material.jks || '').trim();
            if (!jksB64) return null;
            return { kind: 'jks', jks: jksB64, password: password, verify: verify };
        }
        return null;
    }

    window.tlsCerts = {
        arrayBufferToBase64: arrayBufferToBase64,
        validateMaterial: validateMaterial,

        // Store material for a preset. Returns true on success, false when the
        // material is incomplete or storage is unavailable.
        save: function (presetId, material) {
            if (!presetId) return false;
            var clean = validateMaterial(material);
            var s = storage();
            if (!clean || !s) return false;
            try {
                s.setItem(PREFIX + presetId, JSON.stringify(clean));
                return true;
            } catch (e) {
                return false;
            }
        },

        // Retrieve material for a preset, or null when none is stored.
        get: function (presetId) {
            if (!presetId) return null;
            var s = storage();
            if (!s) return null;
            try {
                var raw = s.getItem(PREFIX + presetId);
                return raw ? validateMaterial(JSON.parse(raw)) : null;
            } catch (e) {
                return null;
            }
        },

        has: function (presetId) {
            return this.get(presetId) !== null;
        },

        remove: function (presetId) {
            if (!presetId) return;
            var s = storage();
            if (!s) return;
            try {
                s.removeItem(PREFIX + presetId);
            } catch (e) { /* ignore */ }
        },
    };

    // Node-only export so the pure helpers can be unit-tested without a browser
    // (see tests/js/). No-op in the browser.
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            validateMaterial: validateMaterial,
            arrayBufferToBase64: arrayBufferToBase64,
        };
    }
})();
