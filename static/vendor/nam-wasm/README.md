# NAM wasm engine

`index.js`, `nam-worklet.js` and `nam-engine.wasm` are the engine files from
[neural-amp-modeler-wasm](https://github.com/tone-3000/neural-amp-modeler-wasm) 2.0.1
(npm package `neural-amp-modeler-wasm`, `dist/engine/`), by TONE3000, under the ISC licence.
They are NeuralAmpModelerCore v0.5.4 compiled to WebAssembly, running in an AudioWorklet.

Copied unchanged, except that the source map comment was removed from `index.js` (the map isn't shipped).
To update: `npm pack neural-amp-modeler-wasm@<version>` and copy `package/dist/engine/` over these files.
