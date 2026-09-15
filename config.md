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
- `include_hidden` — also export suspended and buried cards. Off by default:
  those are cards you have set aside, and an Anki search would return them
  all the same.

## `hypnagog`

The presentation's settings, all of them also set from its page:
`prime_ms` (the flash), `show_ms` (each card), `consolidate_ms` (the fade),
`round_pause_ms`, `visual_mode` (2 Polybius, 1 clean), `max_cards`,
`card_source` (`due`, `new`, `all`, `leeches`, `failed_24h`, `failed_7d`),
`progressive_speed`, `include_hidden`.

## `narrator`

The narrator's settings, one entry holding all of them. Everything except
`apply_gui_hooks`, `ffmpeg` and `prices` can also be set from the narrator
page's settings (⚙), which writes back here.

  - `openai_api_key` — your OpenAI key. The environment variable
  `OPENAI_API_KEY` overrides it if set when Anki starts.
  - `order` — `browser`, `learning`, `created` or `random`.
  - `text_model` — the chat model that writes each narration (`gpt-5.4`).
  - `tts_model` — the speech model (`gpt-4o-mini-tts`; `tts-1-hd` also works).
  - `stt_model` — the model that understands what you ask
  (`gpt-4o-mini-transcribe`; `whisper-1` also works).
  - `voice` — one of OpenAI's voices: cedar, echo, onyx, ash (male), marin,
  alloy, ballad, coral, fable, nova, sage, shimmer, verse.
  - `seconds` — planned narration length per card the page starts with.
  - `gap` — pause after a card before the next one, in seconds.
  - `reveal` — a card comes up with its front, as in Anki; the back is
    revealed `auto`, `reveal_gap` seconds into the narration, or on `wait`
    only on Enter, a click or a tap. The narration tells the whole card
    either way.
  - `reveal_gap` — seconds of narration before the back is revealed.
  - `style` — free text the model is told about how to narrate.
  - `language` — spoken language, as a name or ISO code (`German`, `de`,
  `Polish`); empty means each card's own language, as the model names it.
  - `include_hidden` — also narrate suspended and buried cards.
  - `vision` — on an image occlusion card (Anki's own, or Image Occlusion
    Enhanced), show the text model the picture and the region under the
    mask, so it reads the hidden label and narrates that structure. One
    vision call per picture and mask, about a cent, cached. Off: such a
    card has nothing to say and is held for two seconds.
  - `speed` — playback speed, 0.5 to 3; the audio is stretched, pitch kept.
  - `daily_budget` — US dollars a day after which no new narration is made;
  0 for no limit.
  - `pronunciation` — `{"term": "how to say it"}`, applied to what the voice
  is given, longest term first; a term made of letters only matches whole
  words.
  - `share` — answer on the local network (`0.0.0.0`) rather than only on this
  machine, so a phone can open the page; the key stays required.
  - `apply_gui_hooks` — run `gui_hooks.card_will_show`, the hook the reviewer
  uses, so other add-ons can post-process the cards. Off by default: those
  hooks are written for the reviewer and may inject controls that only work
  inside Anki.
  - `last_search` — the search the page opened on last.
  - `ffmpeg` — the ffmpeg the exports run: a name on PATH (`ffmpeg`) or a
  full path.
  - `prices` — what OpenAI charges, in US dollars, for the cost estimate: chat
  models as `{"input": …, "output": …}` per million tokens, speech models as
  `{"per_minute": …}` (gpt-4o-mini-tts) or `{"per_1m_chars": …}` (tts-1).
  A model not listed is matched by the longest listed prefix. Update these
  when OpenAI's price list changes; the add-on's defaults are merged in
  underneath, so a stored config only needs the entries that differ.
