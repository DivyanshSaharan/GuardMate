# GuardMate logo

The shield represents guarded delivery decisions. The speech bubble and three
voice bars represent the assistant's conversation with a courier. The mark uses
the interface's forest green (`#174c3c`) and pale cream (`#e9f2ba`).

The editable master is `frontend/public/guardmate-logo.svg`. Both the public
website and the local desktop dashboard use it through `BrandMark`. The PNG
exports support previews and browser icons. The multi-resolution Windows ICO is
embedded into the desktop executable and installer; shortcuts and the uninstall
entry inherit those icons. The icon has transparent corners.

To regenerate the committed PNG and ICO exports, run with Node.js and the
optional development package `sharp` available:

```text
node packaging/create_brand_assets.mjs
```

Alternatively pass the absolute path of an existing `sharp` package directory as
the first argument. Regeneration is not required for normal frontend or Windows
builds, because the exports are committed.

The design began with a built-in image-generation concept and was redrawn as a
flat SVG for exact colors and small-size rendering. The design prompt was:
"A simple shield and speech bubble with three rounded voice bars, forest green
and pale cream, no text, flat geometry, transparent rounded-square corners."

A new Windows release is required to deliver embedded executable icons to
existing installations. Never replace an already published release's files.
