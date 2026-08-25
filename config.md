These values are the defaults the export dialog starts with; the dialog writes
your last choices back here, so editing them by hand is optional.

- `output_dir` — destination folder pre-filled in the dialog.
- `order` — card order in the export: `deck`, `created`, `card_id` or `due`.
- `single_file` — write one self-contained `.html` with media embedded as
  `data:` URIs instead of a folder with a `media/` subdirectory.
- `embed_mathjax` — bundle Anki's MathJax build (~1.7 MB) when a card uses
  `\(…\)` or `\[…\]`. Turn off if the export is only ever read online.
- `download_remote_media` — fetch `http(s)` images referenced by cards so the
  export works offline, through Anki's own HTTP client. Off by default: it is
  the only part of the add-on that talks to anything but your own machine, and
  in a shared deck the addresses come from its author.
- `apply_gui_hooks` — also run `gui_hooks.card_will_show`, the hook the reviewer
  uses, so other add-ons can post-process the exported cards. Off by default:
  those hooks are written for the reviewer and may inject controls that only
  work inside Anki.
- `view_mode` — which card sides the export starts with: `auto` (the answer,
  plus the question for cards whose answer does not already repeat it), `qa`,
  `a` or `q`.
- `interactive` — let the reader lift a cloze deletion or an occlusion mask by
  clicking it. On by default: Anki's rendering already carries the answer, so
  nothing has to be exported twice for it.
- `study` — start in study mode, where only the front of each card is on screen
  and the answer waits for a click or the space bar.
- `show_meta`, `show_special`, `show_fields`, `dark` — whether the header row,
  the details strip, the note fields and night mode start switched on. The
  three blocks default to off: they are reference material, not part of the
  card. All of them can also be flipped inside the export.
- `excluded_fields`, `excluded_special` — names left unchecked in the dialog's
  include lists.
- `last_search` — the extra Anki search from the last export.
