# Third-party notices

ZenWhisper is licensed under the MIT License ([LICENSE](LICENSE)). The
components below are distributed under their own terms.

## haas_cpu inference code (in `src/asr/haas.py`)

Parts of `src/asr/haas.py` (log-mel frontend, TDT greedy decoding, SentencePiece
byte-fallback detokenizer and quiet-point splitting) are adapted from the
`haas_cpu` package in
[kizuna-intelligence/Haas-ja-v1.0-OneCompression-ONNX](https://huggingface.co/kizuna-intelligence/Haas-ja-v1.0-OneCompression-ONNX/tree/e46a4b107ffba60336eb3de9745da6f628cfae35)
at commit `e46a4b107ffba60336eb3de9745da6f628cfae35`, licensed under the
Apache License, Version 2.0 ([LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)).

ZenWhisper modified that code: it is integrated with the ZenWhisper model
manifest, reads files explicitly as UTF-8, does not download or resample inside
inference, and returns empty text for empty or silent input.

The model weights are not part of this repository. They are downloaded on first
use from the pinned revision above and verified by SHA-256, together with the
upstream `LICENSE` and `NOTICE`. The upstream NOTICE reads, verbatim:

```text
Haas-ja-v1.0 OneCompression ONNX (kizuna-intelligence/Haas-ja-v1.0-OneCompression-ONNX)

Original model: third-intelligence/Haas-ja-v1.0
Model publisher: Third Intelligence
Source: https://huggingface.co/third-intelligence/Haas-ja-v1.0
Revision: 660728d39019e5e065046154beca44ac9a66a540
Original model license: Apache License, Version 2.0 (see LICENSE).

This distribution modifies the model for ONNX Runtime CPU inference,
includes fp32 and quantized encoders, and supplies Python inference code.
Release-copy changes: local/snapshot model lookup, default format and
thread count, and automatic input resampling. Build/evaluation code and
int4-rtn are not distributed.

OneCompression was used to quantize the int4-onecomp encoder (GPTQ + QEP).
Project: https://github.com/FujitsuResearch/OneCompression
Revision: e9a803ada8ffe932a99ca0197f273c4ae4b307f6
OneCompression is licensed under MIT; its code is not bundled here.
The original license notice follows:

MIT License

Copyright 2025-2026 Fujitsu Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Silero VAD model (`assets/vad/`)

See [assets/vad/LICENSE.silero-vad.txt](assets/vad/LICENSE.silero-vad.txt).
