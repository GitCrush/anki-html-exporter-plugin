/* Runs inside every card frame.
 *
 * The frame is a faithful copy of the reviewer's document: body carries the
 * `card cardN` classes, the rendered side sits in #qa, and the note type's own
 * stylesheet and <script> tags are in charge. This file only adds what the
 * export needs on top: height reporting to the parent, theme switching, and
 * stubs for the reviewer globals that hand-written templates like to call. */

(function () {
    "use strict";

    var frameId = window.AHE_FRAME_ID || "";
    var lastHeight = -1;

    /* --- reviewer compatibility shims ------------------------------------ */

    window.ankiPlatform = window.ankiPlatform || "desktop";
    if (typeof window.pycmd !== "function") {
        /* Templates calling pycmd() would otherwise throw and abort the rest
           of their script. */
        window.pycmd = function () {};
    }
    window.onUpdateHook = window.onUpdateHook || [];
    window.onShownHook = window.onShownHook || [];

    function runHooks(hooks) {
        for (var i = 0; i < hooks.length; i++) {
            try {
                hooks[i]();
            } catch (err) {
                /* A broken template hook must not stop height reporting. */
                console.error(err);
            }
        }
    }

    /* --- keeping the card visible ---------------------------------------- */

    /* Some shared note types hide #qa on the front and only reveal it once the
     * reviewer reacts to a pycmd() call -- Ankizin's and AnKing's "cloze one by
     * one" scripts do exactly that. In an export nothing ever answers, so the
     * side would stay blank. Undo such a hide once the template's scripts have
     * had their turn. */
    function ensureVisible() {
        var qa = document.getElementById("qa");
        if (!qa || !qa.children.length) {
            return;
        }
        /* First drop the inline properties the template set: that is what its
           own "not in the reviewer" path does, and it keeps whatever display
           mode the stylesheet asks for. */
        qa.style.removeProperty("display");
        qa.style.removeProperty("visibility");
        var style = getComputedStyle(qa);
        if (style.display === "none") {
            qa.style.setProperty("display", "block", "important");
        }
        if (style.visibility === "hidden") {
            qa.style.setProperty("visibility", "visible", "important");
        }
        if (parseFloat(style.opacity) === 0) {
            qa.style.setProperty("opacity", "1", "important");
        }
    }

    /* Deep enough for a card, cheap enough to run on every measurement. */
    var CLIP_SCAN_LIMIT = 400;

    /* --- height reporting ------------------------------------------------ */

    function measure() {
        var body = document.body;
        var root = document.documentElement;
        if (!body || !root) {
            return 0;
        }
        return Math.ceil(
            Math.max(
                body.scrollHeight,
                body.offsetHeight,
                root.scrollHeight,
                root.offsetHeight
            ) + clipped()
        );
    }

    /* A note type that lays its card out for a *window* rather than for a page
       reports nothing but the height it was given. Ankizin's does: it sizes
       #qa to the viewport and scrolls an inner container, so in the reviewer
       the card fills the screen and scrolls inside itself. A frame has no
       height until this measurement decides one, so such a card would report
       the frame's own two lines and stay collapsed at them forever.

       What it would need is inside the container that clips it, so add back
       what deliberately scrollable boxes are hiding. The frame then grows to
       the whole card -- which is what an export is for; on paper and in a page
       there is nothing to scroll inside. */
    function clipped() {
        var qa = document.getElementById("qa");
        if (!qa || !window.getComputedStyle) {
            return 0;
        }
        /* Cheap gate: on a card that lays itself out normally nothing clips
           here, and the walk below never happens. */
        if (qa.scrollHeight - qa.clientHeight < 2) {
            return 0;
        }
        var extra = hiddenHeight(qa);
        var nodes = qa.querySelectorAll("*");
        for (var i = 0; i < nodes.length && i < CLIP_SCAN_LIMIT; i++) {
            extra += hiddenHeight(nodes[i]);
        }
        return extra;
    }

    function hiddenHeight(node) {
        var hidden = node.scrollHeight - node.clientHeight;
        if (hidden < 2) {
            return 0;
        }
        /* Only boxes that were meant to scroll. One clipping with
           overflow:hidden is decoration -- a mask, a cropped picture -- and
           growing the frame for it would leave a hole. */
        var overflow = window.getComputedStyle(node).overflowY;
        return overflow === "auto" || overflow === "scroll" ? hidden : 0;
    }

    function report(force) {
        var height = measure();
        if (!height) {
            return;
        }
        if (!force && Math.abs(height - lastHeight) < 2) {
            return;
        }
        lastHeight = height;
        try {
            parent.postMessage(
                { source: "ahe-frame", id: frameId, type: "height", height: height },
                "*"
            );
        } catch (err) {
            console.error(err);
        }
    }

    /* --- theme ----------------------------------------------------------- */

    function applyTheme(night) {
        var root = document.documentElement;
        root.classList.toggle("night-mode", !!night);
        document.body.classList.toggle("nightMode", !!night);
        document.body.classList.toggle("night_mode", !!night);
        report(true);
    }

    window.addEventListener("message", function (event) {
        var data = event.data;
        if (!data || data.source !== "ahe-shell") {
            return;
        }
        if (data.type === "theme") {
            applyTheme(data.night);
        } else if (data.type === "measure") {
            report(true);
        } else if (data.type === "interactive") {
            interactive = !!data.on;
            document.documentElement.classList.toggle("ahe-interactive", interactive);
            if (interactive) {
                setupInteractive();
            } else {
                setAllClozes(false);
            }
            report(true);
        } else if (data.type === "reveal") {
            setAllClozes(!!data.open);
            document.body.classList.toggle("ahe-io-lift-all", !!data.open);
            report(true);
        }
    });

    /* --- interactive front side ------------------------------------------ */

    /* Everything a reader needs to uncover a card is already in the rendered
     * question: Anki writes the answer of an active cloze into `data-cloze`,
     * and an image occlusion note carries every mask as a `data-shape` div.
     * The reviewer turns that into interaction by re-rendering the answer; in
     * an export there is nothing to re-render, so the front uncovers itself
     * in place -- click a deletion, click a mask. */

    var interactive = window.AHE_INTERACTIVE !== false;
    var side = window.AHE_SIDE === "a" ? "a" : "q";
    var SVG_NS = "http://www.w3.org/2000/svg";

    /* Cloze deletions ------------------------------------------------------ */

    function clozeSpans() {
        /* Image occlusion notes are cloze notes as well, but their deletions
           are shapes on a picture and are handled further down. */
        return document.querySelectorAll("[data-cloze]:not([data-shape])");
    }

    function markClozes() {
        var spans = clozeSpans();
        for (var i = 0; i < spans.length; i++) {
            if (spans[i].getAttribute("data-ahe-cloze")) {
                continue;
            }
            spans[i].setAttribute("data-ahe-cloze", "1");
            spans[i].setAttribute("role", "button");
            spans[i].setAttribute("tabindex", "0");
            spans[i].setAttribute("title", "Click to reveal");
        }
        return spans.length;
    }

    function revealCloze(span, open) {
        if ((span.getAttribute("data-ahe-open") === "1") === open) {
            return;
        }
        if (open) {
            /* The placeholder is whatever the template chose to show -- "[...]"
               or a hint -- so keep it rather than rebuilding it. */
            span.setAttribute("data-ahe-hidden", span.innerHTML);
            span.innerHTML = span.getAttribute("data-cloze") || "";
            span.setAttribute("data-ahe-open", "1");
        } else {
            span.innerHTML = span.getAttribute("data-ahe-hidden") || span.innerHTML;
            span.removeAttribute("data-ahe-open");
        }
        report(true);
        reportReveal();
    }

    function setAllClozes(open) {
        var spans = document.querySelectorAll("[data-ahe-cloze]");
        for (var i = 0; i < spans.length; i++) {
            revealCloze(spans[i], open);
        }
    }

    /* --- what the reader has uncovered ------------------------------------ */

    /* In a large export a card that has scrolled well out of sight is taken
     * down again and built afresh when it comes back, which would otherwise
     * cover up everything the reader had lifted. So the frame reports what is
     * open and the shell hands it back on the next build.
     *
     * By position, not by identity: the document is rebuilt from the very same
     * HTML every time, so the n-th deletion is the n-th deletion. Nothing here
     * can restore what the note type's own scripts did -- a hint the template
     * expanded is that template's business -- but the deletions and masks are
     * ours, and those come back. */

    function shapeNodes() {
        return document.querySelectorAll(".ahe-io-shape");
    }

    /* Anki's {{hint:Field}} renders a link that hides itself and shows the
       field when it is clicked -- template JavaScript, but written by Anki and
       the same on every card, so unfolding one is a manipulation like any
       other and comes back the same way. */
    function hintLinks() {
        return document.querySelectorAll("a.hint");
    }

    function revealState() {
        var open = [];
        var spans = clozeSpans();
        var i;
        for (i = 0; i < spans.length; i++) {
            if (spans[i].getAttribute("data-ahe-open") === "1") {
                open.push(i);
            }
        }
        var lifted = [];
        var shapes = shapeNodes();
        for (i = 0; i < shapes.length; i++) {
            if (shapes[i].classList.contains("ahe-io-lifted")) {
                lifted.push(i);
            }
        }
        var unfolded = [];
        var hints = hintLinks();
        for (i = 0; i < hints.length; i++) {
            if (hints[i].style.display === "none") {
                unfolded.push(i);
            }
        }
        var overlay = document.getElementById("io-overlay");
        return {
            clozes: open,
            shapes: lifted,
            hints: unfolded,
            masksHidden: document.body.classList.contains("ahe-io-hidden"),
            overlayOff: !!(overlay && overlay.style.display === "none")
        };
    }

    /* Uncovering all deletions at once walks every span, so the report waits
       out the batch rather than sending one message per deletion. */
    var revealTimer = null;

    function reportReveal() {
        if (revealTimer) {
            return;
        }
        revealTimer = setTimeout(function () {
            revealTimer = null;
            try {
                parent.postMessage(
                    {
                        source: "ahe-frame",
                        id: frameId,
                        type: "revealed",
                        state: revealState()
                    },
                    "*"
                );
            } catch (err) {
                /* not fatal */
            }
        }, 0);
    }

    var pendingRestore = window.AHE_REVEALED || null;
    if (pendingRestore) {
        if (!pendingRestore.clozes || !pendingRestore.clozes.length) {
            pendingRestore.clozes = null;
        }
        if (!pendingRestore.shapes || !pendingRestore.shapes.length) {
            pendingRestore.shapes = null;
        }
        if (!pendingRestore.hints || !pendingRestore.hints.length) {
            pendingRestore.hints = null;
        }
    }

    /* Called on every pass of setupInteractive, because the occlusion shapes
       are only drawn once the picture has loaded. Each piece is applied once
       and then struck off, so a later pass never re-opens what the reader has
       closed again in the meantime. */
    function applyRestore() {
        if (!pendingRestore) {
            return;
        }
        var state = pendingRestore;
        var i;

        if (state.clozes) {
            var spans = clozeSpans();
            if (spans.length) {
                for (i = 0; i < state.clozes.length; i++) {
                    if (spans[state.clozes[i]]) {
                        revealCloze(spans[state.clozes[i]], true);
                    }
                }
                state.clozes = null;
            }
        }

        if (state.shapes) {
            var shapes = shapeNodes();
            if (shapes.length) {
                for (i = 0; i < state.shapes.length; i++) {
                    if (shapes[state.shapes[i]]) {
                        shapes[state.shapes[i]].classList.add("ahe-io-lifted");
                    }
                }
                state.shapes = null;
            }
        }

        if (state.hints) {
            var hints = hintLinks();
            if (hints.length) {
                for (i = 0; i < state.hints.length; i++) {
                    var hint = hints[state.hints[i]];
                    /* Clicking it is exactly what the reader did, and it is
                       Anki's own handler that knows which element to show. */
                    if (hint && hint.style.display !== "none") {
                        hint.click();
                    }
                }
                state.hints = null;
            }
        }

        if (state.masksHidden) {
            document.body.classList.add("ahe-io-hidden");
            state.masksHidden = false;
        }

        if (state.overlayOff) {
            var overlay = document.getElementById("io-overlay");
            if (overlay) {
                overlay.style.display = "none";
                var wrapper = document.getElementById("io-wrapper");
                if (wrapper) {
                    wrapper.classList.add("ahe-io-open");
                }
                state.overlayOff = false;
            }
        }

        if (
            !state.clozes &&
            !state.shapes &&
            !state.hints &&
            !state.masksHidden &&
            !state.overlayOff
        ) {
            pendingRestore = null;
        }
    }

    function ancestorWith(node, attribute) {
        while (node && node !== document) {
            if (node.getAttribute && node.getAttribute(attribute)) {
                return node;
            }
            node = node.parentNode;
        }
        return null;
    }

    /* Image occlusion, add-on flavour -------------------------------------- */

    /* Image Occlusion Enhanced stores its masks as SVG files layered over the
     * picture by the note type's own CSS, so the mask is already on screen --
     * only the means to lift it is missing on the question side. */
    function setupMaskOverlay() {
        var wrapper = document.getElementById("io-wrapper");
        var overlay = document.getElementById("io-overlay");
        if (!wrapper || !overlay || wrapper.getAttribute("data-ahe-io")) {
            return false;
        }
        wrapper.setAttribute("data-ahe-io", "1");
        wrapper.classList.add("ahe-io-maskable");
        wrapper.addEventListener("click", function () {
            var hidden = overlay.style.display === "none";
            overlay.style.display = hidden ? "" : "none";
            wrapper.classList.toggle("ahe-io-open", !hidden);
            report(true);
            reportReveal();
        });
        return true;
    }

    /* Image occlusion, Anki's own ------------------------------------------ */

    /* Anki draws these shapes from its reviewer bundle onto a canvas. Half a
     * megabyte of reviewer is not worth shipping for it, and a canvas cannot
     * be clicked shape by shape, so the same geometry is drawn as SVG: the
     * numbers are fractions of the picture, exactly as `data-*` stores them. */

    function shapeData(element) {
        var data = element.dataset || {};
        var kind = data.shape;
        if (kind !== "rect" && kind !== "ellipse" && kind !== "polygon" && kind !== "text") {
            return null;
        }
        return {
            kind: kind,
            left: parseFloat(data.left) || 0,
            top: parseFloat(data.top) || 0,
            width: parseFloat(data.width) || 0,
            height: parseFloat(data.height) || 0,
            rx: parseFloat(data.rx) || 0,
            ry: parseFloat(data.ry) || 0,
            points: (data.points || "")
                .split(" ")
                .filter(Boolean)
                .map(function (pair) {
                    var xy = pair.split(",");
                    return { x: parseFloat(xy[0]) || 0, y: parseFloat(xy[1]) || 0 };
                }),
            text: data.text || "",
            scale: parseFloat(data.scale) || 1,
            fontSize: parseFloat(data.fs) || 0,
            occludeInactive: data.occludeinactive === "1",
            ordinal: parseInt(data.ordinal, 10) || 0
        };
    }

    function readShapes(selector) {
        var nodes = document.querySelectorAll(selector + "[data-shape]");
        var shapes = [];
        for (var i = 0; i < nodes.length; i++) {
            var shape = shapeData(nodes[i]);
            if (shape) {
                shapes.push(shape);
            }
        }
        return shapes;
    }

    function occlusionColors(canvas) {
        var style = canvas ? getComputedStyle(canvas) : null;

        function value(name, fallback) {
            var raw = style ? style.getPropertyValue(name).trim() : "";
            return raw || fallback;
        }

        function border(name, fallbackColor) {
            var parts = value(name, "").split(/\s+/).filter(Boolean);
            var width = parseFloat(parts[0]);
            return {
                width: isNaN(width) ? 1 : width,
                color: parts[1] || fallbackColor
            };
        }

        return {
            active: value("--active-shape-color", "#ffeba2"),
            inactive: value("--inactive-shape-color", "#ffeba2"),
            highlight: value("--highlight-shape-color", "#ff8e8e00"),
            activeBorder: border("--active-shape-border", "#212121"),
            inactiveBorder: border("--inactive-shape-border", "#212121"),
            highlightBorder: border("--highlight-shape-border", "#ff8e8e")
        };
    }

    function svgNode(name, attributes) {
        var node = document.createElementNS(SVG_NS, name);
        Object.keys(attributes).forEach(function (key) {
            node.setAttribute(key, attributes[key]);
        });
        return node;
    }

    function shapeNode(shape, width, height) {
        if (shape.kind === "rect") {
            return svgNode("rect", {
                x: shape.left * width,
                y: shape.top * height,
                width: shape.width * width,
                height: shape.height * height
            });
        }
        if (shape.kind === "ellipse") {
            /* left/top is the bounding box, so the centre sits a radius in. */
            return svgNode("ellipse", {
                cx: (shape.left + shape.rx) * width,
                cy: (shape.top + shape.ry) * height,
                rx: shape.rx * width,
                ry: shape.ry * height
            });
        }
        if (shape.kind === "polygon") {
            if (!shape.points.length) {
                return null;
            }
            var minX = shape.points[0].x;
            var minY = shape.points[0].y;
            shape.points.forEach(function (point) {
                minX = Math.min(minX, point.x);
                minY = Math.min(minY, point.y);
            });
            /* The points are absolute within the picture; left/top says where
               the shape's own bounding box was moved to. */
            var dx = (shape.left - minX) * width;
            var dy = (shape.top - minY) * height;
            return svgNode("polygon", {
                points: shape.points
                    .map(function (point) {
                        return point.x * width + dx + "," + (point.y * height + dy);
                    })
                    .join(" ")
            });
        }
        return null;
    }

    function textNode(shape, width, height, group) {
        var size = (shape.fontSize ? shape.fontSize * height : 40) * shape.scale;
        var text = svgNode("text", {
            x: shape.left * width,
            y: shape.top * height,
            "font-family": "Arial",
            "font-size": size,
            "dominant-baseline": "text-before-edge",
            fill: "#000"
        });
        shape.text.split("\n").forEach(function (line, index) {
            var span = svgNode("tspan", {
                x: shape.left * width,
                dy: index === 0 ? 0 : size * 1.5
            });
            span.textContent = line;
            text.appendChild(span);
        });
        /* The white plate behind the text can only be sized once the glyphs
           are laid out, so the text goes in first and the plate slides under
           it afterwards. */
        group.appendChild(text);
        var box = text.getBBox();
        var plate = svgNode("rect", {
            x: box.x - 2,
            y: box.y - 2,
            width: box.width + 5,
            height: box.height + 5,
            fill: "#ffffff"
        });
        group.insertBefore(plate, text);
        return null;
    }

    function drawOcclusion(container, image, canvas) {
        var width = image.naturalWidth;
        var height = image.naturalHeight;
        if (!width || !height) {
            return;
        }
        /* Anki sizes the container by the picture's aspect ratio; its own CSS
           then stretches image and canvas across it. */
        container.style.aspectRatio = width / height;

        var previous = container.querySelector(".ahe-io-svg");
        if (previous) {
            previous.parentNode.removeChild(previous);
        }

        var colors = occlusionColors(canvas);
        var svg = svgNode("svg", {
            class: "ahe-io-svg",
            viewBox: "0 0 " + width + " " + height,
            preserveAspectRatio: "none"
        });

        [
            { selector: ".cloze", fill: colors.active, border: colors.activeBorder, role: "active" },
            { selector: ".cloze-inactive", fill: colors.inactive, border: colors.inactiveBorder, role: "inactive" },
            { selector: ".cloze-highlight", fill: colors.highlight, border: colors.highlightBorder, role: "highlight" }
        ].forEach(function (layer) {
            readShapes(layer.selector).forEach(function (shape) {
                /* An inactive deletion is only masked when the note asks for
                   it -- that is what "hide all, guess one" means. */
                if (layer.role === "inactive" && !shape.occludeInactive) {
                    return;
                }
                var group = svgNode("g", {
                    class: "ahe-io-shape ahe-io-" + layer.role + (shape.kind === "text" ? " ahe-io-text" : ""),
                    "data-ordinal": shape.ordinal
                });
                if (shape.kind === "text") {
                    textNode(shape, width, height, group);
                } else {
                    var node = shapeNode(shape, width, height);
                    if (!node) {
                        return;
                    }
                    node.setAttribute("fill", layer.fill);
                    node.setAttribute("stroke", layer.border.color);
                    node.setAttribute("stroke-width", layer.border.width);
                    group.appendChild(node);
                }
                svg.appendChild(group);
            });
        });

        if (!svg.childNodes.length) {
            return;
        }
        container.appendChild(svg);
        container.classList.add("ahe-io-drawn");
        report(true);
    }

    function setupOcclusion() {
        var container = document.getElementById("image-occlusion-container");
        if (!container) {
            return false;
        }
        var image = container.querySelector("img");
        var canvas = document.getElementById("image-occlusion-canvas");
        if (!image) {
            return false;
        }
        function draw() {
            drawOcclusion(container, image, canvas);
        }
        if (image.complete && image.naturalWidth) {
            draw();
        } else {
            image.addEventListener("load", draw);
            image.addEventListener("error", function () {});
        }

        /* The note type ships a "Toggle Masks" button that the reviewer wires
           up; without Anki's bundle it would sit there doing nothing. */
        var toggle = document.getElementById("toggle");
        if (toggle && !toggle.getAttribute("data-ahe-io")) {
            toggle.setAttribute("data-ahe-io", "1");
            if (document.querySelector('[data-occludeinactive="1"]')) {
                toggle.addEventListener("click", function () {
                    document.body.classList.toggle("ahe-io-hidden");
                    report(true);
                    reportReveal();
                });
            } else {
                toggle.style.display = "none";
            }
        }
        return true;
    }

    /* Wiring --------------------------------------------------------------- */

    function setupInteractive() {
        if (!interactive) {
            return;
        }
        document.documentElement.classList.add("ahe-interactive");
        markClozes();
        setupMaskOverlay();
        setupOcclusion();
        applyRestore();
    }

    function handleReveal(target) {
        var cloze = ancestorWith(target, "data-ahe-cloze");
        if (cloze) {
            revealCloze(cloze, cloze.getAttribute("data-ahe-open") !== "1");
            return true;
        }
        var shape = ancestorWith(target, "data-ordinal");
        if (shape && shape.classList && shape.classList.contains("ahe-io-shape")) {
            shape.classList.toggle("ahe-io-lifted");
            reportReveal();
            return true;
        }
        return false;
    }

    document.addEventListener("click", function (event) {
        if (interactive && handleReveal(event.target)) {
            return;
        }
        /* A hint unfolds through Anki's own inline handler, which tells nobody
           it ran. The click bubbles up here all the same, so the state is read
           back once that handler is through -- and it is read whether or not
           click-to-reveal is on, because unfolding a hint was never ours to
           switch off. */
        if (event.target.closest && event.target.closest("a.hint")) {
            setTimeout(function () {
                report(true);
                reportReveal();
            }, 0);
        }
    });

    /* Keys pressed while the pointer sits over a card land in the frame, which
       knows nothing about cards, decks or a cursor. Anything the frame has no
       use for itself is handed up to the shell. */
    var FORWARDED_KEYS = [
        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",
        " ",
        "Enter",
        "Escape"
    ];

    function editableTarget(target) {
        if (!target || !target.tagName) {
            return false;
        }
        var tag = target.tagName.toLowerCase();
        return (
            tag === "input" ||
            tag === "textarea" ||
            tag === "select" ||
            target.isContentEditable
        );
    }

    document.addEventListener("keydown", function (event) {
        if (event.ctrlKey || event.metaKey || event.altKey || editableTarget(event.target)) {
            return;
        }
        if (
            interactive &&
            (event.key === "Enter" || event.key === " ") &&
            handleReveal(event.target)
        ) {
            event.preventDefault();
            return;
        }
        if (FORWARDED_KEYS.indexOf(event.key) === -1) {
            return;
        }
        try {
            parent.postMessage(
                { source: "ahe-frame", id: frameId, type: "key", key: event.key },
                "*"
            );
            event.preventDefault();
        } catch (err) {
            console.error(err);
        }
    });

    /* --- links ------------------------------------------------------------ */

    /* A card must never be navigated away from. The frame is written with
     * srcdoc, so it has no address of its own and every relative href resolves
     * against the page that wrote it: an <a href=""> around a picture -- which
     * is how several note types make an image clickable -- therefore loaded
     * the whole export back into the card, a frame inside the frame.
     *
     * A link that names a destination gets a tab of its own. One that names
     * nothing was never meant to go anywhere, and now does not. The listener
     * only prevents the default; it does not stop the event, so a template's
     * own handler -- Anki's hint links among them -- still runs. */
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
            if (href.charAt(0) === "#") {
                return; /* an anchor inside this very card */
            }
            if (/^javascript:/i.test(href)) {
                return; /* the template's own business */
            }
            event.preventDefault();
            window.open(link.href, "_blank", "noopener");
        },
        true
    );

    /* --- wiring ---------------------------------------------------------- */

    function start() {
        runHooks(window.onUpdateHook);
        runHooks(window.onShownHook);
        ensureVisible();
        setupInteractive();
        report(true);

        if (typeof ResizeObserver === "function") {
            var observer = new ResizeObserver(function () {
                report(false);
            });
            observer.observe(document.body);
            /* A card sized to the viewport keeps the body the same two lines
               whatever happens; what changes when the frame is given its
               height is the document element. */
            observer.observe(document.documentElement);
        }

        /* Images and web fonts change the layout after first paint. */
        window.addEventListener("load", function () {
            ensureVisible();
            report(true);
        });
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(function () {
                report(true);
            });
        }
        /* Templates that wait on the reviewer may hide #qa asynchronously, so
           re-check for a while rather than only once. */
        [50, 250, 1000, 2500].forEach(function (delay) {
            setTimeout(function () {
                ensureVisible();
                setupInteractive();
                report(false);
            }, delay);
        });

        if (window.MathJax && window.MathJax.startup && window.MathJax.startup.promise) {
            window.MathJax.startup.promise.then(function () {
                report(true);
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
