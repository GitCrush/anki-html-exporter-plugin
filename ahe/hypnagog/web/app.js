/* Hypnagog on the live server: the engine, with an options sheet on request.
 *
 * The engine (hypnagog.js) is the one the add-on shipped. The page starts it
 * at once on the scope it was opened with and the settings kept in the
 * config; the sheet -- the button top right, or the O key -- changes them:
 * timing and look at once, the cards with a new round. Esc stops the engine
 * and brings the sheet up. */
(function () {
    "use strict";

    var $ = function (id) { return document.getElementById(id); };
    var el = { open: $("options-open"), sheet: $("options"), close: $("options-close"), form: $("options-form"),
               count: $("options-count"), query: $("query"), source: $("source"), max: $("max"), showMs: $("show_ms"),
               mode: $("mode"), progressive: $("progressive"), fullscreen: $("fullscreen"), error: $("error"), start: $("start") };
    var config = null;
    var engine = null;
    var items = [];
    var played = { query: null, source: null, max: null };   /* what the running round was dealt from */
    var fetching = false;

    function api(path, options) {
        return fetch(path, options).then(function (response) {
            return response.json().then(function (body) {
                if (!response.ok) { throw new Error(body.error || response.statusText); }
                return body;
            });
        });
    }

    function fail(text) { el.error.hidden = !text; el.error.textContent = text || ""; }

    function fill(c) {
        el.source.value = c.card_source;
        el.max.value = c.max_cards;
        el.showMs.value = c.show_ms;
        el.mode.value = String(c.visual_mode);
        el.progressive.checked = !!c.progressive_speed;
    }

    function changes() {
        return { card_source: el.source.value, max_cards: parseInt(el.max.value, 10) || 50,
                 show_ms: parseInt(el.showMs.value, 10) || 2500, visual_mode: parseInt(el.mode.value, 10),
                 progressive_speed: el.progressive.checked };
    }

    /* --- the sheet -------------------------------------------------------- */

    function openSheet() {
        el.sheet.hidden = false;
        el.open.hidden = true;
        el.start.textContent = engine ? "Apply" : "Start";
        el.count.textContent = items.length ? items.length + " cards" : "";
        if (engine && !engine.paused) { engine.togglePause(); }
        el.query.focus();
    }

    function closeSheet() {
        el.sheet.hidden = true;
        el.open.hidden = false;
        if (engine && engine.paused) { engine.togglePause(); }
    }

    el.open.addEventListener("click", openSheet);
    el.close.addEventListener("click", closeSheet);
    /* Keys typed into the sheet are not the engine's */
    el.sheet.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { event.preventDefault(); closeSheet(); }
        event.stopPropagation();
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "o" || event.key === "O") {
            if (el.sheet.hidden) { openSheet(); } else { closeSheet(); }
        } else if (event.key === "F11") {
            event.preventDefault();
            if (document.fullscreenElement) { document.exitFullscreen().catch(function () {}); }
            else if (document.documentElement.requestFullscreen) { document.documentElement.requestFullscreen().catch(function () {}); }
        }
    });

    /* --- running ---------------------------------------------------------- */

    function needsNewDeal(c) {
        return el.query.value.trim() !== played.query || c.card_source !== played.source || c.max_cards !== played.max;
    }

    function run() {
        if (engine) { engine.onExit = null; engine.stop(); engine = null; }
        if (el.fullscreen.checked && document.documentElement.requestFullscreen && !document.fullscreenElement) {
            document.documentElement.requestFullscreen().catch(function () {});
        }
        engine = new Hypnagog({
            primeMs: config.prime_ms, showMs: config.show_ms, consolidateMs: config.consolidate_ms,
            roundPauseMs: config.round_pause_ms, progressiveSpeed: !!config.progressive_speed,
            onExit: function () {
                engine = null;
                openSheet();
                if (document.fullscreenElement && document.exitFullscreen) { document.exitFullscreen().catch(function () {}); }
            }
        });
        engine.visualMode = config.visual_mode === 1 ? 1 : 2;
        window.hypnagog = engine;
        var subtitle = (played.query || "the collection") + " · " + items.length + " cards";
        var origRender = engine._render.bind(engine);
        engine._render = function () {
            origRender();
            if (this.container && this.phase === "idle") {
                var sub = this.container.querySelector(".px-sub");
                if (sub) { sub.textContent = subtitle; }
            }
        };
        engine.start(items);
        closeSheet();
    }

    function deal(then) {
        if (fetching) { return; }
        fetching = true;
        el.start.disabled = true;
        fail("");
        var query = el.query.value.trim();
        api("/api/hypnagog/items?q=" + encodeURIComponent(query) + "&source=" + encodeURIComponent(config.card_source) + "&max=" + config.max_cards)
            .then(function (data) {
                fetching = false;
                el.start.disabled = false;
                if (!data.items.length) {
                    fail("No matching cards with usable text — try another source or search.");
                    openSheet();
                    return;
                }
                items = data.items;
                played = { query: query, source: config.card_source, max: config.max_cards };
                el.count.textContent = items.length + " cards";
                then();
            })
            .catch(function (err) { fetching = false; el.start.disabled = false; fail(err.message); openSheet(); });
    }

    el.form.addEventListener("submit", function (event) {
        event.preventDefault();
        var wanted = changes();
        api("/api/hypnagog/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(wanted) })
            .then(function (c) {
                config = c;
                if (!engine || needsNewDeal(c)) { deal(run); return; }
                /* Timing and look change under the running round */
                engine.options.showMs = c.show_ms;
                engine.effectiveShowMs = c.show_ms;
                engine.options.progressiveSpeed = !!c.progressive_speed;
                engine.setMode(c.visual_mode);
                closeSheet();
            })
            .catch(function (err) { fail(err.message); });
    });

    /* --- go ---------------------------------------------------------------- */

    api("/api/hypnagog/config").then(function (c) {
        config = c;
        fill(c);
        /* The scope the export dialog was pointed at travels in the address */
        var q = new URLSearchParams(window.location.search).get("q");
        if (q) { el.query.value = q; }
        deal(run);
    }).catch(function (err) { fail(err.message); openSheet(); });
})();
