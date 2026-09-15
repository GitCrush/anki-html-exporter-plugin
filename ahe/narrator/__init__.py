"""The narrator: the collection as a narrated slide show.

Each card is shown the way the export shows it -- the same renderer, the
same frame -- while a language model condenses it to a spoken text of the
length asked for and text-to-speech reads it. Around that: a look-ahead, a
tutor to talk with about the card, an audiobook and a video export, and a
phone opening the page over TLS.

Everything lives under this package and hangs on the live server as one
:class:`routes.Module`; ``service`` is what the menu calls.
"""
