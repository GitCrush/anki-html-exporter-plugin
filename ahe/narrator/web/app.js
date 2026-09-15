/* Anki Narrator -- the slide show.
 *
 * Cards come from the server a page at a time, rendered by Anki; each is
 * mounted into a frame of its own (Anki's reviewer stylesheet, the note
 * type's stylesheet, the frame script that reports the frame's height). A
 * card is shown the way Anki shows it: its front first, then -- a few
 * seconds into the narration, or on Enter -- its back, with the cloze asked
 * lifted out. The narration tells the whole card; the next card follows
 * when it ends.
 */
(function () {
    "use strict";

    var PAGE = 25;
    /* Narrations the page itself asks for ahead of the reader, and how many
       the server is told to make ready beyond that. */
    var PREFETCH = 2;
    var LOOKAHEAD = 6;

    var $ = function (id) { return document.getElementById(id); };
    /* The bar's controls belong to the shell's markup and are found by name */
    var control = function (name) { return document.querySelector('[data-control="' + name + '"]'); };
    var el = {
        order: control("order"), seconds: control("seconds"), voice: control("voice"), speed: control("speed"),
        count: control("count"), settingsOpen: control("settings"), exportOpen: control("export"), usage: control("usage"),
        main: $("main"), stage: $("stage"), empty: $("empty"), card: $("card"),
        sideQ: $("side-q"), sideA: $("side-a"),
        script: $("script"), scriptState: $("script-state"), scriptText: $("script-text"),
        scriptToggle: $("script-toggle"), regen: $("regen"),
        prev: $("prev"), play: $("play"), reveal: $("reveal"), next: $("next"), progress: $("progress"),
        progressBar: $("progress-bar"), time: $("time"), counter: $("counter"), autoplay: $("autoplay"),
        audio: $("audio"), settings: $("settings"), settingsForm: $("settings-form"),
        settingsCancel: $("settings-cancel"), keyHint: $("key-hint"),
        usageRows: $("usage-rows"), usageNote: $("usage-note"), usageReset: $("usage-reset"),
        usageDetails: $("usage-details"),
        exportDialog: $("export"), exportForm: $("export-form"), exportScope: $("export-scope"),
        exportEstimate: $("export-estimate"), exportCost: $("export-cost"), exportMissingRow: $("export-missing-row"),
        exportNightRow: $("export-night-row"), exportProgress: $("export-progress"), exportBar: $("export-bar"),
        exportPhase: $("export-phase"), exportResult: $("export-result"), exportClose: $("export-close"),
        exportCancel: $("export-cancel"), exportStart: $("export-start"),
        chat: $("chat"), chatLog: $("chat-log"), chatError: $("chat-error"), chatForm: $("chat-form"),
        chatInput: $("chat-input"), chatSend: $("chat-send"), chatAudio: $("chat-audio"), mic: $("mic"), micHint: $("mic-hint")
    };
    var SPEEDS = [1, 1.25, 1.5, 1.75, 2];

    var state = {
        config: null,
        shared: null,          /* assets every frame is built with */
        css: {},               /* note type id -> stylesheet */
        query: "",
        total: 0,
        cards: [],             /* payloads in order; holes until fetched */
        pagesLoading: {},
        index: -1,
        playing: false,
        narrations: {},        /* cache key -> promise of narration */
        gapTimer: null,
        showing: null,         /* card id whose narration is on the deck */
        phase: "q",            /* which side is on screen: q before the reveal, a after */
        revealTimer: null,
        awaitingReveal: false, /* the reveal waits for a key */
        usage: null,           /* the ledger's last snapshot */
        language: ""           /* of the narration on screen */
    };

    /* --- helpers ---------------------------------------------------------- */

    function api(path, options) {
        return fetch(path, options).then(function (response) {
            return response.json().then(function (body) {
                if (!response.ok) {
                    throw new Error(body.error || response.statusText);
                }
                return body;
            });
        });
    }

    /* The bar's count label is the one line of status the page has */
    function setStatus(text, kind) {
        if (!el.count) { return; }
        el.count.textContent = text;
        el.count.title = text;
        el.count.classList.toggle("is-bad", kind === "bad");
    }

    function isDark() { return document.documentElement.classList.contains("ahe-dark"); }

    function fmtTime(seconds) {
        seconds = Math.max(0, Math.round(seconds || 0));
        return Math.floor(seconds / 60) + ":" + ("0" + (seconds % 60)).slice(-2);
    }

    function remember(key, value) {
        try { localStorage.setItem("narrator." + key, JSON.stringify(value)); } catch (e) {}
    }
    function recall(key, fallback) {
        try {
            var raw = localStorage.getItem("narrator." + key);
            return raw === null ? fallback : JSON.parse(raw);
        } catch (e) { return fallback; }
    }

    /* --- boot ------------------------------------------------------------- */

    function boot() {
        el.autoplay.checked = recall("autoplay", true);
        if (recall("scriptHidden", false)) { el.main.classList.add("script-hidden"); el.scriptToggle.textContent = "show"; }

        api("/api/narrator/usage").then(showUsage).catch(function () {});
        api("/api/narrator/config").then(function (config) {
            state.config = config;
            fillOptions(config);
            return refreshStatus();
        }).then(function (ok) {
            if (!ok) { return; }
            /* The shell composes the scope -- its panel, its search -- and
               says so here; whatever it said before this script ran is
               taken up now. */
            window.AHE_NARRATOR = { scope: function (query) { loadScope(query); } };
            if (window.AHE_PENDING_SCOPE !== undefined) {
                loadScope(window.AHE_PENDING_SCOPE);
                delete window.AHE_PENDING_SCOPE;
            }
        });
    }

    /* The selects are written by the server with the config in them; the
       audio follows the speed. */
    function fillOptions(config) {
        if (el.seconds && !el.seconds.value) { el.seconds.value = config.seconds; }
        el.audio.playbackRate = Number(el.speed ? el.speed.value : 1);
    }

    function refreshStatus() {
        return api("/api/narrator/status").then(function (status) {
            if (!status.collection) {
                setStatus("Anki has no collection open", "bad");
                return false;
            }
            if (!status.has_key) {
                setStatus("No OpenAI key yet — add one under ⚙", "bad");
            } else {
                setStatus("Ready", "ok");
            }
            return loadShared().then(function () { return true; });
        }).catch(function (err) {
            setStatus(err.message, "bad");
            return false;
        });
    }

    function loadShared() {
        if (state.shared) { return Promise.resolve(); }
        return api("/api/narrator/shared").then(function (data) { state.shared = data; });
    }

    /* --- scope ------------------------------------------------------------ */

    function loadScope(query) {
        query = (query || "").trim();
        stopAudio();
        state.query = query;
        state.cards = [];
        state.pagesLoading = {};
        state.index = -1;
        state.total = 0;
        api("/api/narrator/config", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ last_search: query })
        }).catch(function () {});
        if (!query) {
            showEmpty();
            return;
        }
        setStatus("Loading…");
        loadPage(0).then(function () {
            if (!state.total) {
                setStatus("No card matches that search", "bad");
                showEmpty();
                return;
            }
            setStatus(state.total + " cards", "ok");
            show(0);
        }).catch(function (err) { setStatus(err.message, "bad"); });
    }

    function loadPage(page) {
        if (state.pagesLoading[page]) { return state.pagesLoading[page]; }
        var query = state.query;
        var have = Object.keys(state.css).join(",");
        var promise = api("/api/narrator/cards?q=" + encodeURIComponent(query) + "&offset=" + (page * PAGE) +
            "&limit=" + PAGE + "&have=" + encodeURIComponent(have))
            .then(function (result) {
                if (state.query !== query) { return; }
                state.total = result.total;
                Object.keys(result.css || {}).forEach(function (ntid) { state.css[ntid] = result.css[ntid]; });
                (result.cards || []).forEach(function (card, i) {
                    state.cards[result.offset + i] = card;
                });
                (result.errors || []).forEach(function (e) { console.warn(e); });
            });
        state.pagesLoading[page] = promise;
        return promise;
    }

    function ensureCard(index) {
        if (state.cards[index]) { return Promise.resolve(state.cards[index]); }
        return loadPage(Math.floor(index / PAGE)).then(function () { return state.cards[index]; });
    }

    /* --- showing a card --------------------------------------------------- */

    function showEmpty() {
        el.empty.hidden = false;
        el.card.hidden = true;
        el.chat.hidden = true;
        el.counter.textContent = "0 / 0";
        el.scriptText.textContent = "";
        el.scriptState.textContent = "—";
    }

    function show(index) {
        if (index < 0 || index >= state.total) { return; }
        clearTimeout(state.gapTimer);
        stopAudio();
        state.index = index;
        el.counter.textContent = (index + 1) + " / " + state.total;
        ensureCard(index).then(function (card) {
            if (state.index !== index || !card) { return; }
            el.empty.hidden = true;
            el.card.hidden = false;
            setPhase(card, "q");
            el.stage.scrollTop = 0;
            stopRecording();
            el.chatAudio.pause();
            showChat(card);
            narrate(card).then(function (narration) {
                if (state.index !== index) { return; }
                present(card, narration);
            }).catch(function (err) {
                if (state.index !== index) { return; }
                el.scriptState.textContent = "narration failed";
                el.scriptText.className = "script-text error";
                el.scriptText.textContent = err.message;
                el.regen.hidden = false;
                /* Nothing to say still shows the whole card */
                setPhase(card, "a");
            });
            for (var ahead = 1; ahead <= PREFETCH; ahead++) { prefetch(index + ahead); }
            lookahead(index + 1);
        });
    }

    function escapeHtml(text) {
        return String(text).replace(/[&<>"]/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
        });
    }

    /* Which sides are on screen. Before the reveal, the front alone -- the
       question, the text with its gap -- as Anki shows it. After it, the
       back, with the front above it when the back does not already repeat
       it; on the back the cloze asked is lifted out. */
    function setPhase(card, phase) {
        state.phase = phase;
        el.reveal.hidden = phase !== "q";
        if (phase === "q") {
            el.sideQ.hidden = false;
            el.sideA.hidden = true;
            mount(el.sideQ, card, "q", card.q);
        } else {
            var showQ = !card.frontRedundant;
            el.sideQ.hidden = !showQ;
            el.sideA.hidden = false;
            if (showQ) { mount(el.sideQ, card, "q", card.q); }
            mount(el.sideA, card, "a", card.aOnly && showQ ? card.aOnly : card.a);
        }
    }

    function mount(container, card, side, body) {
        var iframe = container.querySelector("iframe");
        var frameId = side + "-" + card.id;
        var doc = buildDocument(card, body, frameId, side);
        if (iframe.getAttribute("data-ahe-id") === frameId && iframe.getAttribute("data-doc") === doc) { return; }
        iframe.setAttribute("data-ahe-id", frameId);
        iframe.setAttribute("data-doc", doc);
        iframe.style.height = "80px";
        iframe.srcdoc = doc;
    }

    function mathjaxConfigScript() {
        var config = {
            tex: {
                displayMath: [["\\[", "\\]"]],
                processEscapes: false, processEnvironments: false, processRefs: false,
                packages: { "[+]": ["noerrors", "mathtools"], "[-]": ["textmacros"] }
            },
            loader: { load: ["[tex]/noerrors", "[tex]/mathtools"] },
            startup: { typeset: true }
        };
        if (state.shared.mathjaxDir) { config.loader.paths = { mathjax: state.shared.mathjaxDir }; }
        return "window.MathJax=" + JSON.stringify(config) + ";";
    }

    /* On the revealed back, the cloze asked on this card -- Anki marks it
       "cloze", the note's other deletions "cloze-inactive" -- is lifted out
       whatever the note type's own colours: it is what the narration is about. */
    var FOCUS_CSS = "body.narrator-revealed .cloze{background:rgba(255,200,0,.32);box-shadow:0 0 0 3px rgba(255,200,0,.32);border-radius:3px}";

    /* The document a card frame holds: Anki's own stylesheets, the note
       type's, then the side -- so the card looks the way it does in Anki. */
    function buildDocument(card, bodyHtml, frameId, side) {
        var shared = state.shared;
        var dark = isDark();
        var htmlAttrs = dark ? ' class="night-mode"' : "";
        var bodyClass = card.bodyClass + (dark ? " nightMode night_mode" : "") + (side === "a" ? " narrator-revealed" : "");
        var head =
            '<meta charset="utf-8">' +
            '<meta name="viewport" content="width=device-width, initial-scale=1">' +
            "<script>" + shared.shimJs + "<\/script>" +
            "<style>:root{--ahe-vh:" + Math.max(320, Math.round(window.innerHeight * 0.7)) + "px}</style>" +
            "<style>" + shared.theme + "</style>" +
            "<style>" + shared.reviewer + "</style>" +
            "<style>" + shared.frame + "</style>" +
            "<style>" + (state.css[card.ntid] || "") + "</style>" +
            "<style>" + FOCUS_CSS + "</style>";
        if (card.jquery && state.shared.jqueryUrl) {
            head += '<script src="' + state.shared.jqueryUrl + '"><\/script>';
        }
        if (card.mathjax && state.shared.mathjaxUrl) {
            head += "<script>" + mathjaxConfigScript() + "<\/script>" +
                '<script id="MathJax-script" async src="' + state.shared.mathjaxUrl + '"><\/script>';
        }
        return "<!doctype html><html" + htmlAttrs + ' dir="ltr"><head>' + head + "</head>" +
            '<body class="' + bodyClass + '"><div id="qa">' + bodyHtml + "</div>" +
            "<script>window.AHE_FRAME_ID=" + JSON.stringify(frameId) +
            ";window.AHE_SIDE=" + JSON.stringify(side) +
            ";window.AHE_INTERACTIVE=false;window.AHE_REVEALED=null;<\/script>" +
            "<script>" + shared.frameJs + "<\/script></body></html>";
    }

    window.addEventListener("message", function (event) {
        var payload = event.data;
        if (!payload || payload.source !== "ahe-frame") { return; }
        if (payload.type === "height") {
            var frame = document.querySelector('iframe[data-ahe-id="' + payload.id + '"]');
            if (frame) { frame.style.height = Math.max(40, payload.height) + "px"; }
        } else if (payload.type === "key") {
            onKey({ key: payload.key, target: document.body, preventDefault: function () {} });
        }
    });

    /* --- narration -------------------------------------------------------- */

    function narrationKey(card) {
        return card.id + "|" + el.seconds.value + "|" + el.voice.value;
    }

    function narrate(card, force) {
        var key = narrationKey(card);
        if (force || !state.narrations[key]) {
            var url = "/api/narrator/narration/" + card.id + "?seconds=" + el.seconds.value +
                "&voice=" + encodeURIComponent(el.voice.value) + (force ? "&force=1" : "");
            el.scriptState.textContent = "writing…";
            el.scriptText.className = "script-text pending";
            el.scriptText.textContent = "";
            el.regen.hidden = true;
            state.narrations[key] = api(url).catch(function (err) {
                delete state.narrations[key];
                throw err;
            });
        }
        return state.narrations[key];
    }

    function prefetch(index) {
        if (index >= state.total) { return; }
        ensureCard(index).then(function (card) {
            if (card && !state.narrations[narrationKey(card)]) {
                narrateQuietly(card);
            }
        });
    }

    function narrateQuietly(card) {
        var key = narrationKey(card);
        var url = "/api/narrator/narration/" + card.id + "?seconds=" + el.seconds.value +
            "&voice=" + encodeURIComponent(el.voice.value);
        state.narrations[key] = api(url).catch(function (err) {
            delete state.narrations[key];
            throw err;
        });
        state.narrations[key].catch(function () {});
    }

    /* --- playing a narration ---------------------------------------------- */

    /* The script is light markdown -- paragraphs, and the key terms in bold.
       Anything else the model might have let slip is shown as text. */
    function renderScript(text) {
        return text.trim().split(/\n\s*\n/).map(function (paragraph) {
            var html = escapeHtml(paragraph.replace(/\s*\n\s*/g, " "))
                .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
                .replace(/(^|[\s(])\*(\S[^*]*?)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>");
            return "<p>" + html + "</p>";
        }).join("");
    }

    function present(card, narration) {
        showUsage(narration.usage);
        state.language = narration.language || "";
        state.showing = card.id;
        state.awaitingReveal = false;
        el.scriptText.className = "script-text";
        if (narration.text) { el.scriptText.innerHTML = renderScript(narration.text); }
        else { el.scriptText.textContent = "(nothing to say about this card)"; }
        el.scriptState.textContent = fmtTime(narration.duration) + " · " + narration.words + " words" +
            (narration.language ? " · " + narration.language : "") + (narration.cached ? " · cached" : "");
        el.regen.hidden = false;
        el.audio.src = narration.audio;
        el.audio.load();
        el.audio.playbackRate = Number(el.speed.value);
        if (state.playing) {
            el.audio.play().catch(function () { setPlaying(false); });
        }
        scheduleReveal();
    }

    /* The front stays on screen for the first seconds of the narration,
       then the back comes -- by itself, or, as in Anki, on Enter. A card
       with nothing to say is revealed at once. */
    function scheduleReveal() {
        clearTimeout(state.revealTimer);
        if (state.phase !== "q") { return; }
        if (!el.audio.src || !state.playing) { return; }
        var mode = (state.config && state.config.reveal) || "auto";
        if (mode === "wait") { state.awaitingReveal = true; return; }
        var wait = ((state.config && state.config.reveal_gap) || 0) * 1000;
        state.revealTimer = setTimeout(revealNow, wait);
    }

    function revealNow() {
        clearTimeout(state.revealTimer);
        state.awaitingReveal = false;
        var card = state.cards[state.index];
        if (card && state.phase !== "a") { setPhase(card, "a"); }
    }

    function onNarrationEnded() {
        el.progressBar.style.width = "100%";
        if (state.playing && el.autoplay.checked) {
            var gap = (state.config && state.config.gap) || 0;
            state.gapTimer = setTimeout(next, gap * 1000);
        }
    }

    /* --- transport -------------------------------------------------------- */

    function setPlaying(playing) {
        state.playing = playing;
        el.play.textContent = playing ? "⏸" : "▶";
    }

    function togglePlay() {
        if (state.index < 0) { return; }
        if (state.playing) {
            setPlaying(false);
            el.audio.pause();
            clearTimeout(state.gapTimer);
            clearTimeout(state.revealTimer);
        } else {
            setPlaying(true);
            if (el.audio.src && el.audio.ended) { next(); }
            else if (el.audio.src) { el.audio.play().catch(function () {}); scheduleReveal(); }
        }
    }

    function stopAudio() {
        el.audio.pause();
        el.audio.removeAttribute("src");
        clearTimeout(state.revealTimer);
        state.awaitingReveal = false;
        el.progressBar.style.width = "0";
        el.time.textContent = "0:00";
    }

    function next() {
        if (state.index + 1 < state.total) { show(state.index + 1); }
        else { setPlaying(false); }
    }

    function prev() {
        if (state.index > 0) { show(state.index - 1); }
    }

    el.audio.addEventListener("timeupdate", function () {
        if (!el.audio.duration) { return; }
        el.progressBar.style.width = (100 * el.audio.currentTime / el.audio.duration) + "%";
        el.time.textContent = fmtTime(el.audio.currentTime) + " / " + fmtTime(el.audio.duration);
    });
    el.audio.addEventListener("ended", onNarrationEnded);
    el.progress.addEventListener("click", function (event) {
        if (!el.audio.duration) { return; }
        var rect = el.progress.getBoundingClientRect();
        el.audio.currentTime = el.audio.duration * (event.clientX - rect.left) / rect.width;
    });

    el.play.addEventListener("click", togglePlay);
    el.reveal.addEventListener("click", revealNow);
    el.next.addEventListener("click", next);
    el.prev.addEventListener("click", prev);
    el.autoplay.addEventListener("change", function () { remember("autoplay", el.autoplay.checked); });

    function onKey(event) {
        var tag = (event.target && event.target.tagName) || "";
        if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || el.settings.open || el.exportDialog.open) { return; }
        if (event.key === " ") { event.preventDefault(); togglePlay(); }
        else if (event.key === "Enter") { event.preventDefault(); if (state.phase === "q") { revealNow(); } }
        else if (event.key === "ArrowRight" || event.key === "PageDown") { event.preventDefault(); next(); }
        else if (event.key === "ArrowLeft" || event.key === "PageUp") { event.preventDefault(); prev(); }
        else if (event.key === "Home") { show(0); }
    }
    document.addEventListener("keydown", onKey);

    /* A swipe across the card on a phone: left for the next card, right for
       the one before. */
    (function () {
        var startX = null, startY = null, startT = 0;
        el.stage.addEventListener("touchstart", function (event) {
            var t = event.touches[0];
            startX = t.clientX; startY = t.clientY; startT = Date.now();
        }, { passive: true });
        el.stage.addEventListener("touchend", function (event) {
            if (startX === null) { return; }
            var t = event.changedTouches[0];
            var dx = t.clientX - startX, dy = t.clientY - startY;
            startX = null;
            if (Math.abs(dx) > 70 && Math.abs(dx) > 2 * Math.abs(dy) && Date.now() - startT < 800) {
                if (dx < 0) { next(); } else { prev(); }
            }
        }, { passive: true });
    })();

    /* --- controls --------------------------------------------------------- */

    /* Length and voice are remembered in the add-on's config, like the order,
       so the page opens on what was chosen last. */
    function saveChoice(key, value) {
        api("/api/narrator/config", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(_pair(key, value))
        }).then(function (config) { state.config = config; }).catch(function () {});
    }
    function _pair(key, value) { var o = {}; o[key] = value; return o; }

    el.seconds.addEventListener("change", function () {
        saveChoice("seconds", parseInt(el.seconds.value, 10));
        if (state.index >= 0) { show(state.index); }
    });
    el.order.addEventListener("change", function () {
        api("/api/narrator/config", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ order: el.order.value })
        }).then(function (config) {
            state.config = config;
            if (state.query) { loadScope(state.query); }
        }).catch(function (err) { setStatus(err.message, "bad"); });
    });
    el.voice.addEventListener("change", function () {
        saveChoice("voice", el.voice.value);
        if (state.index >= 0) { show(state.index); }
    });
    el.speed.addEventListener("change", function () {
        el.audio.playbackRate = Number(el.speed.value);
        saveChoice("speed", Number(el.speed.value));
    });
    el.regen.addEventListener("click", function () {
        var card = state.cards[state.index];
        if (!card) { return; }
        var index = state.index;
        stopAudio();
        setPhase(card, "q");
        narrate(card, true).then(function (narration) {
            if (state.index === index) { present(card, narration); }
        }).catch(function (err) {
            el.scriptState.textContent = "narration failed";
            el.scriptText.className = "script-text error";
            el.scriptText.textContent = err.message;
            el.regen.hidden = false;
        });
    });
    el.scriptToggle.addEventListener("click", function () {
        var hidden = el.main.classList.toggle("script-hidden");
        el.scriptToggle.textContent = hidden ? "show" : "hide";
        remember("scriptHidden", hidden);
    });

    /* Night mode is the shell's switch; the card frames follow the root class. */
    new MutationObserver(function () {
        var card = state.cards[state.index];
        if (card) { setPhase(card, state.phase); }
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

    /* --- preparing ahead ------------------------------------------------ */

    /* The server makes the next few narrations ready in the background, so
       that by the time a card comes up its audio is already there. A few,
       not the whole deck: each one costs two API calls. */
    function lookahead(offset) {
        if (offset >= state.total || !state.config || !state.config.has_key) { return; }
        var url = "/api/narrator/prepare?q=" + encodeURIComponent(state.query) + "&offset=" + offset +
            "&count=" + LOOKAHEAD + "&seconds=" + el.seconds.value + "&voice=" + encodeURIComponent(el.voice.value);
        api(url).catch(function () {});
    }

    /* The script is light markdown -- paragraphs, and the key terms in bold.
       Anything else the model might have let slip is shown as text. */
    function renderScript(text) {
        return text.trim().split(/\n\s*\n/).map(function (paragraph) {
            var html = escapeHtml(paragraph.replace(/\s*\n\s*/g, " "))
                .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
                .replace(/(^|[\s(])\*(\S[^*]*?)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>");
            return "<p>" + html + "</p>";
        }).join("");
    }

    /* --- usage ------------------------------------------------------------ */

    function money(value) {
        if (value == null) { return "—"; }
        return "$" + (value < 0.1 ? value.toFixed(3) : value.toFixed(2));
    }
    function count(value) {
        value = Math.round(value || 0);
        return value >= 10000 ? (value / 1000).toFixed(1) + "k" : String(value);
    }
    function minutes(seconds) {
        seconds = Math.round(seconds || 0);
        return Math.floor(seconds / 60) + ":" + ("0" + (seconds % 60)).slice(-2) + " min";
    }

    /* The chip in the bar says what today has cost; the table in the settings
       says the rest. Cached narrations do not count: nothing was bought. */
    function showUsage(usage) {
        if (!usage) { return; }
        state.usage = usage;
        var today = usage.today || {};
        var total = usage.total || {};
        el.usage.hidden = false;
        el.usage.textContent = money(today.cost) + " today";
        el.usage.title = "Today: " + count(today.narrations) + " narrations, " + count(today.input_tokens) + " tokens in, " +
            count(today.output_tokens) + " out, " + minutes(today.speech_seconds) + " of speech\n" +
            "All time: " + money(total.cost) + " for " + count(total.narrations) + " narrations";
        el.usageRows.innerHTML = ["session", "today", "total"].map(function (key) {
            var row = usage[key] || {};
            return "<tr><td>" + { session: "This session", today: "Today", total: "All time" }[key] + "</td><td>" +
                count(row.narrations) + "</td><td>" + count(row.input_tokens) + "</td><td>" + count(row.output_tokens) +
                "</td><td>" + minutes(row.speech_seconds) + "</td><td>" + money(row.cost) + "</td></tr>";
        }).join("");
        el.usageNote.textContent = usage.unpriced && usage.unpriced.length
            ? "No price is configured for " + usage.unpriced.join(", ") + "; its calls are counted but not costed."
            : "Estimated from the prices in the add-on's config; cached narrations cost nothing.";
    }

    el.usage.addEventListener("click", function () {
        el.settingsOpen.click();
        el.usageDetails.open = true;
    });
    el.usageReset.addEventListener("click", function () {
        api("/api/narrator/usage/reset", { method: "POST" }).then(showUsage).catch(function () {});
    });
    /* The look-ahead spends in the background; look now and then. */
    setInterval(function () {
        if (state.index >= 0) { api("/api/narrator/usage").then(showUsage).catch(function () {}); }
    }, 20000);

    /* --- talking about the card ------------------------------------------- */

    /* Tap the microphone and speak; when the voice has stopped for a while the
       recording is sent, transcribed, and answered -- aloud, in the narration's
       voice. Tapping again sends at once. The narration is paused first, or
       the microphone would take it down too. */
    var VAD_SPEECH_RMS = 0.015;   /* louder than this is speech */
    var VAD_SILENCE_MS = 4000;    /* this much silence after speech ends the take */
    var VAD_MAX_MS = 90000;
    var chat = { card: null, recorder: null, stream: null, ctx: null, raf: null, spoke: false, chunks: [], busy: false };

    function showChat(card) {
        chat.card = card;
        el.chat.hidden = false;
        el.chatLog.innerHTML = "";
        api("/api/narrator/chat/" + card.id).then(function (data) {
            if (chat.card === card) { renderTurns(data.history || []); }
        }).catch(function () {});
    }

    function renderTurns(history, pending) {
        el.chatLog.innerHTML = history.map(function (turn) {
            return '<div class="turn ' + turn.role + '">' + escapeHtml(turn.content) + "</div>";
        }).join("") + (pending ? '<div class="turn assistant pending">' + escapeHtml(pending) + "</div>" : "");
        el.chatLog.scrollTop = el.chatLog.scrollHeight;
    }

    function chatError(text) {
        el.chatError.hidden = !text;
        el.chatError.textContent = text || "";
    }

    function ask(message) {
        var card = chat.card;
        if (!card || !message.trim() || chat.busy) { return; }
        setChatBusy(true);
        chatError("");
        var shown = Array.prototype.map.call(el.chatLog.querySelectorAll(".turn:not(.pending)"), function (node) {
            return { role: node.classList.contains("user") ? "user" : "assistant", content: node.textContent };
        });
        renderTurns(shown.concat([{ role: "user", content: message }]), "…");
        api("/api/narrator/chat", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ card_id: card.id, message: message, voice: el.voice.value,
                                   narration: el.scriptText.classList.contains("error") ? "" : el.scriptText.textContent })
        }).then(function (answer) {
            setChatBusy(false);
            if (chat.card !== card) { return; }
            renderTurns(answer.history || []);
            showUsage(answer.usage);
            el.chatAudio.src = answer.audio;
            el.chatAudio.playbackRate = Number(el.speed.value);
            el.chatAudio.play().catch(function () {});
        }).catch(function (err) {
            setChatBusy(false);
            renderTurns(shown);
            chatError(err.message);
        });
    }

    function setChatBusy(busy) {
        chat.busy = busy;
        el.chatSend.disabled = busy;
        el.mic.classList.toggle("busy", busy && !chat.recorder);
    }

    /* Recording */

    function stopRecording() {
        try { if (chat.recorder && chat.recorder.state !== "inactive") { chat.recorder.stop(); } } catch (e) {}
    }

    function releaseMic() {
        if (chat.raf) { cancelAnimationFrame(chat.raf); chat.raf = null; }
        if (chat.ctx) { try { chat.ctx.close(); } catch (e) {} chat.ctx = null; }
        if (chat.stream) { chat.stream.getTracks().forEach(function (t) { t.stop(); }); chat.stream = null; }
        el.mic.classList.remove("recording");
        el.micHint.hidden = true;
    }

    /* Energy-based voice activity: the take ends by itself once the speaker
       has been quiet for a while, with the seconds left shown on the button. */
    function watchSilence() {
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            if (ctx.state === "suspended") { ctx.resume().catch(function () {}); }
            chat.ctx = ctx;
            var analyser = ctx.createAnalyser();
            analyser.fftSize = 2048;
            ctx.createMediaStreamSource(chat.stream).connect(analyser);
            var buf = new Uint8Array(analyser.fftSize);
            var started = Date.now(), quietSince = null;
            var tick = function () {
                analyser.getByteTimeDomainData(buf);
                var sum = 0;
                for (var i = 0; i < buf.length; i++) { var x = (buf[i] - 128) / 128; sum += x * x; }
                var rms = Math.sqrt(sum / buf.length);
                var now = Date.now();
                if (rms > VAD_SPEECH_RMS) { chat.spoke = true; quietSince = null; el.micHint.hidden = true; }
                else if (chat.spoke) {
                    if (!quietSince) { quietSince = now; }
                    else if (now - quietSince > VAD_SILENCE_MS) { stopRecording(); return; }
                    else {
                        el.micHint.hidden = false;
                        el.micHint.textContent = Math.ceil((VAD_SILENCE_MS - (now - quietSince)) / 1000) + " s — tap to send now";
                    }
                }
                if (now - started > VAD_MAX_MS) { stopRecording(); return; }
                chat.raf = requestAnimationFrame(tick);
            };
            chat.raf = requestAnimationFrame(tick);
        } catch (e) { /* without an AudioContext the tap still stops it */ }
    }

    function toggleMic() {
        if (chat.busy && !chat.recorder) { return; }
        if (chat.recorder) { stopRecording(); return; }
        if (!navigator.mediaDevices || !window.MediaRecorder) {
            chatError(window.isSecureContext ? "This browser cannot record." :
                "The microphone needs a secure page: open the narrator on this computer (127.0.0.1), or over HTTPS.");
            return;
        }
        chatError("");
        /* The narration must not be heard by the microphone */
        if (state.playing) { setPlaying(false); el.audio.pause(); clearTimeout(state.gapTimer); }
        el.chatAudio.pause();
        navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } })
            .then(function (stream) {
                chat.stream = stream;
                var mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].filter(function (m) {
                    return MediaRecorder.isTypeSupported(m);
                })[0] || "";
                var recorder = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : undefined);
                chat.recorder = recorder;
                chat.chunks = [];
                chat.spoke = false;
                recorder.ondataavailable = function (event) { if (event.data.size) { chat.chunks.push(event.data); } };
                recorder.onstop = function () {
                    releaseMic();
                    var blob = new Blob(chat.chunks, { type: (mime || "audio/webm").split(";")[0] });
                    chat.chunks = [];
                    chat.recorder = null;
                    if (!chat.spoke || blob.size < 1200) { chatError("Nothing was heard — a little louder, please."); return; }
                    transcribe(blob);
                };
                recorder.start();
                el.mic.classList.add("recording");
                watchSilence();
            })
            .catch(function (err) {
                chatError(err && err.name === "NotAllowedError" ? "Microphone access was refused — allow it in the address bar." :
                    err && err.name === "NotFoundError" ? "No microphone found." : "The microphone could not be started.");
                releaseMic();
                chat.recorder = null;
            });
    }

    function transcribe(blob) {
        setChatBusy(true);
        /* The card's language is the best hint for what was said */
        fetch("/api/narrator/transcribe?language=" + encodeURIComponent(state.language || ""), {
            method: "POST", headers: { "Content-Type": blob.type || "audio/webm" }, body: blob
        }).then(function (response) {
            return response.json().then(function (body) {
                if (!response.ok) { throw new Error(body.error || response.statusText); }
                return body;
            });
        }).then(function (data) {
            setChatBusy(false);
            showUsage(data.usage);
            if (!data.text) { chatError("Nothing could be understood — once more, please."); return; }
            ask(data.text);
        }).catch(function (err) {
            setChatBusy(false);
            chatError("Transcription failed: " + err.message);
        });
    }

    el.mic.addEventListener("click", toggleMic);
    el.chatForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var message = el.chatInput.value.trim();
        if (!message) { return; }
        el.chatInput.value = "";
        ask(message);
    });

    /* --- export ----------------------------------------------------------- */

    /* An export is the scope on screen -- its order, length and voice -- as
       one file. What it would cost is said before anything is spent: only
       cards without a narration cost, and only if the box is ticked. */
    var exportState = { estimate: null, timer: null };

    function exportParams() {
        return "q=" + encodeURIComponent(state.query) + "&seconds=" + el.seconds.value +
            "&voice=" + encodeURIComponent(el.voice.value);
    }

    function openExport() {
        if (!state.query || !state.total) { setStatus("Load some cards first", "bad"); return; }
        var form = el.exportForm;
        el.exportScope.textContent = state.total + " cards · " + state.query + " · " + el.seconds.value + " s per card · " + el.voice.value;
        el.exportEstimate.textContent = "Counting…";
        el.exportCost.textContent = "";
        el.exportResult.hidden = true;
        el.exportProgress.hidden = true;
        el.exportStart.disabled = true;
        el.exportStart.hidden = false;
        el.exportCancel.hidden = true;
        el.exportDialog.showModal();
        api("/api/narrator/export/estimate?" + exportParams()).then(function (estimate) {
            exportState.estimate = estimate;
            showEstimate();
            return api("/api/narrator/export/status");
        }).then(function (status) {
            if (status && status.running) { followExport(); }
        }).catch(function (err) {
            el.exportEstimate.textContent = err.message;
        });
        form.kind.value = "audio";
        form.night.checked = isDark();
        updateExportKind();
    }

    function showEstimate() {
        var e = exportState.estimate;
        if (!e) { return; }
        var parts = [e.ready + " of " + e.total + " cards already narrated"];
        if (e.missing) { parts.push(e.missing + " still to narrate"); }
        parts.push("about " + e.minutes + " minutes of narration");
        el.exportEstimate.textContent = parts.join(" · ") + ".";
        el.exportMissingRow.hidden = !e.missing;
        el.exportCost.textContent = e.missing ? (e.priced ? "(≈ " + money(e.cost) + ")" : "(price not configured)") : "";
        var blocked = "";
        if (e.too_many) { blocked = "That is more than " + e.max + " cards; narrow the search."; }
        else if (!e.ffmpeg) { blocked = "ffmpeg was not found — install it, or set its path in the add-on config."; }
        else if (el.exportForm.kind.value === "video" && !e.can_draw) { blocked = "No web engine to draw the cards with (Anki's, or a Chromium on this machine)."; }
        else if (!e.ready && !e.missing) { blocked = "Nothing to export."; }
        if (blocked) { el.exportEstimate.textContent += " " + blocked; }
        el.exportStart.disabled = !!blocked;
    }

    function updateExportKind() {
        el.exportNightRow.hidden = el.exportForm.kind.value !== "video";
        showEstimate();
    }
    el.exportForm.addEventListener("change", function (event) {
        if (event.target.name === "kind") { updateExportKind(); }
    });

    function followExport() {
        clearTimeout(exportState.timer);
        el.exportProgress.hidden = false;
        el.exportStart.hidden = true;
        el.exportCancel.hidden = false;
        api("/api/narrator/export/status").then(function (status) {
            var labels = { narrating: "Narrating", capturing: "Drawing the cards", encoding: "Encoding", done: "Done", failed: "Failed", cancelled: "Stopped" };
            var fraction = status.total ? status.done / status.total : 0;
            if (status.phase === "encoding") { fraction = 1; }
            el.exportBar.style.width = Math.round(100 * fraction) + "%";
            var text = (labels[status.phase] || status.phase);
            if (status.phase === "narrating" || status.phase === "capturing") { text += " · " + status.done + " / " + status.total; }
            if (status.skipped) { text += " · " + status.skipped + " left out"; }
            if (status.error && status.phase !== "failed") { text += " · " + status.error; }
            el.exportPhase.textContent = text;
            if (status.running) {
                exportState.timer = setTimeout(followExport, 1000);
                return;
            }
            el.exportStart.hidden = false;
            el.exportCancel.hidden = true;
            el.exportResult.hidden = false;
            if (status.phase === "done") {
                el.exportResult.innerHTML = 'Written to <code>user_files/exports/' + escapeHtml(status.file) + '</code> — ' +
                    '<a href="/api/narrator/export/file/' + encodeURIComponent(status.file) + '">download</a>.';
            } else {
                el.exportResult.textContent = (status.phase === "cancelled" ? "Stopped." : "Failed: " + status.error);
            }
            api("/api/narrator/usage").then(showUsage).catch(function () {});
        }).catch(function (err) {
            el.exportPhase.textContent = err.message;
        });
    }

    el.exportOpen.addEventListener("click", openExport);
    el.exportClose.addEventListener("click", function () { clearTimeout(exportState.timer); el.exportDialog.close(); });
    el.exportCancel.addEventListener("click", function () {
        api("/api/narrator/export/cancel", { method: "POST" }).catch(function () {});
    });
    el.exportForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var form = el.exportForm;
        el.exportResult.hidden = true;
        api("/api/narrator/export/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                kind: form.kind.value, q: state.query, seconds: parseInt(el.seconds.value, 10), voice: el.voice.value,
                narrate_missing: form.narrate_missing.checked && !el.exportMissingRow.hidden,
                night: form.night.checked, title: state.query
            })
        }).then(function () { followExport(); }).catch(function (err) {
            el.exportResult.hidden = false;
            el.exportResult.textContent = err.message;
        });
    });

    /* --- settings --------------------------------------------------------- */

    el.settingsOpen.addEventListener("click", function () {
        var form = el.settingsForm;
        var c = state.config || {};
        form.openai_api_key.value = "";
        el.keyHint.textContent = c.has_key ? "a key is stored (" + c.key_hint + ")" : "none stored";
        form.text_model.value = c.text_model || "";
        form.tts_model.value = c.tts_model || "";
        form.stt_model.value = c.stt_model || "";
        form.language.value = c.language || "";
        form.gap.value = c.gap != null ? c.gap : 1;
        form.reveal.value = c.reveal || "auto";
        form.reveal_gap.value = c.reveal_gap != null ? c.reveal_gap : 2;
        form.style.value = c.style || "";
        form.include_hidden.checked = !!c.include_hidden;
        form.vision.checked = c.vision !== false;
        form.daily_budget.value = c.daily_budget || 0;
        form.share.checked = !!c.share;
        form.pronunciation.value = Object.keys(c.pronunciation || {}).map(function (k) {
            return k + " = " + c.pronunciation[k];
        }).join("\n");
        el.settings.showModal();
    });
    el.settingsCancel.addEventListener("click", function () { el.settings.close(); });
    el.settingsForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var form = el.settingsForm;
        var changes = {
            text_model: form.text_model.value,
            tts_model: form.tts_model.value,
            stt_model: form.stt_model.value,
            language: form.language.value,
            gap: form.gap.value,
            reveal: form.reveal.value,
            reveal_gap: form.reveal_gap.value,
            style: form.style.value,
            include_hidden: form.include_hidden.checked,
            vision: form.vision.checked,
            daily_budget: form.daily_budget.value,
            share: form.share.checked,
            pronunciation: {}
        };
        form.pronunciation.value.split("\n").forEach(function (line) {
            var at = line.indexOf("=");
            if (at > 0) {
                var term = line.slice(0, at).trim(), said = line.slice(at + 1).trim();
                if (term && said) { changes.pronunciation[term] = said; }
            }
        });
        if (form.openai_api_key.value.trim()) { changes.openai_api_key = form.openai_api_key.value.trim(); }
        api("/api/narrator/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(changes)
        }).then(function (config) {
            var hiddenChanged = state.config && config.include_hidden !== state.config.include_hidden;
            state.config = config;
            state.narrations = {};
            el.settings.close();
            return refreshStatus().then(function (ok) {
                if (!ok) { return; }
                if (hiddenChanged || state.index < 0) { if (state.query) { loadScope(state.query); } }
                else { show(state.index); }
            });
        }).catch(function (err) { setStatus(err.message, "bad"); });
    });

    boot();
})();
