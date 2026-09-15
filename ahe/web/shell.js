/* The export shell.
 *
 * Every card side is rendered in its own frame whose document is an exact
 * copy of what Anki's reviewer builds: Anki's theme variables and
 * reviewer.css, then the note type's stylesheet, a body carrying the
 * `card cardN` classes and the rendered side inside #qa. That isolation is
 * what makes different note types -- and the JavaScript many of them ship --
 * coexist on one page without interfering with each other.
 *
 * Frames are built here rather than written into the file so that the note
 * type stylesheets are stored once instead of once per card, and are mounted
 * lazily so that an export of a few thousand cards still opens instantly. */

(function () {
    "use strict";

    var SIDES = { q: "q", a: "a" };
    /* A card mounts once it is this close to the viewport ... */
    var MOUNT_MARGIN = 800;
    /* ... and is taken down again once it is this far from it. The gap between
       the two is what keeps a card at the edge of the screen from mounting and
       unmounting on every scroll tick. */
    var KEEP_MARGIN = 3000;
    /* A hard ceiling on mounted cards for the cases geometry alone does not
       reach: very short cards, or a window tall enough to hold dozens. */
    var MOUNT_BUDGET = 40;
    var LAZY_MARGIN = MOUNT_MARGIN + "px";
    /* Up to this many cards, everything is mounted in the background shortly
       after load, so find-in-page and a plain Ctrl+P cover the whole export.
       Above it the explicit "Load all" button stays in charge. */
    var AUTO_MOUNT_LIMIT = 300;

    var dataEl = document.getElementById("ahe-data");
    var data = JSON.parse(dataEl.textContent);
    /* The payload is by far the largest thing in the document -- tens of
       megabytes in an export of a few thousand cards. Once parsed it exists
       twice: as the object, and as the text node it was read from. The text is
       of no further use, and holding on to it doubles the bill. */
    if (dataEl.parentNode) {
        dataEl.parentNode.removeChild(dataEl);
    }
    var cardsById = Object.create(null);
    data.cards.forEach(function (card) {
        cardsById[card.id] = card;
    });

    var frames = Object.create(null);
    /* The height a frame last reported, kept per card side so that taking a
       card down leaves a gap of the right size behind it. */
    var frameHeights = Object.create(null);
    /* What the reader has uncovered inside a frame, per card side. A frame is
       a document: taking it down forgets everything in it, so the frame
       reports this upwards and gets it back when it is built again. */
    var revealStates = Object.create(null);
    /* Articles carrying at least one mounted frame. Deliberately short. */
    var mounted = [];
    /* Switched off once everything was asked for at once. */
    var recycling = true;
    var root = document.documentElement;
    /* Reassigned by a shuffle, which rearranges the cards themselves. */
    var articles = Array.prototype.slice.call(
        document.querySelectorAll(".ahe-card")
    );

    /* Served by the add-on out of the running collection, rather than written
       to disk with its cards baked in. */
    var live = !!(data.meta && data.meta.live);
    /* The narrator's page: the bar, the panel and the search are this
       script's; what the scope holds is played by the narrator's own script,
       which is told through window.AHE_NARRATOR. */
    var narrator = !!(data.meta && data.meta.narrator);

    var state = loadState();

    /* --- persisted view state -------------------------------------------- */

    function defaults() {
        return {
            /* auto | qa | a | q -- see modeShows() */
            mode: data.meta.viewMode || "auto",
            special: !!data.meta.showSpecial,
            fields: !!data.meta.showFields,
            meta: !!data.meta.showMeta,
            dark: !!data.meta.dark,
            interactive: data.meta.interactive !== false,
            study: !!data.meta.study,
            printSize: "large",
            /* facet key -> the indices ticked in the navigation panel */
            filters: { decks: [], tags: [], notetypes: [], states: [], flags: [] },
            hiddenFields: [],
            hiddenSpecial: (data.meta.hiddenSpecial || []).slice()
        };
    }

    function storageKey() {
        return "ahe:" + (data.meta.id || "export");
    }

    function loadState() {
        var base = defaults();
        try {
            var stored = window.localStorage.getItem(storageKey());
            if (stored) {
                var parsed = JSON.parse(stored);
                Object.keys(base).forEach(function (key) {
                    if (parsed[key] !== undefined) {
                        base[key] = parsed[key];
                    }
                });
            }
        } catch (err) {
            /* file:// without storage access, or corrupt state: use defaults */
        }
        return base;
    }

    function saveState() {
        try {
            window.localStorage.setItem(storageKey(), JSON.stringify(state));
        } catch (err) {
            /* not fatal */
        }
    }

    /* --- frame construction ---------------------------------------------- */

    function mathjaxConfigScript() {
        var config = {
            tex: {
                displayMath: [["\\[", "\\]"]],
                processEscapes: false,
                processEnvironments: false,
                processRefs: false,
                packages: { "[+]": ["noerrors", "mathtools"], "[-]": ["textmacros"] }
            },
            loader: {
                load: ["[tex]/noerrors", "[tex]/mathtools"]
            },
            startup: { typeset: true }
        };
        if (data.meta.mathjaxDir) {
            config.loader.paths = { mathjax: data.meta.mathjaxDir };
        }
        return "window.MathJax=" + JSON.stringify(config) + ";";
    }

    /* Bundled scripts live next to the document in folder exports and as
       base64 inside the payload in single-file exports; in the latter case
       every frame is handed the same blob URL, so the source is stored once
       and the browser can cache the parse. */
    var scriptUrls = Object.create(null);

    function scriptUrl(name) {
        if (scriptUrls[name] !== undefined) {
            return scriptUrls[name];
        }
        var direct = data.meta[name + "Url"];
        if (direct) {
            scriptUrls[name] = direct;
            return direct;
        }
        var encoded = data[name + "Source"];
        if (!encoded) {
            scriptUrls[name] = "";
            return "";
        }
        try {
            var source = atob(encoded);
            var bytes = new Uint8Array(source.length);
            for (var i = 0; i < source.length; i++) {
                bytes[i] = source.charCodeAt(i);
            }
            scriptUrls[name] = URL.createObjectURL(
                new Blob([bytes], { type: "text/javascript" })
            );
        } catch (err) {
            scriptUrls[name] = "";
        }
        return scriptUrls[name];
    }

    function buildDocument(card, bodyHtml, frameId, side) {
        var revealed = revealStates[frameId + variant(side)] || null;
        var htmlAttrs = state.dark ? ' class="night-mode"' : "";
        var bodyClass = card.bodyClass + (state.dark ? " nightMode night_mode" : "");
        var head =
            '<meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1">' +
            /* Before anything the note type ships: a template calling into
               the reviewer while it is parsed must not throw. */
            "<script>" + data.shared.shimJs + "<\/script>" +
            "<style>:root{--ahe-vh:" +
            Math.max(320, Math.round(window.innerHeight * 0.95)) +
            "px}</style>" +
            "<style>" + data.shared.theme + "</style>" +
            "<style>" + data.shared.reviewer + "</style>" +
            "<style>" + data.shared.frame + "</style>" +
            "<style>" + (data.css[card.ntid] || "") + "</style>";

        if (card.jquery) {
            /* Not async and not deferred: the template's own inline scripts
               run while the frame is parsed and expect $ to be there. */
            var jquery = scriptUrl("jquery");
            if (jquery) {
                head += '<script src="' + jquery + '"><\/script>';
            }
        }

        if (card.mathjax) {
            var mathjax = scriptUrl("mathjax");
            if (mathjax) {
                head +=
                    "<script>" + mathjaxConfigScript() + "<\/script>" +
                    '<script id="MathJax-script" async src="' + mathjax + '"><\/script>';
            }
        }

        return (
            "<!doctype html><html" + htmlAttrs + ' dir="ltr"><head>' + head +
            '</head><body class="' + bodyClass + '">' +
            '<div id="qa">' + bodyHtml + "</div>" +
            "<script>window.AHE_FRAME_ID=" + JSON.stringify(frameId) +
            ";window.AHE_SIDE=" + JSON.stringify(side) +
            ";window.AHE_INTERACTIVE=" + (state.interactive ? "true" : "false") +
            /* Numbers and booleans only, so there is nothing in here that
               could close the script element early. */
            ";window.AHE_REVEALED=" + JSON.stringify(revealed) +
            ";<\/script>" +
            "<script>" + data.shared.frameJs + "<\/script>" +
            "</body></html>"
        );
    }

    function mount(article, side) {
        var slot = article.querySelector(
            '.ahe-side[data-side="' + side + '"] .ahe-slot'
        );
        if (!slot || slot.getAttribute("data-mounted") === side + variant(side)) {
            return;
        }

        var card = cardsById[article.getAttribute("data-cid")];
        if (!card) {
            return;
        }

        var body;
        if (side === SIDES.q) {
            body = card.q;
        } else {
            /* Anki's answer side normally repeats the question through
               {{FrontSide}}. "Q + A" shows the question separately and "A"
               does not want it at all, so both get it trimmed off; only "Auto"
               keeps the answer side whole, because there the embedded question
               is the context that replaces a separate front.

               aOnly is only written out when it genuinely differs: for a cloze
               card, and for any template whose back does not repeat the front,
               there is nothing to trim and the two would be the same string
               stored twice. */
            body = trimsAnswer() ? card.aOnly || card.a : card.a;
        }

        var frameId = side + "-" + card.id;
        var key = frameId + variant(side);
        var iframe = document.createElement("iframe");
        iframe.setAttribute("scrolling", "no");
        iframe.setAttribute("loading", "eager");
        iframe.setAttribute("data-ahe-id", frameId);
        iframe.setAttribute("data-ahe-key", key);
        iframe.setAttribute(
            "title",
            (side === SIDES.q ? "Front of card " : "Back of card ") + card.id
        );
        /* A card that has been mounted before comes back at the height it had,
           so re-mounting one the reader scrolled back to moves nothing. */
        if (frameHeights[key]) {
            iframe.style.height = frameHeights[key] + "px";
        }
        frames[frameId] = iframe;
        iframe.srcdoc = buildDocument(card, body, frameId, side);

        slot.textContent = "";
        slot.appendChild(iframe);
        slot.setAttribute("data-mounted", side + variant(side));
        if (mounted.indexOf(article) === -1) {
            mounted.push(article);
        }
    }

    function trimsAnswer() {
        return state.mode === "qa" || state.mode === "a";
    }

    /* Back frames have to be rebuilt when the view mode changes what they show. */
    function variant(side) {
        return side === SIDES.a && trimsAnswer() ? ":trimmed" : "";
    }

    function remountBacks() {
        articles.forEach(function (article) {
            var slot = article.querySelector('.ahe-side[data-side="a"] .ahe-slot');
            if (slot && slot.getAttribute("data-mounted")) {
                slot.removeAttribute("data-mounted");
                if (isSideVisible(article, SIDES.a)) {
                    mount(article, SIDES.a);
                }
            }
        });
    }

    window.addEventListener("message", function (event) {
        var payload = event.data;
        if (!payload || payload.source !== "ahe-frame") {
            return;
        }
        if (payload.type === "height") {
            var frame = frames[payload.id];
            if (frame) {
                var height = Math.max(40, payload.height);
                frame.style.height = height + "px";
                frameHeights[frame.getAttribute("data-ahe-key")] = height;
            }
        } else if (payload.type === "revealed") {
            var reporter = frames[payload.id];
            if (reporter) {
                revealStates[reporter.getAttribute("data-ahe-key")] = payload.state;
            }
        } else if (payload.type === "key") {
            /* A key pressed with the pointer over a card is caught by that
               card's frame, which has no idea what a card is. */
            studyKey(payload.key);
        } else if (payload.type === "swipe") {
            var swiped = frames[payload.id];
            if (swiped) {
                frameSwipe(swiped, payload);
            }
        }
    });

    function broadcast(message) {
        Object.keys(frames).forEach(function (id) {
            var win = frames[id].contentWindow;
            if (win) {
                win.postMessage(
                    Object.assign({ source: "ahe-shell" }, message),
                    "*"
                );
            }
        });
    }

    /* --- visibility ------------------------------------------------------- */

    /* Which sides a mode asks for. "auto" is the useful default: the answer
       side of a normal template already repeats the question, and a cloze
       answer is the same text revealed, so showing the question again is just
       noise -- unless the two sides genuinely differ, which the exporter
       decided per card (frontRedundant). */
    function modeShows(card, side) {
        var mode = state.mode;
        if (side === SIDES.q) {
            return (
                mode === "q" ||
                mode === "qa" ||
                (mode === "auto" && !(card && card.frontRedundant))
            );
        }
        return mode === "a" || mode === "qa" || mode === "auto";
    }

    function isSideVisible(article, side) {
        if (article.classList.contains(side + "-on")) {
            return true;
        }
        if (article.classList.contains(side + "-off")) {
            return false;
        }
        if (state.study) {
            /* The point of study mode is that the answer is not on screen
               yet, so the mode's own q/a rules do not apply: the front is
               always up, the back only once it has been asked for. */
            return side === SIDES.q || article.classList.contains("ahe-revealed");
        }
        return modeShows(cardsById[article.getAttribute("data-cid")], side);
    }

    function applySides() {
        articles.forEach(function (article) {
            [SIDES.q, SIDES.a].forEach(function (side) {
                var section = article.querySelector(
                    '.ahe-side[data-side="' + side + '"]'
                );
                if (section) {
                    section.hidden = !isSideVisible(article, side);
                }
            });
        });
    }

    function applyState() {
        root.classList.toggle("show-special", state.special);
        root.classList.toggle("show-fields", state.fields);
        root.classList.toggle("show-meta", state.meta);
        root.classList.toggle("ahe-dark", state.dark);
        root.classList.toggle("ahe-study", state.study);
        applyHiddenNames();
        applySides();
        syncControls();
        mountVisible();
        saveState();
    }

    var hiddenStyle = document.createElement("style");
    document.head.appendChild(hiddenStyle);

    function cssEscape(value) {
        if (window.CSS && CSS.escape) {
            return CSS.escape(value);
        }
        return value.replace(/["\\]/g, "\\$&");
    }

    function applyHiddenNames() {
        var rules = [];
        state.hiddenFields.forEach(function (name) {
            rules.push('.ahe-field[data-field="' + cssEscape(name) + '"]');
        });
        state.hiddenSpecial.forEach(function (name) {
            rules.push('.ahe-special-row[data-special="' + cssEscape(name) + '"]');
        });
        hiddenStyle.textContent = rules.length
            ? rules.join(",") + "{display:none!important}"
            : "";
    }

    /* --- print size ------------------------------------------------------- */

    /* Printing.
     *
     * A card is laid out for a screen, so at 1:1 one card fills a sheet of
     * paper. Simply scaling a screen-width card down wastes the page twice
     * over: the shrunk card leaves a wide empty margin beside it, and at the
     * factor needed to fit two of them side by side its 20px body text lands
     * around 5pt.
     *
     * So the frames are first relaid out at a narrow width -- the card reflows
     * the way it would on a phone -- and only then scaled by a mild factor.
     * Two columns of that are readable and actually fill the sheet. Reflowing
     * changes the heights the frames report, which is why this has to happen
     * before the print dialog opens rather than in a beforeprint handler, and
     * why the Print button is the path that gets it right. */
    var PRINT_PAGE_W = 715; /* A4 portrait minus Chrome's default margins, 96dpi */
    var PRINT_PAGE_H = 950;
    var PRINT_GAP = 12;
    var PRINT_PRESETS = [
        { key: "large", cols: 1, scale: 0.8 },
        { key: "medium", cols: 2, scale: 0.62 },
        { key: "small", cols: 2, scale: 0.45 }
    ];

    function printPreset(key) {
        for (var i = 0; i < PRINT_PRESETS.length; i++) {
            if (PRINT_PRESETS[i].key === key) {
                return PRINT_PRESETS[i];
            }
        }
        return PRINT_PRESETS[0];
    }

    function columnWidth(preset) {
        return (PRINT_PAGE_W - PRINT_GAP * (preset.cols - 1)) / preset.cols;
    }

    /* Lay the frames out at the width they will be printed from. */
    var reCollapse = [];

    function beginPrintLayout() {
        var preset = printPreset(state.printSize);
        root.style.setProperty("--ahe-print-cols", String(preset.cols));
        root.style.setProperty(
            "--ahe-prep-w",
            Math.round(columnWidth(preset) / preset.scale) + "px"
        );
        root.classList.add("print-prep");
        openFieldsForPrint();
    }

    /* A collapsed field prints as its one-line teaser, and on paper there is
       nothing to click, so the fields block would come out as a list of
       truncated lines. Opening them is a plain DOM change, so unlike the frame
       relayout it can also be done from beforeprint -- which is what makes
       Ctrl+P produce the same fields as the Print button. */
    function openFieldsForPrint() {
        if (!state.fields) {
            return;
        }
        document.querySelectorAll(".ahe-field:not([open])").forEach(function (field) {
            reCollapse.push(field);
            field.open = true;
        });
    }

    function restoreFieldsAfterPrint() {
        reCollapse.forEach(function (field) {
            field.open = false;
        });
        reCollapse = [];
    }

    /* Once the reflowed heights have come back, work out the factor and the
       room each frame will take on paper.
       The factor is per card, not per side: a card that does not fit a column
       gets torn between two of them and leaves the rest of the first column
       empty, which is exactly the space we are trying to win back. Shrinking
       the card as a whole keeps it in one piece and lets the next card follow
       right after it. */
    var CARD_CHROME_PX = 70; /* header, side strips, borders, margin */

    function finishPrintLayout() {
        var preset = printPreset(state.printSize);
        articles.forEach(function (article) {
            var slots = article.querySelectorAll(".ahe-side:not([hidden]) .ahe-slot");
            var total = 0;
            var heights = [];
            slots.forEach(function (slot) {
                var frame = slot.querySelector("iframe");
                var height = frame ? parseFloat(frame.style.height) : 0;
                heights.push({ slot: slot, frame: frame, height: height || 0 });
                total += height || 0;
            });
            if (!total) {
                return;
            }
            var room = PRINT_PAGE_H - CARD_CHROME_PX;
            var scale = Math.min(preset.scale, room / total);
            heights.forEach(function (entry) {
                if (!entry.frame || !entry.height) {
                    return;
                }
                entry.frame.style.setProperty("--ahe-print-scale", scale.toFixed(4));
                entry.slot.style.setProperty(
                    "--ahe-print-height",
                    Math.ceil(entry.height * scale) + "px"
                );
            });
        });
    }

    function endPrintLayout() {
        root.classList.remove("print-prep");
        restoreFieldsAfterPrint();
        Object.keys(frames).forEach(function (id) {
            frames[id].style.removeProperty("--ahe-print-scale");
            var slot = frames[id].parentElement;
            if (slot) {
                slot.style.removeProperty("--ahe-print-height");
            }
        });
    }

    function withPrintLayout(action) {
        beginPrintLayout();
        return mountAll()
            .then(function () {
                /* give the reflowed frames time to report their new heights */
                return new Promise(function (resolve) {
                    setTimeout(resolve, 700);
                });
            })
            .then(function () {
                finishPrintLayout();
                return new Promise(function (resolve) {
                    setTimeout(resolve, 120);
                });
            })
            .then(action);
    }

    window.addEventListener("afterprint", endPrintLayout);

    /* --- mounting --------------------------------------------------------- */

    /* A frame is a whole document: Anki's theme variables and reviewer
       stylesheet, the note type's own CSS and whatever JavaScript it ships --
       widely used note types run a timer in every single card. Mounting one is
       expensive, and never taking one down again is what brings a large export
       to its knees: a few hundred cards into the page the browser is carrying
       a thousand live documents it will not show again.

       So a card that has left the neighbourhood is released, and its slot
       keeps the height its frame last reported. Scrolling back mounts it
       afresh and nothing moves in between. What the reader had uncovered on a
       recycled card is covered again -- the same thing a reload does, and the
       price of a page that stays answerable at any size. */

    function distance(article) {
        /* How far outside the viewport a card sits, in pixels; zero while any
           part of it is on screen. */
        var rect = article.getBoundingClientRect();
        if (rect.bottom < 0) {
            return -rect.bottom;
        }
        if (rect.top > window.innerHeight) {
            return rect.top - window.innerHeight;
        }
        return 0;
    }

    function mountArticle(article) {
        if (article.hidden) {
            return;
        }
        if (isSideVisible(article, SIDES.q)) {
            mount(article, SIDES.q);
        }
        if (isSideVisible(article, SIDES.a)) {
            mount(article, SIDES.a);
        }
    }

    function unmountArticle(article, keepHeight) {
        var slots = article.querySelectorAll(".ahe-slot[data-mounted]");
        Array.prototype.forEach.call(slots, function (slot) {
            var frame = slot.querySelector("iframe");
            var height = 0;
            if (frame) {
                height = parseFloat(frame.style.height) || 0;
                delete frames[frame.getAttribute("data-ahe-id")];
            }
            var placeholder = document.createElement("div");
            placeholder.className = "ahe-placeholder";
            placeholder.textContent = "…";
            if (keepHeight && height) {
                placeholder.style.height = height + "px";
            }
            slot.textContent = "";
            slot.appendChild(placeholder);
            slot.removeAttribute("data-mounted");
        });
        var at = mounted.indexOf(article);
        if (at !== -1) {
            mounted.splice(at, 1);
        }
    }

    function sweep() {
        if (!recycling || !mounted.length) {
            return;
        }

        /* Measured in one pass, acted on in the next. Taking a card down
           invalidates the layout, so a loop that alternated between reading a
           position and changing the document would force a reflow per card --
           the very stall this is here to prevent. */
        var ranked = mounted.map(function (article) {
            return { article: article, away: distance(article) };
        });
        ranked.sort(function (a, b) {
            return b.away - a.away;
        });

        var budget = mounted.length;
        for (var index = 0; index < ranked.length; index++) {
            var entry = ranked[index];
            /* Far enough away to release, or over the ceiling and not on
               screen. The ceiling is a backstop, never a reason to blank the
               card being read. */
            if (entry.away > KEEP_MARGIN || (budget > MOUNT_BUDGET && entry.away > 0)) {
                unmountArticle(entry.article, true);
                budget--;
            } else {
                /* Sorted by distance, so nothing after this qualifies. */
                break;
            }
        }
    }

    function mountVisible() {
        /* Cards sit in document order, so once one is below the horizon every
           card after it is too. In an export of twenty thousand cards the
           difference between stopping there and measuring all of them is the
           difference between a filter that applies at once and one that locks
           the tab for a second. */
        for (var index = 0; index < articles.length; index++) {
            var article = articles[index];
            if (article.hidden) {
                continue;
            }
            var rect = article.getBoundingClientRect();
            if (rect.top > window.innerHeight + MOUNT_MARGIN) {
                break;
            }
            if (rect.bottom > -MOUNT_MARGIN) {
                mountArticle(article);
            }
        }
        sweep();
    }

    function mountAll(onProgress) {
        /* Everything at once was asked for explicitly -- to print, or to let
           the browser's own find-in-page cover the export -- so the recycler
           stands down and stays down: taking cards back off would undo the
           very thing that was asked for. */
        recycling = false;
        var pending = articles.filter(function (article) {
            return !article.hidden;
        });
        var index = 0;

        return new Promise(function (resolve) {
            function step() {
                var deadline = Date.now() + 30;
                while (index < pending.length && Date.now() < deadline) {
                    mountArticle(pending[index]);
                    index++;
                }
                if (onProgress) {
                    onProgress(index, pending.length);
                }
                if (index < pending.length) {
                    setTimeout(step, 0);
                } else {
                    /* Give the last frames a moment to report their height. */
                    setTimeout(resolve, 600);
                }
            }
            step();
        });
    }

    var mountObserver = null;
    if (typeof IntersectionObserver === "function") {
        mountObserver = new IntersectionObserver(
            function (entries) {
                var touched = false;
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        mountArticle(entry.target);
                        touched = true;
                    }
                });
                if (touched) {
                    sweep();
                }
            },
            { rootMargin: LAZY_MARGIN }
        );
    }

    function track(article) {
        if (mountObserver) {
            mountObserver.observe(article);
        }
    }

    function untrack(article) {
        if (mountObserver) {
            mountObserver.unobserve(article);
        }
    }

    articles.forEach(track);

    /* The observer says when a card has come into reach. It says nothing about
       when one has drifted far enough away to be released -- a reader scrolling
       past a card gets no second event for it -- so the sweep runs off the
       scroll instead: once per frame, and over the handful of mounted cards
       rather than over the whole export. */
    var scrollPending = false;
    window.addEventListener(
        "scroll",
        function () {
            if (scrollPending) {
                return;
            }
            scrollPending = true;
            requestAnimationFrame(function () {
                scrollPending = false;
                if (mountObserver) {
                    sweep();
                } else {
                    mountVisible();
                }
            });
        },
        { passive: true }
    );

    /* --- controls --------------------------------------------------------- */

    function control(name) {
        return document.querySelector('[data-control="' + name + '"]');
    }

    function bindToggle(name, key, after) {
        var button = control(name);
        if (!button) {
            return;
        }
        button.addEventListener("click", function () {
            state[key] = !state[key];
            applyState();
            if (after) {
                after();
            }
        });
    }

    function syncControls() {
        [
            ["special", "special"],
            ["fields", "fields"],
            ["meta", "meta"],
            ["dark", "dark"],
            ["study", "study"],
            ["interactive", "interactive"]
        ].forEach(function (pair) {
            var button = control(pair[0]);
            if (button) {
                button.setAttribute("aria-pressed", state[pair[1]] ? "true" : "false");
            }
        });

        document.querySelectorAll("[data-mode]").forEach(function (button) {
            button.setAttribute(
                "aria-pressed",
                button.getAttribute("data-mode") === state.mode ? "true" : "false"
            );
        });

        document.querySelectorAll("[data-print-size]").forEach(function (button) {
            button.setAttribute(
                "aria-pressed",
                button.getAttribute("data-print-size") === state.printSize
                    ? "true"
                    : "false"
            );
        });

        document
            .querySelectorAll('.ahe-menu-panel input[data-field-name]')
            .forEach(function (input) {
                input.checked =
                    state.hiddenFields.indexOf(input.getAttribute("data-field-name")) === -1;
            });
        document
            .querySelectorAll('.ahe-menu-panel input[data-special-name]')
            .forEach(function (input) {
                input.checked =
                    state.hiddenSpecial.indexOf(
                        input.getAttribute("data-special-name")
                    ) === -1;
            });

        articles.forEach(syncCardToggles);
    }

    bindToggle("special", "special");
    bindToggle("fields", "fields");
    bindToggle("meta", "meta");
    bindToggle("dark", "dark", function () {
        broadcast({ type: "theme", night: state.dark });
    });
    bindToggle("study", "study", function () {
        if (!state.study) {
            /* Leaving study mode should not leave half the export marked as
               answered; the next round starts covered again. */
            articles.forEach(function (article) {
                article.classList.remove("ahe-revealed");
            });
            applyState();
        }
    });
    bindToggle("interactive", "interactive", function () {
        broadcast({ type: "interactive", on: state.interactive });
    });

    document.querySelectorAll("[data-mode]").forEach(function (button) {
        button.addEventListener("click", function () {
            var next = button.getAttribute("data-mode");
            if (next === state.mode) {
                return;
            }
            var wasTrimmed = trimsAnswer();
            state.mode = next;
            var trimChanged = trimsAnswer() !== wasTrimmed;
            if (trimChanged) {
                remountBacks();
            }
            applyState();
        });
    });

    document.querySelectorAll("[data-print-size]").forEach(function (button) {
        button.addEventListener("click", function () {
            state.printSize = button.getAttribute("data-print-size");
            applyState();
            saveState();
        });
    });

    /* Expand / collapse every field at once. Opening fields while the block
       itself is switched off would look like nothing happened, so turn it on. */
    document.querySelectorAll("[data-fields-expand]").forEach(function (button) {
        button.addEventListener("click", function () {
            var open = button.getAttribute("data-fields-expand") === "1";
            document.querySelectorAll(".ahe-field").forEach(function (field) {
                field.open = open;
            });
            if (open && !state.fields) {
                state.fields = true;
                applyState();
            }
        });
    });

    /* Field images are capped to a preview height; a click shows full size. */
    document.addEventListener("click", function (event) {
        var image = event.target;
        if (image.tagName === "IMG" && image.closest(".ahe-field-value")) {
            image.classList.toggle("ahe-zoom");
        }
    });

    /* Field / special-field check lists */
    document.addEventListener("change", function (event) {
        var input = event.target;
        var fieldName = input.getAttribute && input.getAttribute("data-field-name");
        var specialName = input.getAttribute && input.getAttribute("data-special-name");
        var list = null;
        var name = null;
        var block = null;
        if (fieldName !== null && fieldName !== undefined) {
            list = "hiddenFields";
            name = fieldName;
            block = "fields";
        } else if (specialName !== null && specialName !== undefined) {
            list = "hiddenSpecial";
            name = specialName;
            block = "special";
        }
        if (!list) {
            return;
        }
        /* Ticking an entry in a block that is switched off looks like a dead
           control, so switch the block on with it. */
        if (input.checked && !state[block]) {
            state[block] = true;
        }
        var hidden = state[list].slice();
        var index = hidden.indexOf(name);
        if (input.checked && index !== -1) {
            hidden.splice(index, 1);
        } else if (!input.checked && index === -1) {
            hidden.push(name);
        }
        state[list] = hidden;
        applyState();
    });

    document.addEventListener("click", function (event) {
        if (!event.target.closest) {
            return;
        }
        /* Clicks on the panel's own controls must not close it. */
        if (event.target.closest(".ahe-menu-panel")) {
            return;
        }
        var trigger = event.target.closest("[data-menu-toggle]");
        document.querySelectorAll(".ahe-menu").forEach(function (menu) {
            if (!trigger || menu !== trigger.parentElement) {
                menu.classList.remove("open");
            }
        });
        if (trigger) {
            var menu = trigger.parentElement;
            if (menu.classList.toggle("open")) {
                placeMenu(menu);
            }
        }
    });

    /* The tool line scrolls, which makes it a clipping container: a panel
       positioned inside it would be cut off at its edge. So the panels are
       fixed and put under their control by hand -- and on a narrow screen they
       span the width instead, because a 240 px box anchored to a button near
       the right edge would hang off the screen. */
    function placeMenu(menu) {
        var panel = menu.querySelector(".ahe-menu-panel");
        if (!panel) {
            return;
        }
        var anchor = menu.getBoundingClientRect();
        panel.style.top = Math.round(anchor.bottom + 6) + "px";
        if (window.innerWidth <= 720) {
            panel.style.left = "8px";
            panel.style.right = "8px";
            panel.style.width = "auto";
            return;
        }
        panel.style.right = "auto";
        panel.style.width = "";
        var width = panel.offsetWidth || 240;
        var left = Math.min(anchor.left, window.innerWidth - width - 12);
        panel.style.left = Math.max(8, Math.round(left)) + "px";
    }

    /* A panel placed against the old geometry would sit somewhere arbitrary. */
    window.addEventListener("resize", function () {
        document.querySelectorAll(".ahe-menu.open").forEach(function (menu) {
            menu.classList.remove("open");
        });
    });

    document.querySelectorAll("[data-menu-all]").forEach(function (button) {
        button.addEventListener("click", function () {
            var panel = button.closest(".ahe-menu-panel");
            var check = button.getAttribute("data-menu-all") === "on";
            panel.querySelectorAll("input[type=checkbox]").forEach(function (input) {
                if (input.checked !== check) {
                    input.checked = check;
                    input.dispatchEvent(new Event("change", { bubbles: true }));
                }
            });
        });
    });

    /* A link in a note field would replace the export itself, which is worse
       than what it does inside a card. Same rule, same reason; our own
       references are card anchors and start with a hash, so they pass. */
    document.addEventListener(
        "click",
        function (event) {
            var link = event.target.closest ? event.target.closest("a[href]") : null;
            if (!link) {
                return;
            }
            var href = link.getAttribute("href") || "";
            if (href === "" || href === "#") {
                event.preventDefault();
                return;
            }
            if (href.charAt(0) === "#" || /^javascript:/i.test(href)) {
                return;
            }
            event.preventDefault();
            window.open(link.href, "_blank", "noopener");
        },
        true
    );

    /* Tag overflow */
    document.addEventListener("click", function (event) {
        var toggle = event.target.closest
            ? event.target.closest("[data-tags-toggle]")
            : null;
        if (!toggle) {
            return;
        }
        var open = toggle.closest(".ahe-meta").classList.toggle("tags-open");
        toggle.textContent = open ? "less" : toggle.getAttribute("data-label");
    });

    /* Per-card toggles */
    document.addEventListener("click", function (event) {
        var button = event.target.closest
            ? event.target.closest("[data-card-toggle]")
            : null;
        if (!button) {
            return;
        }
        var article = button.closest(".ahe-card");
        var key = button.getAttribute("data-card-toggle");
        var on = key + "-on";
        var off = key + "-off";
        var byDefault = defaultVisible(article, key);

        if (article.classList.contains(on)) {
            article.classList.remove(on);
            if (byDefault) {
                article.classList.add(off);
            }
        } else if (article.classList.contains(off)) {
            article.classList.remove(off);
            if (!byDefault) {
                article.classList.add(on);
            }
        } else {
            article.classList.add(byDefault ? off : on);
        }
        applySides();
        syncCardToggles(article);
        mountArticle(article);
    });

    function defaultVisible(article, key) {
        if (key === "q" || key === "a") {
            if (state.study) {
                /* The chips have to say what is on screen, and in study mode
                   that is the front until the answer has been asked for. */
                return key === "q" || article.classList.contains("ahe-revealed");
            }
            return modeShows(cardsById[article.getAttribute("data-cid")], key);
        }
        return key === "s" ? state.special : state.fields;
    }

    function syncCardToggles(article) {
        article.querySelectorAll("[data-card-toggle]").forEach(function (button) {
            var key = button.getAttribute("data-card-toggle");
            var visible;
            if (article.classList.contains(key + "-on")) {
                visible = true;
            } else if (article.classList.contains(key + "-off")) {
                visible = false;
            } else {
                visible = defaultVisible(article, key);
            }
            button.setAttribute("aria-pressed", visible ? "true" : "false");
        });
    }

    /* --- study mode ------------------------------------------------------- */

    /* Study mode turns the export back into something you can be quizzed by:
       the front stands alone until the answer is asked for. Which card that
       applies to is whichever one the reader is looking at, so the shell keeps
       a cursor -- a card the keyboard acts on and the page scrolls to. */

    var cursor = -1;

    function focusCard(index, scroll) {
        if (!articles.length) {
            return;
        }
        var visible = articles.filter(function (article) {
            return !article.hidden;
        });
        if (!visible.length) {
            return;
        }
        var current = articles[cursor];
        var position = visible.indexOf(current);
        if (position === -1) {
            position = index > 0 ? -1 : 0;
        }
        var next = visible[Math.min(Math.max(position + index, 0), visible.length - 1)];
        if (index === 0 && current && visible.indexOf(current) !== -1) {
            next = current;
        }
        articles.forEach(function (article) {
            article.classList.remove("ahe-cursor");
        });
        next.classList.add("ahe-cursor");
        cursor = articles.indexOf(next);
        mountArticle(next);
        if (scroll) {
            next.scrollIntoView({ block: "start", behavior: "smooth" });
        }
    }

    function currentCard() {
        if (cursor >= 0 && articles[cursor] && !articles[cursor].hidden) {
            return articles[cursor];
        }
        /* Nothing picked yet: the card the reader is looking at is the first
           one whose top edge has not scrolled past the control bar. */
        var bar = document.querySelector(".ahe-bar");
        var top = bar ? bar.getBoundingClientRect().height : 0;
        for (var i = 0; i < articles.length; i++) {
            if (articles[i].hidden) {
                continue;
            }
            var box = articles[i].getBoundingClientRect();
            if (box.bottom > top + 4) {
                cursor = i;
                return articles[i];
            }
        }
        return null;
    }

    function revealCard(article, open) {
        if (!article) {
            return;
        }
        article.classList.toggle("ahe-revealed", open);
        applySides();
        syncCardToggles(article);
        mountArticle(article);
    }

    document.addEventListener("click", function (event) {
        var button = event.target.closest
            ? event.target.closest("[data-card-reveal]")
            : null;
        if (!button) {
            return;
        }
        var article = button.closest(".ahe-card");
        cursor = articles.indexOf(article);
        revealCard(article, true);
    });

    function studyKey(key) {
        if (key === "ArrowDown" || key === "ArrowRight") {
            focusCard(1, true);
            return true;
        }
        if (key === "ArrowUp" || key === "ArrowLeft") {
            focusCard(-1, true);
            return true;
        }
        if (key === " " || key === "Enter") {
            var article = currentCard();
            if (!article) {
                return false;
            }
            focusCard(0, false);
            if (state.study && !article.classList.contains("ahe-revealed")) {
                revealCard(article, true);
            } else {
                /* Already answered: move on, which is what a second press
                   means when you are working through a stack. */
                focusCard(1, true);
            }
            return true;
        }
        if (key === "Escape") {
            if (qrLayer) {
                closeQr();
                return true;
            }
            if (navPanel && navIsOpen()) {
                /* One panel, one meaning per press: close what is open before
                   touching what is revealed. */
                openNav(false);
                return true;
            }
            articles.forEach(function (article) {
                article.classList.remove("ahe-revealed");
            });
            broadcast({ type: "reveal", open: false });
            applySides();
            articles.forEach(syncCardToggles);
            return true;
        }
        return false;
    }

    function editableTarget(target) {
        if (!target || !target.tagName) {
            return false;
        }
        var tag = target.tagName.toLowerCase();
        return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
    }

    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey || editableTarget(event.target)) {
            return;
        }
        if (studyKey(event.key)) {
            event.preventDefault();
        }
    });

    /* --- search ----------------------------------------------------------- */

    var searchInput = control("search");
    var countLabel = control("count");

    function updateCount(visible) {
        var total = articles.length - putAway.length;
        var text = visible === total ? total + " cards" : visible + " / " + total + " cards";
        if (countLabel) {
            countLabel.textContent = text;
        }
        if (navCountLabel) {
            navCountLabel.textContent = text;
        }
    }

    function runSearch() {
        var query = (searchInput ? searchInput.value : "").trim().toLowerCase();
        var terms = query ? query.split(/\s+/) : [];
        var visible = 0;

        articles.forEach(function (article) {
            var card = cardsById[article.getAttribute("data-cid")];
            var haystack = card ? card.search : "";
            var match =
                !article.hasAttribute("data-dismissed") &&
                facetMatch(card) &&
                terms.every(function (term) {
                    return haystack.indexOf(term) !== -1;
                });
            article.hidden = !match;
            if (match) {
                visible++;
            }
        });

        updateCount(visible);
        document.querySelector(".ahe-empty").hidden = visible !== 0;
        mountVisible();
    }

    if (searchInput) {
        var searchTimer = null;
        searchInput.addEventListener("input", function () {
            clearTimeout(searchTimer);
            /* A live search is a round trip to Anki, so it waits a little
               longer than filtering text that is already on the page. */
            searchTimer = setTimeout(live ? liveScope : runSearch, live ? 350 : 120);
        });
    }

    /* --- navigation panel -------------------------------------------------- */

    /* The export is a fixed set of cards, so what a reader wants afterwards is
     * not a different query against the collection -- that would need Anki --
     * but a way to narrow down what is on the page. Deck and tag names are
     * hierarchical, so the panel shows them as trees: ticking a parent takes
     * everything below it with it, and every node carries how many cards it
     * stands for. */

    var facets = data.facets || {};

    function facetTable(name) {
        return facets[name] || [];
    }

    /* Tags are the one facet a card can have several of, and it can have none;
       -1 stands for that, so "untagged" is selectable like any other tag. */
    var NO_TAG = -1;

    function cardFacets(card) {
        return (card && card.facets) || null;
    }

    /* A tree built from "::"-separated names. Each node knows every facet
       index below it, which is what turns a tick into a filter and a subtree
       into a count. */
    function buildTree(names, countFor) {
        var root = { name: "", label: "", children: [], indices: [], count: 0 };
        var byPath = {};

        names.forEach(function (name, index) {
            var parts = String(name).split("::");
            var path = "";
            var parent = root;
            for (var i = 0; i < parts.length; i++) {
                path = path ? path + "::" + parts[i] : parts[i];
                var node = byPath[path];
                if (!node) {
                    node = {
                        name: path,
                        label: parts[i],
                        children: [],
                        indices: [],
                        count: 0
                    };
                    byPath[path] = node;
                    parent.children.push(node);
                }
                node.indices.push(index);
                parent = node;
            }
        });

        (function count(node) {
            node.count = node.indices.reduce(function (sum, index) {
                return sum + (countFor(index) || 0);
            }, 0);
            node.children.forEach(count);
        })(root);

        return root;
    }

    function flatTree(entries, countFor) {
        return {
            children: entries.map(function (entry) {
                return {
                    name: String(entry.value),
                    label: entry.label,
                    children: [],
                    indices: [entry.value],
                    count: countFor(entry.value) || 0,
                    swatch: entry.swatch
                };
            })
        };
    }

    /* Counts come from the cards themselves rather than from the tables, so a
       facet that no card in this export uses never shows up. */
    function tally(pick) {
        var counts = {};
        articles.forEach(function (article) {
            var card = cardsById[article.getAttribute("data-cid")];
            var value = card ? pick(cardFacets(card)) : null;
            if (value === null || value === undefined) {
                return;
            }
            if (Object.prototype.toString.call(value) === "[object Array]") {
                value.forEach(function (item) {
                    counts[item] = (counts[item] || 0) + 1;
                });
            } else {
                counts[value] = (counts[value] || 0) + 1;
            }
        });
        return function (index) {
            return counts[index] || 0;
        };
    }

    var FLAG_LABELS = [
        "No flag",
        "Red",
        "Orange",
        "Green",
        "Blue",
        "Pink",
        "Turquoise",
        "Purple"
    ];

    function sections() {
        var deckCount = tally(function (f) {
            return f && f.deck;
        });
        var tagCount = tally(function (f) {
            return f && (f.tags.length ? f.tags : [NO_TAG]);
        });
        var typeCount = tally(function (f) {
            return f && f.notetype;
        });
        var stateCount = tally(function (f) {
            return f && f.state;
        });
        var flagCount = tally(function (f) {
            return f && f.flag;
        });

        var tagTree = buildTree(facetTable("tags"), tagCount);
        if (tagCount(NO_TAG)) {
            tagTree.children.push({
                name: "untagged",
                label: "(no tags)",
                children: [],
                indices: [NO_TAG],
                count: tagCount(NO_TAG)
            });
        }

        function present(entries, countFor) {
            return entries.filter(function (entry) {
                return countFor(entry.value) > 0;
            });
        }

        return [
            {
                key: "decks",
                title: "Decks",
                tree: buildTree(facetTable("decks"), deckCount),
                match: function (f, chosen) {
                    return chosen[f.deck] === true;
                }
            },
            {
                key: "tags",
                title: "Tags",
                tree: tagTree,
                match: function (f, chosen) {
                    if (!f.tags.length) {
                        return chosen[NO_TAG] === true;
                    }
                    return f.tags.some(function (index) {
                        return chosen[index] === true;
                    });
                }
            },
            {
                key: "notetypes",
                title: "Note types",
                tree: flatTree(
                    present(
                        facetTable("notetypes").map(function (name, index) {
                            return { value: index, label: name };
                        }),
                        typeCount
                    ),
                    typeCount
                ),
                match: function (f, chosen) {
                    return chosen[f.notetype] === true;
                }
            },
            {
                key: "states",
                title: "Card state",
                tree: flatTree(
                    present(
                        facetTable("states").map(function (name, index) {
                            return { value: index, label: name || "unscheduled" };
                        }),
                        stateCount
                    ),
                    stateCount
                ),
                match: function (f, chosen) {
                    return chosen[f.state] === true;
                }
            },
            {
                key: "flags",
                title: "Flags",
                tree: flatTree(
                    present(
                        FLAG_LABELS.map(function (label, number) {
                            return {
                                value: number,
                                label: label,
                                swatch: number ? "ahe-flag-" + number : ""
                            };
                        }),
                        flagCount
                    ),
                    flagCount
                ),
                match: function (f, chosen) {
                    return chosen[f.flag] === true;
                }
            }
        ];
    }

    /* A facet with a single value says nothing about this export -- one deck,
       one note type -- so it is left out rather than shown as a dead switch.
       Counted over the whole tree rather than over its roots: an export taken
       from one shared deck has a single root and everything worth filtering by
       below it. */
    function nodeCount(node) {
        var total = 0;
        (node.children || []).forEach(function (child) {
            total += 1 + nodeCount(child);
        });
        return total;
    }

    /* Reassigned once the live view has asked the collection what there is. */
    var NAV = live
        ? []
        : sections().filter(function (section) {
              return nodeCount(section.tree) > 1;
          });

    /* Every card is tested against every active section on every keystroke in
       the search box, and an export can run to tens of thousands of cards --
       so the ticked indices are turned into lookup tables once per change
       rather than once per card. */
    var chosenCache = null;

    function chosenSets() {
        if (!chosenCache) {
            chosenCache = {};
            NAV.forEach(function (section) {
                var picked = state.filters[section.key] || [];
                var map = {};
                picked.forEach(function (index) {
                    map[index] = true;
                });
                chosenCache[section.key] = { map: map, size: picked.length };
            });
        }
        return chosenCache;
    }

    function chosenSet(key) {
        var entry = chosenSets()[key];
        return entry ? entry.map : {};
    }

    /* An untouched section restricts nothing, which is what lets the panel be
       used one facet at a time instead of having to be filled in first. */
    function facetMatch(card) {
        var f = cardFacets(card);
        if (!f) {
            return true;
        }
        var sets = chosenSets();
        for (var i = 0; i < NAV.length; i++) {
            var chosen = sets[NAV[i].key];
            if (!chosen || !chosen.size) {
                continue;
            }
            /* The live view's sections carry an Anki search term instead of a
               facet index, and the server has already applied it. */
            if (!NAV[i].match) {
                continue;
            }
            if (!NAV[i].match(f, chosen.map)) {
                return false;
            }
        }
        return true;
    }

    function nodeState(section, node) {
        var chosen = chosenSet(section.key);
        var picked = 0;
        node.indices.forEach(function (index) {
            if (chosen[index]) {
                picked++;
            }
        });
        if (!picked) {
            return "none";
        }
        return picked === node.indices.length ? "all" : "some";
    }

    function setNode(section, node, on) {
        var chosen = {};
        (state.filters[section.key] || []).forEach(function (index) {
            chosen[index] = true;
        });
        node.indices.forEach(function (index) {
            if (on) {
                chosen[index] = true;
            } else {
                delete chosen[index];
            }
        });
        /* Left as the strings object keys already are: an export ticks facet
           indices, the live view ticks deck and tag names. */
        state.filters[section.key] = Object.keys(chosen);
        chosenCache = null;
    }

    /* Tailwind utilities, kept as whole literal strings: the class scanner
       reads this file, and a class assembled from fragments would never be
       generated. Semantic names stay alongside them as the handles the code
       and the tests reach for. */
    var NAV_ROW =
        "ahe-nav-row group flex cursor-pointer items-center gap-2 py-[3px] pr-2.5 hover:bg-surface-2";
    var NAV_TWIST =
        "ahe-nav-twist w-4 shrink-0 cursor-pointer appearance-none border-0 bg-transparent p-0 " +
        "text-[10px] leading-none text-muted transition-transform duration-150 " +
        "hover:text-ink aria-expanded:rotate-90";
    var NAV_LEAF = "invisible cursor-default";
    var NAV_LABEL = "ahe-nav-label min-w-0 flex-1 truncate";
    var NAV_COUNT = "ahe-nav-num text-[11px] tabular-nums text-muted";
    var NAV_JUMP =
        "ahe-nav-jump invisible cursor-pointer appearance-none rounded border-0 bg-transparent " +
        "px-1.5 [font:inherit] text-muted group-hover:visible hover:bg-accent hover:text-surface";
    var NAV_SECTION =
        "ahe-nav-section sticky top-0 z-10 bg-surface px-3.5 pt-2.5 pb-1 text-[11px] " +
        "font-bold tracking-wider text-muted uppercase";

    var navPanel = document.querySelector(".ahe-nav");
    var navBackdrop = document.querySelector(".ahe-nav-backdrop");
    var navBody = navPanel ? navPanel.querySelector(".ahe-nav-body") : null;
    var navCountLabel = navPanel ? navPanel.querySelector(".ahe-nav-count") : null;
    var navNodes = [];

    /* A shared deck's tag tree can run to thousands of names six levels deep,
       so a node keeps its children folded away until asked. Rows are nested
       rather than laid out flat, which is what lets a whole subtree be hidden
       by hiding one element. */
    function renderNode(section, node, depth, into) {
        var wrapper = document.createElement("div");
        wrapper.className = "ahe-nav-node";

        var row = document.createElement("div");
        row.className = NAV_ROW;
        row.style.paddingLeft = 6 + depth * 14 + "px";

        var twist = document.createElement("button");
        twist.type = "button";
        twist.className = NAV_TWIST;
        /* The glyph is content rather than a ::before rule, so the utility
           that turns it can act on the element itself. */
        twist.textContent = "▶";
        if (node.children.length) {
            twist.setAttribute("aria-expanded", "false");
            twist.title = "Show what is below";
        } else {
            /* Leaves keep the space so names stay in one column. */
            twist.className += " " + NAV_LEAF;
            twist.tabIndex = -1;
            twist.setAttribute("aria-hidden", "true");
        }
        row.appendChild(twist);

        var box = document.createElement("input");
        box.type = "checkbox";
        row.appendChild(box);

        var label = document.createElement("span");
        label.className = NAV_LABEL;
        label.title = node.name;
        if (node.swatch) {
            var swatch = document.createElement("span");
            swatch.className = "ahe-nav-swatch mr-1.5 " + node.swatch;
            swatch.textContent = "⚑";
            label.appendChild(swatch);
        }
        label.appendChild(document.createTextNode(node.label));
        row.appendChild(label);

        if (section.counted !== false) {
            var count = document.createElement("span");
            count.className = NAV_COUNT;
            count.textContent = node.count;
            row.appendChild(count);
        }

        /* The panel is navigation as much as it is filtering: this goes to the
           first card the node stands for without changing what is shown. */
        var jump = document.createElement("button");
        jump.className = NAV_JUMP;
        jump.type = "button";
        jump.title = "Jump to the first card";
        jump.textContent = "→";
        jump.addEventListener("click", function (event) {
            event.stopPropagation();
            jumpTo(section, node);
        });
        row.appendChild(jump);

        function toggle() {
            setNode(section, node, nodeState(section, node) !== "all");
            applyFilters();
        }
        box.addEventListener("change", toggle);
        label.addEventListener("click", toggle);

        wrapper.appendChild(row);
        into.appendChild(wrapper);

        var entry = {
            section: section,
            node: node,
            depth: depth,
            box: box,
            row: row,
            twist: node.children.length ? twist : null,
            children: null,
            filled: false
        };
        navNodes.push(entry);

        if (node.children.length) {
            var children = document.createElement("div");
            children.className = "ahe-nav-children";
            children.hidden = true;
            wrapper.appendChild(children);
            entry.children = children;
            twist.addEventListener("click", function (event) {
                event.stopPropagation();
                expandNode(entry, children.hidden);
            });
            /* A node with something ticked below it opens itself, so a filter
               restored from the last visit is not hidden behind a triangle. */
            if (nodeState(section, node) !== "none") {
                expandNode(entry, true);
            }
        }
    }

    /* A branch's rows are built the first time it is opened. The live view's
       tag tree runs to eighty thousand nodes across sixteen levels; building
       that up front would cost more than everything else the page does. */
    function fillNode(entry) {
        if (entry.filled || !entry.children) {
            return;
        }
        entry.filled = true;
        entry.node.children.forEach(function (child) {
            renderNode(entry.section, child, entry.depth + 1, entry.children);
        });
        syncNav();
    }

    function expandNode(entry, open) {
        if (!entry.children) {
            return;
        }
        if (open) {
            fillNode(entry);
        }
        entry.children.hidden = !open;
        entry.twist.setAttribute("aria-expanded", open ? "true" : "false");
        entry.twist.title = open ? "Fold away" : "Show what is below";
    }

    /* A tick six levels down is not a tick anyone can see while the tree is
       folded, and in the live view a parent does not show it either -- its
       row stands for its own name alone, since deck:Parent covers the
       subdecks. So the branches above every ticked node are opened, as far
       down as the ticks go. Branches fill as they open, which appends to
       navNodes; the walk goes by index so it takes those rows in too. */
    function revealTicked() {
        for (var i = 0; i < navNodes.length; i++) {
            var entry = navNodes[i];
            if (!entry.children) {
                continue;
            }
            var below = entry.node.name + "::";
            var ticked = (state.filters[entry.section.key] || []).some(function (name) {
                return String(name).indexOf(below) === 0;
            });
            if (ticked) {
                expandNode(entry, true);
            }
        }
    }

    function expandAll(open) {
        if (!open) {
            navNodes.forEach(function (entry) {
                expandNode(entry, false);
            });
            return;
        }
        /* Opening fills branches, which appends to navNodes -- so walk by
           index rather than with forEach, which would stop at the rows that
           existed when it started. */
        for (var i = 0; i < navNodes.length; i++) {
            expandNode(navNodes[i], true);
        }
    }

    function jumpTo(section, node) {
        var chosen = {};
        node.indices.forEach(function (index) {
            chosen[index] = true;
        });
        for (var i = 0; i < articles.length; i++) {
            if (articles[i].hidden) {
                continue;
            }
            var f = cardFacets(cardsById[articles[i].getAttribute("data-cid")]);
            if (f && section.match(f, chosen)) {
                mountArticle(articles[i]);
                articles[i].scrollIntoView({ block: "start", behavior: "smooth" });
                return;
            }
        }
    }

    function treeSize(node) {
        return node.children.reduce(function (sum, child) {
            return sum + treeSize(child);
        }, node.children.length);
    }

    function renderNav() {
        if (!navBody) {
            return;
        }
        navBody.textContent = "";
        navNodes = [];

        /* Expanding a collection-sized tag tree would lock the page up for
           seconds, so above a certain size it is simply not on offer --
           folding away what you opened still is. */
        var nodes = NAV.reduce(function (sum, section) {
            return sum + treeSize(section.tree);
        }, 0);
        var expandButton = navPanel && navPanel.querySelector('[data-nav-expand="1"]');
        if (expandButton) {
            expandButton.hidden = nodes > 3000;
        }
        NAV.forEach(function (section) {
            var heading = document.createElement("div");
            heading.className = NAV_SECTION;
            heading.textContent = section.title;
            navBody.appendChild(heading);
            section.tree.children.forEach(function (child) {
                renderNode(section, child, 0, navBody);
            });
            /* One root is a section that says nothing until it is opened, and
               a panel of single rows is not a panel. */
            if (section.tree.children.length === 1) {
                var only = navNodes[navNodes.length - 1];
                if (only && only.children) {
                    expandNode(only, true);
                }
            }
        });
        revealTicked();
        syncNav();
    }

    function syncNav() {
        navNodes.forEach(function (entry) {
            var status = nodeState(entry.section, entry.node);
            entry.box.checked = status === "all";
            entry.box.indeterminate = status === "some";
            var label = entry.row.querySelector(".ahe-nav-label");
            label.classList.toggle("font-semibold", status !== "none");
            label.classList.toggle("text-accent", status !== "none");
        });
        var active = NAV.some(function (section) {
            return (state.filters[section.key] || []).length > 0;
        });
        var button = control("nav");
        if (button) {
            button.setAttribute("aria-pressed", active ? "true" : "false");
        }
    }

    function applyFilters() {
        syncNav();
        if (live) {
            liveScope();
        } else {
            runSearch();
        }
        saveState();
    }

    function navIsOpen() {
        return root.classList.contains("ahe-nav-open");
    }

    function openNav(open) {
        if (!navPanel) {
            return;
        }
        if (open && !navNodes.length) {
            /* Built before the panel starts moving, so the slide does not have
               to share a frame with the first render of the tree. */
            renderNav();
        }
        root.classList.toggle("ahe-nav-open", open);
        var button = control("nav");
        if (button) {
            button.setAttribute("aria-expanded", open ? "true" : "false");
        }
    }

    var navButton = control("nav");
    if (navButton) {
        if (!NAV.length) {
            /* One deck, one note type, no tags: nothing to narrow down. */
            navButton.hidden = true;
        }
        navButton.addEventListener("click", function () {
            openNav(!navIsOpen());
        });
    }
    if (navBackdrop) {
        navBackdrop.addEventListener("click", function () {
            openNav(false);
        });
    }
    if (navPanel) {
        navPanel
            .querySelector("[data-nav-close]")
            .addEventListener("click", function () {
                openNav(false);
            });
        navPanel.querySelectorAll("[data-nav-expand]").forEach(function (button) {
            button.addEventListener("click", function () {
                expandAll(button.getAttribute("data-nav-expand") === "1");
            });
        });
        navPanel
            .querySelector("[data-nav-reset]")
            .addEventListener("click", function () {
                NAV.forEach(function (section) {
                    state.filters[section.key] = [];
                });
                chosenCache = null;
                applyFilters();
            });
    }

    /* --- shuffling -------------------------------------------------------- */

    /* Working through a stack in the order it was exported teaches the order as
       much as the cards, so the page can deal them again. The cards themselves
       are moved rather than merely reordered by CSS: on paper and in the
       browser's own find-in-page, only the document order counts. */

    function unmountAll() {
        /* Without the remembered heights: a shuffle re-deals the cards and the
           print preparation relays them out, so the heights of a moment ago
           say nothing about the heights to come. */
        articles.forEach(function (article) {
            unmountArticle(article, false);
        });
    }

    /* The running number says where a card sits now, so it has to be rewritten
       -- including the copies the print layout puts on the details strip and
       the field block, which exist to attribute a stray fragment to its card. */
    function renumber() {
        var total = articles.length;
        articles.forEach(function (article, position) {
            var index = position + 1;
            var badge = article.querySelector(".ahe-index");
            if (badge && badge.firstChild) {
                badge.firstChild.nodeValue = String(index);
            }
            article.querySelectorAll(".ahe-block-ref").forEach(function (ref) {
                ref.textContent = ref.textContent.replace(
                    /\d+\s*\/\s*\d+/,
                    index + "/" + total
                );
            });
        });
    }

    function shuffleCards() {
        if (live) {
            /* Only a fraction of the scope is on the page, so dealing here
               would shuffle the first two dozen cards and leave the rest
               behind them in the collection's order. The server holds the
               whole scope and deals it there. */
            liveShuffle();
            return;
        }
        var order = articles.slice();
        for (var i = order.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var swap = order[i];
            order[i] = order[j];
            order[j] = swap;
        }

        /* Moving an iframe in the DOM reloads it in any case, so take the
           frames down first: a fresh deal is a fresh pass, and everything the
           reader had uncovered should be covered again anyway. */
        unmountAll();
        /* A fresh deal is a fresh pass, so what was uncovered is forgotten
           here on purpose -- unlike a card that merely scrolled out of sight. */
        revealStates = Object.create(null);
        articles.forEach(function (article) {
            article.classList.remove("ahe-revealed", "ahe-cursor");
        });
        cursor = -1;

        var main = document.querySelector("main");
        var empty = main.querySelector(".ahe-empty");
        order.forEach(function (article) {
            main.appendChild(article);
        });
        if (empty) {
            main.appendChild(empty);
        }

        articles = order;
        renumber();
        applySides();
        articles.forEach(syncCardToggles);
        /* Only ever reached in an export: runSearch() filters the cards on
           the page by their own text, and the live view's search box holds an
           Anki search -- "is:due", a deck term -- which appears in no card's
           text at all. */
        runSearch();
        window.scrollTo({ top: 0, behavior: "smooth" });
        if (articles.length <= AUTO_MOUNT_LIMIT) {
            setTimeout(function () {
                mountAll();
            }, 300);
        }
    }

    var shuffleButton = control("shuffle");
    if (shuffleButton) {
        shuffleButton.addEventListener("click", shuffleCards);
    }

    /* --- load all / print -------------------------------------------------- */

    var loadAllButton = control("load-all");
    if (loadAllButton) {
        loadAllButton.addEventListener("click", function () {
            var original = loadAllButton.textContent;
            loadAllButton.disabled = true;
            mountAll(function (done, total) {
                loadAllButton.textContent = "Loading " + done + "/" + total;
            }).then(function () {
                loadAllButton.textContent = original;
                loadAllButton.disabled = false;
            });
        });
    }

    var printButton = control("print");
    if (printButton) {
        printButton.addEventListener("click", function () {
            var original = printButton.textContent;
            printButton.disabled = true;
            printButton.textContent = "Preparing…";
            withPrintLayout(function () {
                printButton.textContent = original;
                printButton.disabled = false;
                window.print();
            });
        });
    }

    /* Ctrl+P skips the preparation the Print button does, so do here whatever
       can still be done synchronously. */
    window.addEventListener("beforeprint", function () {
        mountVisible();
        openFieldsForPrint();
    });
    window.addEventListener("afterprint", restoreFieldsAfterPrint);

    if (!live && articles.length <= AUTO_MOUNT_LIMIT) {
        setTimeout(function () {
            mountAll();
        }, 1200);
    }

    /* --- swiping a card away ----------------------------------------------- */

    /* On a phone a card that has been read is swiped off the page, left or
     * right, the way a message is: it slides out, the cards below close the
     * gap, and in the live view the next ones come up from the server. This is
     * for the session only -- nothing is written back to Anki and a reload
     * brings every card back -- so the one thing it needs beyond the gesture
     * is a way to take an accidental swipe back, which the short notice at the
     * bottom offers.
     *
     * Most of a card is a frame, and a touch inside it never reaches this
     * document; the frame recognises the swipe itself and reports it here.
     * The header and the toggles are ours, so the same recognition runs here
     * for them. Both feed the one gesture below. */

    var SWIPE_LOCK = 12; /* px of sideways travel before a swipe is one */
    var SWIPE_PART = 0.35; /* of the card's width, past which it goes */
    var SWIPE_FLING = 0.6; /* px/ms: a quick flick needs less distance */
    var SWIPE_OUT = 180; /* ms for the slide out, and again for the gap */
    var UNDO_FOR = 5000;

    var swipe = null;
    var putAway = []; /* what went, newest last, for undo */

    function swipeBegin(article) {
        if (swipe || !article || article.hidden || article.classList.contains("ahe-gone")) {
            return;
        }
        swipe = {
            article: article,
            width: article.offsetWidth || 1,
            dx: 0,
            at: performance.now(),
            speed: 0
        };
        article.classList.add("ahe-swiping");
    }

    function swipeMove(dx) {
        if (!swipe) {
            return;
        }
        var now = performance.now();
        var elapsed = now - swipe.at;
        if (elapsed > 0) {
            swipe.speed = (dx - swipe.dx) / elapsed;
        }
        swipe.at = now;
        swipe.dx = dx;
        swipe.article.style.transform = "translateX(" + dx + "px)";
        swipe.article.style.opacity = String(Math.max(0.25, 1 - Math.abs(dx) / swipe.width));
    }

    function swipeEnd(dx, cancelled) {
        if (!swipe) {
            return;
        }
        var gesture = swipe;
        if (typeof dx === "number") {
            swipeMove(dx);
        }
        swipe = null;
        gesture.article.classList.remove("ahe-swiping");
        var far = Math.abs(gesture.dx) > gesture.width * SWIPE_PART;
        var flung =
            Math.abs(gesture.speed) > SWIPE_FLING &&
            Math.abs(gesture.dx) > SWIPE_LOCK * 2 &&
            gesture.speed * gesture.dx > 0;
        if (!cancelled && (far || flung)) {
            dismissCard(gesture.article, gesture.dx < 0 ? -1 : 1, gesture.width);
        } else {
            settle(gesture.article);
        }
    }

    function settle(article) {
        article.classList.add("ahe-settling");
        article.style.transform = "";
        article.style.opacity = "";
        setTimeout(function () {
            article.classList.remove("ahe-settling");
        }, SWIPE_OUT);
    }

    function dismissCard(article, direction, width) {
        var height = article.getBoundingClientRect().height;
        article.classList.add("ahe-gone");
        article.style.height = height + "px";
        article.style.transform = "translateX(" + direction * (width + 48) + "px)";
        article.style.opacity = "0";
        setTimeout(function () {
            /* Out of sight; now the cards below close up over the space. */
            article.style.height = "0px";
            article.style.marginBottom = "0px";
            setTimeout(function () {
                takeOut(article);
            }, SWIPE_OUT);
        }, SWIPE_OUT);
    }

    function takeOut(article) {
        article.classList.remove("ahe-gone");
        article.style.transform = "";
        article.style.opacity = "";
        article.style.height = "";
        article.style.marginBottom = "";
        unmountArticle(article, false);
        if (articles[cursor] === article) {
            cursor = -1;
        }
        var index = articles.indexOf(article);
        if (live) {
            /* The page holds a window onto the scope, and the server counts
               in scope positions, so a card leaving the page changes nothing
               about what is asked for next. */
            untrack(article);
            articles.splice(index, 1);
            article.parentNode.removeChild(article);
            liveState.gone++;
            putAway.push({ article: article, index: index });
            liveCount();
            mountVisible();
            liveScroll();
        } else {
            article.setAttribute("data-dismissed", "");
            putAway.push({ article: article, index: index });
            runSearch();
        }
        offerUndo();
    }

    function takeBack() {
        var last = putAway.pop();
        if (!last) {
            return;
        }
        var article = last.article;
        if (live) {
            var index = Math.min(last.index, articles.length);
            var before = index < articles.length ? articles[index] : liveEmpty;
            liveMain.insertBefore(article, before);
            articles.splice(index, 0, article);
            track(article);
            liveState.gone--;
            liveCount();
            mountVisible();
        } else {
            article.removeAttribute("data-dismissed");
            runSearch();
        }
        article.scrollIntoView({ block: "nearest" });
        if (putAway.length) {
            offerUndo();
        } else {
            hideUndo();
        }
    }

    var undoBar = null;
    var undoTimer = null;

    function offerUndo() {
        if (!undoBar) {
            undoBar = document.createElement("div");
            undoBar.className = "ahe-undo";
            undoBar.setAttribute("role", "status");
            var text = document.createElement("span");
            text.className = "ahe-undo-text";
            undoBar.appendChild(text);
            var button = document.createElement("button");
            button.type = "button";
            button.setAttribute("data-control", "undo");
            button.textContent = "Undo";
            button.addEventListener("click", takeBack);
            undoBar.appendChild(button);
            document.body.appendChild(undoBar);
        }
        undoBar.querySelector(".ahe-undo-text").textContent =
            putAway.length === 1 ? "Card swiped away" : putAway.length + " cards swiped away";
        undoBar.hidden = false;
        clearTimeout(undoTimer);
        undoTimer = setTimeout(hideUndo, UNDO_FOR);
    }

    function hideUndo() {
        clearTimeout(undoTimer);
        if (undoBar) {
            undoBar.hidden = true;
        }
    }

    function forgetPutAway() {
        putAway = [];
        hideUndo();
    }

    /* The frame's report of a finger on the card side. */
    function frameSwipe(frame, payload) {
        var article = frame.closest(".ahe-card");
        if (payload.phase === "move") {
            if (!swipe) {
                swipeBegin(article);
            }
            if (swipe && swipe.article === article) {
                swipeMove(payload.dx);
            }
        } else if (swipe && swipe.article === article) {
            swipeEnd(payload.dx, payload.phase === "cancel");
        }
    }

    /* The same recognition for a finger on the card's own chrome -- header,
       side strip, toggles -- which is this document's to hear. */
    var chromeTouch = null;

    function scrollsSideways(target, dx) {
        var node = target;
        while (node && node !== document.documentElement) {
            if (node.nodeType === 1 && node.scrollWidth > node.clientWidth + 1) {
                var overflow = getComputedStyle(node).overflowX;
                if (overflow === "auto" || overflow === "scroll") {
                    if (dx < 0 && node.scrollLeft + node.clientWidth < node.scrollWidth - 1) {
                        return true;
                    }
                    if (dx > 0 && node.scrollLeft > 0) {
                        return true;
                    }
                }
            }
            node = node.parentNode;
        }
        return false;
    }

    document.addEventListener(
        "touchstart",
        function (event) {
            var article = event.target.closest ? event.target.closest(".ahe-card") : null;
            if (!article || event.touches.length !== 1) {
                chromeTouch = null;
                return;
            }
            var point = event.touches[0];
            chromeTouch = {
                article: article,
                target: event.target,
                x: point.clientX,
                y: point.clientY,
                swiping: false,
                dead: false
            };
        },
        { passive: true }
    );

    document.addEventListener(
        "touchmove",
        function (event) {
            if (!chromeTouch || chromeTouch.dead || event.touches.length !== 1) {
                return;
            }
            var point = event.touches[0];
            var dx = point.clientX - chromeTouch.x;
            var dy = point.clientY - chromeTouch.y;
            if (!chromeTouch.swiping) {
                if (Math.abs(dy) >= SWIPE_LOCK && Math.abs(dy) >= Math.abs(dx)) {
                    chromeTouch.dead = true;
                    return;
                }
                if (Math.abs(dx) < SWIPE_LOCK || Math.abs(dx) < Math.abs(dy) * 1.5) {
                    return;
                }
                if (scrollsSideways(chromeTouch.target, dx)) {
                    chromeTouch.dead = true;
                    return;
                }
                chromeTouch.swiping = true;
                swipeBegin(chromeTouch.article);
            }
            if (swipe && swipe.article === chromeTouch.article) {
                event.preventDefault();
                swipeMove(dx);
            }
        },
        { passive: false }
    );

    function endChromeTouch(event) {
        if (chromeTouch && chromeTouch.swiping && swipe && swipe.article === chromeTouch.article) {
            var point = event.changedTouches && event.changedTouches[0];
            swipeEnd(point ? point.clientX - chromeTouch.x : swipe.dx, event.type !== "touchend");
        }
        chromeTouch = null;
    }

    document.addEventListener("touchend", endChromeTouch, { passive: true });
    document.addEventListener("touchcancel", endChromeTouch, { passive: true });

    /* --- live view -------------------------------------------------------- */

    /* Served by the add-on while Anki runs, the page is no longer a fixed set
     * of cards: the panel offers every deck and tag in the collection, and the
     * scope is whatever Anki's own search says it is right now. Nothing is
     * copied anywhere -- the cards arrive as the reader scrolls, and pictures
     * are served straight out of collection.media.
     *
     * The markup is the same markup the export writes, rendered by the same
     * Python and handed over as fragments, so everything built on it -- the
     * per-card toggles, study mode, shuffling, the print layout -- works here
     * without a second implementation. */

    var LIVE_PAGE = 25;
    var LIVE_REACH = 1500; /* px from the bottom at which the next page loads */

    var liveState = {
        query: "",
        total: 0,
        loaded: 0,
        loading: false,
        exhausted: false,
        gone: 0 /* swiped off the page since the scope was loaded */
    };

    function liveUrl(path, params) {
        var query = Object.keys(params || {})
            .filter(function (key) {
                return params[key] !== undefined && params[key] !== "";
            })
            .map(function (key) {
                return encodeURIComponent(key) + "=" + encodeURIComponent(params[key]);
            })
            .join("&");
        return path + (query ? "?" + query : "");
    }

    function liveFetch(path, params) {
        /* The key rides along as a cookie, set when the page was served. */
        return fetch(liveUrl(path, params), { credentials: "same-origin" }).then(
            function (response) {
                if (!response.ok) {
                    return response.text().then(function (text) {
                        throw new Error(text || response.statusText);
                    });
                }
                return response.json();
            }
        );
    }

    /* Building an Anki search out of what is ticked ------------------------- */

    /* Inside the quotes Anki still reads the backslash, the quote and the
       wildcards * and _ specially. */
    function quoteTerm(value, suffix) {
        return '"' + String(value).replace(/[\\"*_]/g, "\\$&") + (suffix || "") + '"';
    }

    function liveQuery() {
        var parts = [];
        NAV.forEach(function (section) {
            var picked = state.filters[section.key] || [];
            if (!picked.length || !section.term) {
                return;
            }
            var terms = picked.map(section.term);
            parts.push(terms.length === 1 ? terms[0] : "(" + terms.join(" OR ") + ")");
        });
        var typed = (searchInput ? searchInput.value : "").trim();
        if (typed) {
            parts.push("(" + typed + ")");
        }
        return parts.join(" ");
    }

    /* The panel, filled from the collection rather than from the payload ---- */

    function liveSections(tree) {
        /* An export ticks facet indices, because a card carries indices. Here
           a tick becomes a term in an Anki search, so a node stands for its own
           name -- and only its own: "deck:Parent" already covers the subdecks,
           and the tag term below says both halves explicitly. Without this a
           parent would expand into one term per descendant. */
        function identify(node) {
            node.indices = [node.name];
            node.children.forEach(identify);
        }

        /* Sections carry counted: false -- a nought against every deck would
           read as "this deck is empty", and a real count would mean a query
           per node across seventy thousand tags. */
        function plain(names) {
            /* No counts: a count per node would be a query per node, and this
               collection has seventy thousand tags. */
            var tree = buildTree(names, function () {
                return 0;
            });
            tree.children.forEach(identify);
            return tree;
        }

        return [
            {
                counted: false,
                key: "decks",
                title: "Decks",
                tree: plain(tree.decks || []),
                /* deck: already covers subdecks, so a parent needs one term. */
                term: function (name) {
                    return "deck:" + quoteTerm(name);
                }
            },
            {
                counted: false,
                key: "tags",
                title: "Tags",
                tree: plain(tree.tags || []),
                /* tag:x does not match x::y, so a branch has to say both. */
                term: function (name) {
                    return (
                        "(tag:" + quoteTerm(name) + " OR tag:" + quoteTerm(name, "::*") + ")"
                    );
                }
            },
            {
                counted: false,
                key: "notetypes",
                title: "Note types",
                tree: plain(tree.notetypes || []),
                term: function (name) {
                    return "note:" + quoteTerm(name);
                }
            }
        ];
    }

    /* The scope the page was opened on ------------------------------------- */

    /* A scope comes in through the address -- from the export dialog, or a
       link handed on. Its deck and tag terms become ticks in the panel and
       only the rest stays typed: the panel is then in charge, and a tick
       replaces what the dialog chose rather than fighting it. The forms the
       dialog and this page write, and the bare one, are all read. */
    var TERM_RE = /"(deck|tag):((?:[^"\\]|\\.)+)"|(deck|tag):"((?:[^"\\]|\\.)+)"|(deck|tag):([^\s()"]+)/g;

    function unescapeTerm(text) {
        return text.replace(/\\(.)/g, "$1");
    }

    function liftSeededTerms(tree) {
        if (!searchInput) {
            return;
        }
        var typed = searchInput.value.trim();
        if (!typed) {
            return;
        }
        var decks = (tree && tree.decks) || [];
        var tags = (tree && tree.tags) || [];
        var rest = typed;
        var lifted = false;
        rest = rest.replace(TERM_RE, function (whole, k1, v1, k2, v2, k3, v3) {
            var kind = k1 || k2 || k3;
            var value = unescapeTerm(v1 || v2 || v3 || "");
            /* The page's own tag term says a branch twice; the second half
               is the same tick. */
            if (kind === "tag" && /::\*$/.test(value)) {
                value = value.replace(/::\*$/, "");
            }
            var known = kind === "deck" ? decks : tags;
            if (known.indexOf(value) === -1) {
                return whole;
            }
            var key = kind === "deck" ? "decks" : "tags";
            state.filters[key] = state.filters[key] || [];
            if (state.filters[key].indexOf(value) === -1) {
                state.filters[key].push(value);
            }
            lifted = true;
            return " ";
        });
        if (!lifted) {
            return;
        }
        /* What is left once the terms are out: a bare OR, empty brackets, the
           joins the page itself writes between them. */
        rest = rest.replace(/\(\s*(?:OR\s*)*\)/g, " ").replace(/\bOR\s+OR\b/g, "OR")
            .replace(/^\s*OR\s+|\s+OR\s*$/g, " ").replace(/\(\s*OR\s+/g, "(").replace(/\s+OR\s*\)/g, ")")
            .replace(/\(\s*\)/g, " ").replace(/\s+/g, " ").trim();
        /* The dialog brackets its extra search; alone, the brackets say nothing */
        var wrapped = /^\(([^()]*)\)$/.exec(rest);
        if (wrapped) {
            rest = wrapped[1].trim();
        }
        searchInput.value = rest;
        chosenCache = null;
    }

    /* Cards ---------------------------------------------------------------- */

    var liveMain = document.querySelector("main");
    var liveEmpty = document.querySelector(".ahe-empty");

    function liveReset() {
        articles.forEach(function (article) {
            untrack(article);
            unmountArticle(article, false);
            article.parentNode.removeChild(article);
        });
        articles = [];
        Object.keys(frames).forEach(function (id) {
            delete frames[id];
        });
        cursor = -1;
        liveState.loaded = 0;
        liveState.exhausted = false;
        liveState.gone = 0;
        forgetPutAway();
    }

    function liveAppend(result) {
        Object.keys(result.css || {}).forEach(function (ntid) {
            data.css[ntid] = result.css[ntid];
        });

        var holder = document.createElement("div");
        holder.innerHTML = result.cards
            .map(function (card) {
                return card.html;
            })
            .join("");

        result.cards.forEach(function (card) {
            cardsById[card.payload.id] = card.payload;
        });

        var added = Array.prototype.slice.call(holder.querySelectorAll(".ahe-card"));
        added.forEach(function (article) {
            liveMain.insertBefore(article, liveEmpty);
            articles.push(article);
            track(article);
        });

        liveState.loaded += result.cards.length;
        if (!result.cards.length || liveState.loaded >= liveState.total) {
            liveState.exhausted = true;
        }

        applySides();
        added.forEach(syncCardToggles);
        applyHiddenNames();
        mountVisible();
        liveCount();

        /* A page of cards may not fill the window -- placeholders are short,
           and the frames only grow once they have rendered. Waiting for a
           scroll event that will never come would leave the view half empty,
           so ask again from the geometry rather than from an event. */
        setTimeout(liveScroll, 50);
    }

    function liveCount() {
        var text;
        var total = liveState.total - liveState.gone;
        if (!liveState.query) {
            text = "no scope selected";
        } else if (liveState.loaded >= liveState.total) {
            text = total + " cards";
        } else {
            text = liveState.loaded - liveState.gone + " of " + total + " cards";
        }
        if (countLabel) {
            countLabel.textContent = text;
        }
        if (navCountLabel) {
            navCountLabel.textContent = text;
        }
        liveEmpty.hidden = !(liveState.query && liveState.total === 0);
        if (liveEmpty.hidden === false) {
            liveEmpty.textContent = "No card matches this scope.";
        }
    }

    function livePage() {
        if (liveState.loading || liveState.exhausted || !liveState.query) {
            return Promise.resolve();
        }
        liveState.loading = true;
        return liveFetch("/api/cards", {
            offset: liveState.loaded,
            limit: LIVE_PAGE,
            /* Which scope this page belongs to, so the answer does not depend
               on the server still being the one that was asked for it. */
            q: liveState.query,
            have: Object.keys(data.css).join(",")
        })
            .then(function (result) {
                liveAppend(result);
            })
            .catch(function (error) {
                liveProblem(error);
                liveState.exhausted = true;
            })
            .then(function () {
                liveState.loading = false;
            });
    }

    /* --- the page's own address, as a code -------------------------------- */

    /* Reading on a desktop and wanting to go on reading on a phone is the whole
       point of the network share, and typing out an address with a random key
       in it by hand is not something anyone does twice. The code is drawn by
       the add-on, so it is available wherever the view is -- including here,
       without going back to Anki for it. */

    /* How long to wait before asking again while the view is not shared. The
       switch for that is in Anki, and this window is told nothing when it is
       flipped -- so the only way for the message to go away by itself is to
       keep asking. */
    var QR_RETRY = 2000;

    var qrLayer = null;
    var qrRetry = null;

    function closeQr() {
        if (qrRetry) {
            clearTimeout(qrRetry);
            qrRetry = null;
        }
        if (qrLayer && qrLayer.parentNode) {
            qrLayer.parentNode.removeChild(qrLayer);
        }
        qrLayer = null;
        var button = control("qr");
        if (button) {
            button.setAttribute("aria-expanded", "false");
        }
    }

    function qrMessage(text) {
        if (!qrLayer) {
            return;
        }
        var card = qrLayer.querySelector(".ahe-qr-card");
        card.textContent = "";
        var line = document.createElement("p");
        line.textContent = text;
        card.appendChild(line);
    }

    function drawQr() {
        if (qrRetry) {
            clearTimeout(qrRetry);
            qrRetry = null;
        }
        if (!qrLayer) {
            return;
        }
        liveFetch((data.meta && data.meta.qrPath) || "/api/qr", { q: liveState.query })
            .then(function (result) {
                if (!qrLayer) {
                    return; /* closed while it was being drawn */
                }
                if (!result.shared) {
                    qrMessage(
                        result.error ||
                            "This page is only being served to this machine. Switch " +
                            "on “Also reachable from this network” in Anki and the " +
                            "code appears here."
                    );
                    /* The narrator can open the network from here: its share
                       is a setting of its own, not the dialog's. */
                    if (result.enable) {
                        var open = document.createElement("button");
                        open.className = "ahe-qr-open";
                        open.textContent = "Open to the local network";
                        open.addEventListener("click", function (event) {
                            event.stopPropagation();
                            fetch(result.enable, { method: "POST", credentials: "same-origin",
                                headers: { "Content-Type": "application/json" }, body: JSON.stringify({ share: true }) })
                                .then(function () { qrMessage("Opening…"); });
                        });
                        qrLayer.querySelector(".ahe-qr-card").appendChild(open);
                    }
                    qrRetry = setTimeout(drawQr, QR_RETRY);
                    return;
                }
                var card = qrLayer.querySelector(".ahe-qr-card");
                card.innerHTML = result.svg;
                var address = document.createElement("p");
                address.className = "ahe-qr-url";
                address.textContent = result.url;
                card.appendChild(address);
                if (result.note) {
                    var note = document.createElement("p");
                    note.className = "ahe-qr-note";
                    note.textContent = result.note;
                    if (result.cert) {
                        var link = document.createElement("a");
                        link.href = result.cert;
                        link.textContent = " Install the certificate.";
                        link.addEventListener("click", function (event) { event.stopPropagation(); });
                        note.appendChild(link);
                    }
                    card.appendChild(note);
                }
            })
            .catch(function (error) {
                qrMessage(String(error && error.message ? error.message : error));
                qrRetry = setTimeout(drawQr, QR_RETRY);
            });
    }

    function showQr() {
        closeQr();
        qrLayer = document.createElement("div");
        qrLayer.className = "ahe-qr";
        qrLayer.setAttribute("role", "dialog");
        qrLayer.innerHTML = '<div class="ahe-qr-card"></div>';
        /* Anywhere outside it closes it; there is nothing to do in here but
           point a camera. */
        qrLayer.addEventListener("click", closeQr);
        document.body.appendChild(qrLayer);
        qrMessage("…");
        var button = control("qr");
        if (button) {
            button.setAttribute("aria-expanded", "true");
        }
        drawQr();
    }

    /* Coming back from Anki is the likeliest moment for the answer to have
       changed, so take it rather than waiting out the retry. */
    window.addEventListener("focus", drawQr);

    function liveShuffle() {
        if (!liveState.query) {
            return Promise.resolve();
        }
        return liveFetch("/api/shuffle", { q: liveState.query })
            .then(function (result) {
                /* A fresh deal is a fresh pass, here as in an export. */
                revealStates = Object.create(null);
                liveReset();
                liveState.total = result.total;
                liveCount();
                window.scrollTo({ top: 0, behavior: "smooth" });
                return livePage();
            })
            .catch(liveProblem);
    }

    function liveScope() {
        var query = liveQuery();
        liveState.query = query;
        if (narrator) {
            if (window.AHE_NARRATOR) {
                window.AHE_NARRATOR.scope(query);
            } else {
                /* Not there yet: the narrator's script starts after this one
                   and asks for the scope it finds here. */
                window.AHE_PENDING_SCOPE = query;
            }
            return Promise.resolve();
        }
        if (!query) {
            liveReset();
            liveState.total = 0;
            liveCount();
            return Promise.resolve();
        }
        return liveFetch("/api/scope", { q: query })
            .then(function (result) {
                liveReset();
                liveState.total = result.total;
                liveCount();
                return livePage();
            })
            .catch(liveProblem);
    }

    function liveProblem(error) {
        liveEmpty.hidden = false;
        liveEmpty.textContent = String(error && error.message ? error.message : error);
    }

    function liveScroll() {
        var remaining =
            document.documentElement.scrollHeight -
            (window.scrollY + window.innerHeight);
        if (remaining < LIVE_REACH) {
            livePage();
        }
    }

    function startLive() {
        document.documentElement.classList.add("ahe-live");
        window.addEventListener("scroll", liveScroll, { passive: true });

        /* In the live view the search box is Anki's search, not a filter over
           text that is already on the page. */
        /* Only the live view has an address to hand out; an export is a file. */
        var qrButton = control("qr");
        if (qrButton) {
            qrButton.hidden = false;
            qrButton.addEventListener("click", function () {
                if (qrLayer) {
                    closeQr();
                } else {
                    showQr();
                }
            });
        }

        if (searchInput) {
            searchInput.placeholder = "Anki search — is:due, -tag:leech, added:30…";
            searchInput.setAttribute("aria-label", "Anki search");
            /* A scope can come in through the address, which is what makes a
               link worth sharing: it opens on the cards it was meant to show
               rather than on an empty page. */
            var seeded = /[?&]q=([^&]*)/.exec(location.search);
            if (seeded) {
                searchInput.value = decodeURIComponent(seeded[1].replace(/\+/g, " "));
            }
        }

        liveFetch("/api/tree")
            .then(function (tree) {
                NAV = liveSections(tree);
                liftSeededTerms(tree);
                chosenCache = null;
                NAV.forEach(function (section) {
                    if (!state.filters[section.key]) {
                        state.filters[section.key] = [];
                    }
                });
                var button = control("nav");
                if (button) {
                    button.hidden = false;
                }
                renderNav();
                return liveScope();
            })
            .catch(liveProblem);
    }

    /* --- go --------------------------------------------------------------- */

    applyState();
    syncNav();
    if (live) {
        startLive();
    } else {
        runSearch();
    }

    if (location.hash) {
        var target = document.querySelector(location.hash);
        if (target) {
            mountArticle(target);
            target.scrollIntoView();
        }
    }
})();
