# vendored third-party code

`pdf.min.mjs` and `pdf.worker.min.mjs` are the build of
[pdf.js](https://mozilla.github.io/pdf.js/) **4.10.38**, taken unmodified from
`pdfjs-dist` on jsDelivr.

They are bundled instead of loaded from a CDN because HardlyReading is a
local-first app: it should keep working with no internet beyond archive.org
itself, and a CDN would also mean the reader silently breaks offline.

The PDF reader needs pdf.js for one specific reason: the browser's built-in
viewer is a plugin, so page script cannot read which page is on screen, and
without that there is no reading position to save and no library history.

pdf.js is licensed under the Apache License 2.0, which is compatible with this
project's GPL-3.0 licence. The licence notice is preserved in the header of each
file. Sources: <https://github.com/mozilla/pdf.js>
