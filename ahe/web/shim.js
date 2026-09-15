/* Runs in the head of every card frame, before the note type's own scripts.
 *
 * Templates are written for the reviewer and call into it while the document
 * is being parsed -- `pycmd` to ask for the next card, `anki.imageOcclusion`
 * to draw masks. A missing global there does not merely do nothing: it throws,
 * and the rest of that script never runs. The export cannot answer any of
 * these calls, but it can make sure they are harmless.
 *
 * frame.js repeats some of this at the end of the body; that is for hooks and
 * timers, which run later. Only what is in the head is early enough for the
 * template's own inline scripts. */

window.ankiPlatform = window.ankiPlatform || "desktop";

if (typeof window.pycmd !== "function") {
    window.pycmd = function () {};
}
if (typeof window.bridgeCommand !== "function") {
    window.bridgeCommand = function () {};
}

window.onUpdateHook = window.onUpdateHook || [];
window.onShownHook = window.onShownHook || [];

/* Anki's own image occlusion note type calls this to paint its masks onto a
   canvas. frame.js draws the same shapes as SVG instead -- what matters here
   is that the template's try/catch does not report the export as broken. */
window.anki = window.anki || {};
window.anki.imageOcclusion = window.anki.imageOcclusion || {
    setup: function () {},
    drawShape: function () {}
};
/* The name the note type had in Anki 23.10; the reviewer still aliases it,
   and note types made back then -- copied and renamed since -- still call it. */
window.anki.setupImageCloze = window.anki.setupImageCloze || window.anki.imageOcclusion.setup;
