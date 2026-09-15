"""Hypnagog: the cards as a rapid, full-screen presentation.

Each card's fact flashes, shows, and fades -- cloze targets blanked and then
revealed one by one in their own colours, a question's answer fading in
below it -- with the ambient audio of the "Polybius" mode or none at all.
Named after the hypnagogic state, the threshold between waking and sleep.

Once an add-on of its own with a Qt window; here a page of the live server,
``/hypnagog``, opened from the export dialog on the cards it shows. The
engine is ``web/hypnagog.js``, self-contained; ``extract`` turns notes into
the items it plays; ``routes`` serves both.
"""
