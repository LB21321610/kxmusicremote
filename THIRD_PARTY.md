# LDDC dependency and licensing

Upstream: https://github.com/chenmozhijin/LDDC

Reviewed/pinned main HEAD: `84631e8cd011fcc3f71ca0ae017e2c9758958ffc`,
package version v0.9.2. Checked against remote HEAD during implementation.

LDDC source headers specify **GPL-3.0-only**, copyright 2024–2025
沉默の金 <cmzj@cmzj.org>. Its complete unchanged license is included in
`licenses/LDDC-GPL-3.0.txt`. The installed package and pinned public source
contain the complete corresponding upstream source.

`lddc_worker.py`, `lyrics_matching.py` and `lyrics_multisource.py` are new
GPL-3.0-only adapter components. They reuse upstream provider classes
`QMAPI`, `NEAPI`, `KGAPI`, `LrclibAPI`, lyrics models and matching algorithms;
they do not copy or modify installed upstream files. The project's original
MIT attribution remains. Do not describe a redistributed combined LDDC-enabled
package as solely MIT: retain notices/license text and supply corresponding
source and modifications as required by GPLv3. Running a helper process is an
execution/dependency boundary, not a claim of exemption from GPL obligations.

The README and `pyproject.toml` confirm Python >=3.10, QQ/NetEase/Kugou/LRCLIB
support and dependencies: PySide6-Essentials >=6.8, httpx[brotli,http2], mutagen,
diskcache, charset-normalizer, pyaes, psutil (Cocoa only on macOS). Only Windows
runs the helper here. No `QApplication`, Qt GUI entry point, translation provider
or LDDC desktop service is started. QtCore is an upstream module dependency.

OpenCC/py3langid provide local conversion/language identification. Foreign lyrics
are translated by the user's local Ollama `hy-mt2-translate` model; no model weights
are bundled, and DeepL credentials are no longer used. LDDC's Bing/Google/OpenAI
translation modules are not invoked. Source
lyrics remain third-party provider content; LDDC's software license does not
grant ownership of song lyrics.
