// sk_pqc web backend bootstrap.
//
// Exposes the AUDITED @noble/post-quantum ml_kem768 to the Dart web backend as
// `globalThis.skPqc`. Bundle this (e.g. via esbuild/rollup) and load it BEFORE
// your Flutter web app so the symbol exists when `createMlKem768Backend()` runs.
//
// Delivery options (pick one, document it in your app):
//   1. Bundle: `esbuild sk_pqc_noble_bootstrap.js --bundle --format=esm \
//        --outfile=web/sk_pqc_noble.js` then add
//        `<script type="module" src="sk_pqc_noble.js"></script>` to web/index.html.
//   2. CDN/import-map: map "@noble/post-quantum" to an esm.sh / jsdelivr URL.
//   3. Asset: vendor the bundle under web/ and reference it from index.html.
//
// Pin the version you audited. As of this package: @noble/post-quantum 0.6.x.

import { ml_kem768 } from '@noble/post-quantum/ml-kem.js';

globalThis.skPqc = {
  keygen() {
    const k = ml_kem768.keygen();
    return { publicKey: k.publicKey, secretKey: k.secretKey };
  },
  encapsulate(publicKey) {
    const e = ml_kem768.encapsulate(publicKey);
    return { cipherText: e.cipherText, sharedSecret: e.sharedSecret };
  },
  decapsulate(ciphertext, secretKey) {
    return ml_kem768.decapsulate(ciphertext, secretKey);
  },
};
