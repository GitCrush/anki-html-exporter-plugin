"""The narration itself: a model condenses the card, a voice reads it.

Two stages, each cached on disk under ``user_files/cache``:

* the **script** -- what is said -- keyed by the card's content, the planned
  length, the model and the style. Changing the voice does not change it.
* the **audio**, keyed by the spoken form of the script and the voice.

The script is light markdown -- paragraphs, and the key terms in bold -- so
the page can show it as a readable text beside the card. The voice gets it
with the marks stripped off.

So a listener who tries another voice pays for speech only, and one who asks
for a longer telling gets a new script but keeps the old one for later. The
exports (audiobook, video) will read the same cache, which is why the audio is
written as files rather than streamed.

Requests run on the server's request threads, never on the collection thread:
a model call takes seconds, and the collection must not wait for it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import imaging, mp3, occlusion
from ..renderer import RenderedCard
from .openai_api import OpenAI
from .text import card_text
from .usage import Ledger

# What the voices manage at the calm pace they are asked for, in German
# medical text: measured at 1.5-1.8 words a second. This is only the starting
# point -- every narration's audio is measured, and the running rate replaces
# it, see Pace.
WORDS_PER_SECOND = 1.7

# Not too many at once: the page prefetches the next cards, and nothing is
# gained by asking for twenty narrations in parallel.
PARALLEL = 3

SYSTEM_PROMPT = """You are the narrator of a slide show made from flashcards. For each card you are given its content and a target length. Write what the narrator says while that card is on screen.

Rules:
- Tell the essential content of the card as a short, coherent spoken piece: what the card is about and what one should take away. Flowing speech, complete sentences.
- The listener sees the card, so do not describe its layout, do not say "this card", "the front", "the back", "the answer is", do not mention decks, tags, note types or field names.
- Use the language the card is written in unless told otherwise. Begin your answer with one line "Language: xx" -- the ISO 639-1 code of the language you narrate in (de, en, fr, es, ...) -- then a blank line, then the narration. That line is not spoken.
- Format: one to three paragraphs, separated by a blank line, as the length allows. Mark the few terms the listener should remember -- names, numbers, the key concept -- in **bold**; two to five per card, never whole sentences. Nothing else: no headings, no bullet points, no other markdown, no quotation marks around the whole text, no stage directions.
- End with one more paragraph of a single sentence: the card's message, what the listener should take away. Put it as a statement of the point itself, not as "the point is" or "remember that".
- You are told how many words the card itself holds and how many words telling all of it takes -- its natural length -- and how many words you may use. Below the natural length, compress: keep the key facts, drop the detail, never the point. At the natural length, tell the card. Above it, the room beyond the card is for understanding only: the why, the mechanism, an example, how it connects to what the listener knows -- never for saying the same thing at greater length, and never for facts you are not sure of. Stop as soon as there is nothing more worth saying; a short narration is right, a padded one is wrong.
- The word budget is a ceiling, and the closing sentence is inside it. With fewer than 25 words available there is no room for two: write one paragraph and let it be the message.
- Write for the ear: expand abbreviations that are not spoken as such, keep formulas readable when said aloud, prefer "for example" over "e.g.".
- Keep close to the requested length. Never pad; if the card is thin, say less.
- Do not invent facts that are not on the card. You may add a few words of context that make a bare fact understandable, but the card's content comes first.
- An image occlusion card asks for one thing: the label hidden under the mask, which you are given as read off the picture. Narrate that structure -- name it, say where it lies in the figure and what it is -- and use the other labels only to place it. Do not list what else is in the picture."""

VISION_PROMPT = """You are shown a labelled figure from a flashcard, then an enlarged piece of it: the region under a mask the learner has to name. Answer as JSON with three keys:
"label": the text under the mask, read exactly as written (if the region holds no text, a short name of what is marked there);
"context": one or two sentences, where this structure lies in the figure and what it is, in the language of the figure's labels;
"others": the other labels visible in the figure, comma-separated, at most twelve.
JSON only."""

# How the voice is to read. The language is named, and the direction given in
# that language where we have it: told in English only, the voice drifts
# towards an English accent. Other languages get the English direction with
# the language's name in it, which the voice follows well enough.
SPEECH_DIRECTION = {
    "de": (
        "Sprich Deutsch als Muttersprachler, mit korrekter deutscher Aussprache, ohne "
        "fremden Akzent. Ruhig, gleichmäßig, sachlich, wie ein Dokumentarsprecher oder "
        "ein Dozent, der sein Fach kennt. Gelassenes natürliches Tempo, deutliche "
        "Artikulation, keine Dramatik, keine Begeisterung, eine kurze Pause zwischen "
        "den Absätzen."
    ),
    "en": (
        "Speak English as a native speaker. Calm, even, matter-of-fact delivery, like "
        "a documentary narrator or a lecturer who knows the subject well. Unhurried "
        "natural pace, clear articulation, no dramatisation, no enthusiasm, a short "
        "pause between paragraphs."
    ),
    "fr": (
        "Parle français en locuteur natif, avec une prononciation française correcte, "
        "sans accent étranger. Calme, régulier, factuel, comme un narrateur de "
        "documentaire ou un enseignant qui connaît son sujet. Rythme naturel et posé, "
        "articulation nette, sans dramatisation, sans enthousiasme, une courte pause "
        "entre les paragraphes."
    ),
    "es": (
        "Habla español como hablante nativo, con pronunciación española correcta, sin "
        "acento extranjero. Tranquilo, uniforme, objetivo, como un narrador de "
        "documentales o un profesor que domina su materia. Ritmo natural y pausado, "
        "articulación clara, sin dramatismo, sin entusiasmo, una breve pausa entre "
        "párrafos."
    ),
    "it": (
        "Parla italiano come madrelingua, con pronuncia italiana corretta, senza "
        "accento straniero. Calmo, uniforme, oggettivo, come un narratore di "
        "documentari o un docente che conosce la materia. Ritmo naturale e pacato, "
        "articolazione chiara, senza drammatizzare, senza entusiasmo, una breve pausa "
        "tra i paragrafi."
    ),
    "pt": (
        "Fala português como falante nativo, com pronúncia portuguesa correta, sem "
        "sotaque estrangeiro. Calmo, uniforme, objetivo, como um narrador de "
        "documentários ou um professor que domina a matéria. Ritmo natural e pausado, "
        "articulação clara, sem dramatização, sem entusiasmo, uma breve pausa entre "
        "parágrafos."
    ),
    "nl": (
        "Spreek Nederlands als moedertaalspreker, met correcte Nederlandse uitspraak, "
        "zonder buitenlands accent. Rustig, gelijkmatig, zakelijk, als een "
        "documentairestem of een docent die zijn vak kent. Ontspannen natuurlijk tempo, "
        "duidelijke articulatie, geen dramatiek, geen enthousiasme, een korte pauze "
        "tussen alinea's."
    ),
}
SPEECH_DIRECTION_OTHER = (
    "Speak {language} as a native speaker, with correct native pronunciation and no "
    "foreign accent. Calm, even, matter-of-fact delivery, like a documentary narrator "
    "or a lecturer who knows the subject well. Unhurried natural pace, clear "
    "articulation, no dramatisation, no enthusiasm, a short pause between paragraphs."
)

# ISO 639-1 codes to the names the voice is told, for the languages it is
# likely to meet; an unknown code is passed on as it is.
LANGUAGE_NAMES = {
    "ar": "Arabic", "bg": "Bulgarian", "ca": "Catalan", "cs": "Czech", "da": "Danish",
    "de": "German", "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian", "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "la": "Latin", "lt": "Lithuanian", "lv": "Latvian",
    "nl": "Dutch", "no": "Norwegian", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sk": "Slovak", "sl": "Slovenian", "sr": "Serbian", "sv": "Swedish",
    "th": "Thai", "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese",
}
_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
_NAME_TO_CODE.update({"deutsch": "de", "englisch": "en", "français": "fr", "französisch": "fr",
                      "español": "es", "spanisch": "es", "italiano": "it", "italienisch": "it",
                      "português": "pt", "nederlands": "nl", "latein": "la", "русский": "ru"})

# Enough of the words that carry a language to tell the common ones apart
# when the model's own line is missing.
_MARKER_WORDS = {
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "ein", "eine", "des", "im", "zu",
           "von", "den", "dem", "sich", "auf", "für", "wird", "werden", "bei", "als", "sind"},
    "en": {"the", "and", "is", "of", "to", "in", "that", "with", "for", "are", "this", "which",
           "from", "by", "it", "as", "on", "an", "be", "or"},
    "fr": {"le", "la", "les", "des", "est", "et", "une", "dans", "pour", "que", "qui", "pas",
           "sont", "avec", "sur", "au", "aux", "du", "ce", "cette"},
    "es": {"el", "los", "las", "es", "y", "una", "en", "que", "por", "con", "para", "del", "se",
           "son", "como", "más", "pero", "sus", "este", "esta"},
    "it": {"il", "lo", "gli", "è", "e", "una", "che", "per", "con", "non", "del", "della", "sono",
           "come", "più", "nel", "nella", "anche", "questo", "questa"},
    "nl": {"de", "het", "een", "en", "is", "van", "dat", "niet", "met", "voor", "zijn", "wordt",
           "worden", "ook", "bij", "aan", "door", "naar", "deze", "dit"},
    "pt": {"o", "os", "as", "é", "e", "uma", "em", "que", "por", "com", "para", "não", "são",
           "como", "mais", "do", "da", "dos", "das", "ao"},
}
LANGUAGE_LINE_RE = re.compile(r"^\s*language\s*[:=]\s*([A-Za-z-]{2,20})\s*\n+", re.IGNORECASE)


def language_code(value: str) -> str:
    """'German', 'de', 'de-DE' -> 'de'; anything unknown is kept, lower-cased."""
    value = value.strip().lower()
    if not value:
        return ""
    if value in _NAME_TO_CODE:
        return _NAME_TO_CODE[value]
    head = value.split("-")[0]
    return head if len(head) == 2 else value


def split_language(answer: str) -> tuple[str, str]:
    """The language line the model was asked for, off the front of its answer."""
    match = LANGUAGE_LINE_RE.match(answer)
    if not match:
        return "", answer
    return language_code(match.group(1)), answer[match.end():]


def guess_language(text: str) -> str:
    """A code from the words alone, for a script without the model's line."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    scores = {code: sum(1 for w in words if w in marks) for code, marks in _MARKER_WORDS.items()}
    best = max(scores, key=scores.get) if scores else ""
    return best if scores.get(best, 0) > 0 else ""


def detect_language(text: str, configured: str = "", declared: str = "") -> str:
    """The language the voice is told: the config's, else the model's, else guessed."""
    return language_code(configured) or language_code(declared) or guess_language(text)


def speech_instructions(language: str) -> str:
    if language in SPEECH_DIRECTION:
        return SPEECH_DIRECTION[language]
    if language:
        name = LANGUAGE_NAMES.get(language, language.capitalize())
        return SPEECH_DIRECTION_OTHER.format(language=name)
    return SPEECH_DIRECTION["en"]


class Pace:
    """How many words a second the voice actually speaks, measured as it goes.

    The model is given a word budget for the seconds asked for; how long the
    audio then runs depends on the language, the voice and the delivery. So
    each narration's words and seconds are added up, and the ratio becomes
    the budget for the next one. Kept on disk with the cache.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.words = 0
        self.seconds = 0.0
        try:
            data = json.loads(path.read_text("utf-8"))
            self.words, self.seconds = int(data["words"]), float(data["seconds"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def rate(self) -> float:
        with self._lock:
            # A few narrations are enough to know; before that, the default.
            if self.seconds < 60:
                return WORDS_PER_SECOND
            return min(3.0, max(1.0, self.words / self.seconds))

    def add(self, words: int, seconds: float) -> None:
        if words <= 0 or seconds <= 0:
            return
        with self._lock:
            self.words += words
            self.seconds += seconds
            try:
                self.path.write_text(json.dumps({"words": self.words, "seconds": self.seconds}), "utf-8")
            except OSError:
                pass


@dataclass
class Narration:
    text: str
    audio: Path
    duration: float
    words: int
    cached: bool
    language: str = ""

    def as_json(self, audio_url: str) -> dict:
        return {
            "text": self.text,
            "audio": audio_url,
            "duration": self.duration,
            "words": self.words,
            "cached": self.cached,
            "language": self.language,
        }


class BudgetExceeded(Exception):
    """Today's narrations have cost what the config allows for a day."""


def pronounce(words: str, glossary: dict) -> str:
    """The spoken words with the glossary's replacements made, longest first."""
    for term in sorted(glossary, key=len, reverse=True):
        replacement = glossary[term]
        if not term:
            continue
        pattern = re.escape(term)
        # A term made of letters must not fire inside a longer word.
        if term[0].isalnum():
            pattern = r"(?<![\w-])" + pattern
        if term[-1].isalnum():
            pattern = pattern + r"(?![\w-])"
        words = re.sub(pattern, replacement, words)
    return words


class Narrator:
    def __init__(self, cache_dir: Path, ledger: Ledger | None = None) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger or Ledger(cache_dir.parent / "usage.json")
        self.pace = Pace(cache_dir.parent / "pace.json")
        # The collection's media folder, told by whoever renders the cards;
        # the pictures of image occlusion cards are read from it.
        self.media_dir: str | None = None
        self._limit = threading.BoundedSemaphore(PARALLEL)
        self._lock = threading.Lock()
        # One request per job at a time: a second thread asking for the same
        # narration waits for the first rather than paying for it again.
        self._inflight: dict[str, threading.Event] = {}
        self._results: dict[str, Any] = {}

    # Keys
    ##################################################################

    @staticmethod
    def script_key(config: dict, content_hash: str, seconds: int) -> str:
        raw = "|".join([content_hash, str(seconds), config["text_model"],
                        config["style"], config["language"], "v7"])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def audio_key(config: dict, text: str, voice: str, language: str = "") -> str:
        instructions = speech_instructions(detect_language(text, config.get("language", ""), language))
        voiced = pronounce(spoken(text), config.get("pronunciation") or {})
        raw = "|".join([voiced, voice, config["tts_model"], instructions, "v1"])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    # Public API
    ##################################################################

    def cached_duration(self, card: RenderedCard, config: dict, seconds: int, voice: str) -> float | None:
        """The length of the narration this would give, if script and audio are on disk."""
        skey = self.script_key(config, card_text(card, self._seen_cached(card, config))["hash"], seconds)
        script_path = self.cache_dir / f"script-{skey}.json"
        if not script_path.is_file():
            return None
        try:
            script = json.loads(script_path.read_text("utf-8"))
            meta_path = self.cache_dir / f"audio-{self.audio_key(config, script['text'], voice, script.get('language', ''))}.json"
            return float(json.loads(meta_path.read_text("utf-8"))["duration"])
        except (OSError, ValueError, KeyError):
            return None

    def narrate(
        self, card: RenderedCard, config: dict, seconds: int, voice: str, force: bool = False
    ) -> Narration:
        content = card_text(card, self.seen(card, config))
        skey = self.script_key(config, content["hash"], seconds)
        job = f"{skey}:{voice}:{int(force)}"

        with self._lock:
            waiting = self._inflight.get(job)
            if waiting is None:
                waiting = threading.Event()
                self._inflight[job] = waiting
                leader = True
            else:
                leader = False

        if not leader:
            waiting.wait()
            result = self._results.get(job)
            if isinstance(result, BaseException):
                raise result
            return result

        try:
            with self._limit:
                result = self._narrate(content, skey, config, seconds, voice, force)
        except BaseException as exc:
            result = exc
            raise
        finally:
            with self._lock:
                self._results[job] = result
                self._inflight.pop(job, None)
                waiting.set()
                # Followers read the result right after the event; a moment
                # later nobody needs it any more.
                threading.Timer(5.0, lambda: self._results.pop(job, None)).start()
        return result

    def _narrate(
        self, content: dict, skey: str, config: dict, seconds: int, voice: str, force: bool
    ) -> Narration:
        client = self.client(config)
        script_path = self.cache_dir / f"script-{skey}.json"
        cached = script_path.is_file() and not force
        if cached:
            script = json.loads(script_path.read_text("utf-8"))
            text, language = script["text"], script.get("language", "")
        else:
            self._check_budget(config)
            text, language = self._write_script(client, content, config, seconds)
            script_path.write_text(
                json.dumps({"text": text, "language": language, "seconds": seconds,
                            "model": config["text_model"], "content": content["prompt"]},
                           ensure_ascii=False, indent=2),
                "utf-8",
            )
        language = detect_language(text, config.get("language", ""), language)

        akey = self.audio_key(config, text, voice, language)
        audio_path = self.cache_dir / f"audio-{akey}.mp3"
        meta_path = self.cache_dir / f"audio-{akey}.json"
        if audio_path.is_file() and meta_path.is_file():
            duration = json.loads(meta_path.read_text("utf-8"))["duration"]
        else:
            cached = False
            self._check_budget(config)
            duration = self._speak(client, text, config, voice, audio_path, language)
            meta_path.write_text(
                json.dumps({"duration": duration, "voice": voice, "model": config["tts_model"],
                            "language": language}),
                "utf-8",
            )
        return Narration(text, audio_path, duration, len(spoken(text).split()), cached, language)

    # The picture, for an image occlusion card
    ##################################################################

    def _vision_key(self, found: occlusion.Occlusion, config: dict) -> Path | None:
        """Where the vision result for this picture and these boxes is kept."""
        try:
            picture = found.image.read_bytes()
        except OSError:
            return None
        raw = hashlib.sha1(picture).hexdigest() + "|" + "|".join(
            f"{b.left:.4f},{b.top:.4f},{b.width:.4f},{b.height:.4f}" for b in found.boxes
        ) + "|" + config["text_model"] + "|v1"
        return self.cache_dir / f"vision-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]}.json"

    def _seen_cached(self, card: RenderedCard, config: dict) -> dict | None:
        """The vision result if it is on disk; nothing is asked for here."""
        if not config.get("vision", True) or not self.media_dir:
            return None
        found = occlusion.find(card, self.media_dir)
        path = self._vision_key(found, config) if found else None
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def seen(self, card: RenderedCard, config: dict) -> dict | None:
        """What the vision model read under this card's mask, cached by picture and box."""
        cached = self._seen_cached(card, config)
        if cached is not None:
            return cached
        if not config.get("vision", True) or not self.media_dir:
            return None
        found = occlusion.find(card, self.media_dir)
        path = self._vision_key(found, config) if found else None
        if found is None or path is None:
            return None
        whole = imaging.whole_png(found.image)
        if whole is None:
            return None
        images = [(whole[0], whole[1], "low")]
        for box in found.boxes:
            crop = imaging.crop_png(found.image, box)
            if crop:
                images.append((crop, "image/png", "high"))
        boxes = "; ".join(
            f"left {b.left:.0%}, top {b.top:.0%}, width {b.width:.0%}, height {b.height:.0%}" for b in found.boxes
        )
        user = f"The mask covers the region at {boxes} of the picture." + (
            " The enlarged piece(s) follow the whole figure." if len(images) > 1 else
            " No enlarged piece could be made; read the region from the whole figure."
        )
        self._check_budget(config)
        answer, tokens_in, tokens_out = self.client(config).see(config["text_model"], VISION_PROMPT, user, images)
        self.ledger.add_chat(config.get("prices", {}), config["text_model"], tokens_in, tokens_out)
        seen = _parse_seen(answer)
        if seen is None:
            return None
        try:
            path.write_text(json.dumps(seen, ensure_ascii=False), "utf-8")
        except OSError:
            pass
        return seen

    def client(self, config: dict) -> OpenAI:
        """The API client, made here so that one place decides which."""
        return OpenAI(config["openai_api_key"])

    def speak_text(self, text: str, config: dict, voice: str, language: str = "") -> tuple[Path, float, str]:
        """Any text as audio, through the same cache the narrations use."""
        language = detect_language(text, config.get("language", ""), language)
        akey = self.audio_key(config, text, voice, language)
        audio_path = self.cache_dir / f"audio-{akey}.mp3"
        meta_path = self.cache_dir / f"audio-{akey}.json"
        if audio_path.is_file() and meta_path.is_file():
            return audio_path, json.loads(meta_path.read_text("utf-8"))["duration"], language
        self._check_budget(config)
        duration = self._speak(self.client(config), text, config, voice, audio_path, language)
        meta_path.write_text(
            json.dumps({"duration": duration, "voice": voice, "model": config["tts_model"], "language": language}),
            "utf-8",
        )
        return audio_path, duration, language

    def _check_budget(self, config: dict) -> None:
        budget = float(config.get("daily_budget") or 0)
        if budget <= 0:
            return
        spent = self.ledger.snapshot()["today"].get("cost", 0)
        if spent >= budget:
            raise BudgetExceeded(
                f"today's narrations have cost ${spent:.2f}, the daily budget is ${budget:.2f} "
                "(raise it under Settings to go on)"
            )

    # Stages
    ##################################################################

    def _write_script(self, client: OpenAI, content: dict, config: dict, seconds: int) -> tuple[str, str]:
        """The script, and the language the model says it wrote it in."""
        if content["empty"]:
            return "", ""
        words = max(12, round(seconds * self.pace.rate()))
        natural = natural_length(content["content_words"])
        if words < natural * 0.8:
            depth = "That is less than the card: compress to the essentials."
        elif words <= natural * 1.3:
            depth = "That is about the card's own length: tell the card."
        else:
            depth = ("That is more than the card: the room beyond it is for explanation that aids "
                     "understanding, and only as much of that as is worth saying.")
        instructions = [
            f"The card holds about {content['content_words']} words; telling all of it takes about {natural} words.",
            f"Length: at most {words} words, which is about {seconds} seconds of speech. {depth}",
        ]
        if config["language"]:
            instructions.append(f"Speak in {config['language']}, whatever language the card is in.")
        if config["style"]:
            instructions.append(f"Style: {config['style']}")
        user = "\n".join(instructions) + "\n\nCARD:\n" + content["prompt"]
        answer, tokens_in, tokens_out = client.complete(config["text_model"], SYSTEM_PROMPT, user)
        self.ledger.add_chat(config.get("prices", {}), config["text_model"], tokens_in, tokens_out)
        language, text = split_language(answer.strip())
        return _clean(text), language

    def _speak(self, client: OpenAI, text: str, config: dict, voice: str, path: Path, language: str = "") -> float:
        if not text.strip():
            # Nothing to say: the page holds the card for a moment on its own.
            path.write_bytes(b"")
            return 0.0
        words = pronounce(spoken(text), config.get("pronunciation") or {})
        instructions = speech_instructions(language)
        payload = client.speak(config["tts_model"], voice, words, instructions)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(payload)
        tmp.replace(path)
        duration = mp3.duration(path)
        self.ledger.add_speech(config.get("prices", {}), config["tts_model"], len(words), duration)
        self.pace.add(len(words.split()), duration)
        return duration


class Preparer:
    """A background run over the next few cards.

    One job at a time; a new one replaces the last, since the reader has
    moved on and the cards it was working through are either done or no
    longer next. The job narrates one card at a time, so the page's own
    requests -- the card the reader is looking at right now -- are never
    starved.
    """

    def __init__(self, narrator: Narrator) -> None:
        self.narrator = narrator
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict[str, Any] = {"done": 0, "total": 0, "failed": 0, "error": ""}

    def start(self, card_ids: list[int], render: Any, config: dict, seconds: int, voice: str) -> None:
        with self._lock:
            self._stop.set()
            self._stop = threading.Event()
            self._state = {"done": 0, "total": len(card_ids), "failed": 0, "error": ""}
            self._thread = threading.Thread(
                target=self._run,
                args=(card_ids, render, config, seconds, voice, self._stop, self._state),
                name="narrator-prepare", daemon=True,
            )
            self._thread.start()

    def _run(self, card_ids, render, config, seconds, voice, stop, state) -> None:
        for card_id in card_ids:
            if stop.is_set():
                break
            try:
                self.narrator.narrate(render(card_id), config, seconds, voice)
            except Exception as exc:
                state["failed"] += 1
                state["error"] = str(exc)
                # A key that does not work fails every card the same way.
                if state["failed"] >= 2:
                    break
            state["done"] += 1

    def stop(self) -> None:
        with self._lock:
            self._stop.set()

    def status(self) -> dict:
        state = dict(self._state)
        state["running"] = bool(self._thread and self._thread.is_alive())
        return state


def _parse_seen(answer: str) -> dict | None:
    text = answer.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    return {k: str(data.get(k) or "").strip() for k in ("label", "context", "others")}


def natural_length(content_words: int) -> int:
    """The words it takes to tell a card in full: its own, said as prose.

    Card text is telegraphic -- "Afferenzen: Muskelspindel" -- and speech
    needs articles, verbs and a sentence to hang a fact on; a fifth more, and
    a sentence to open and one to close.
    """
    return max(15, round(content_words * 1.2) + 12)


def _clean(text: str) -> str:
    """The model's answer as the script is kept: paragraphs and bold, nothing else.

    The model is told not to, but a wrapping quote, a heading or a bullet does
    turn up now and then.
    """
    text = text.strip()
    text = re.sub(r"^[\"“„]|[\"”“]$", "", text)
    lines = []
    for line in text.splitlines():
        if re.match(r"\s*#", line):
            continue  # a heading is a label, not something said
        line = re.sub(r"^\s*([-*•]\s+|\d+[.)]\s+)", "", line)
        lines.append(re.sub(r"[ \t]+", " ", line).strip())
    text = "\n".join(lines)
    text = re.sub(r"[`#]+", "", text)
    return re.sub(r"\n{2,}", "\n\n", text).strip()


def spoken(text: str) -> str:
    """The script as the voice is given it: the marks off, one flow of words."""
    text = re.sub(r"[*_`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()
