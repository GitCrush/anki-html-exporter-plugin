/**
 * HYPNAGOG / POLYBIUS EDITION
 *
 * Mode 1: Clean (sans-serif, no FX layers, no audio)
 * Mode 2: Polybius (CRT, particles, ambient audio)
 * Both modes: color-coded cloze keys
 *
 * Keys: 1/2 mode, 3 cloze, Space pause, ←→ nav, ↑ prioritize, ↓ dismiss, F11 fs, ESC exit
 */

class Hypnagog {
  constructor(options = {}) {
    this.options = {
      primeMs: 80,
      showMs: 2500,
      consolidateMs: 900,
      roundPauseMs: 1200,
      revealMs: 1200,
      blankMs: 1400,
      progressiveSpeed: false,
      ...options
    };

    this.items = [];
    this.currentIndex = 0;
    this.round = 1;
    this.phase = 'idle'; // idle, prime, show, consolidate
    this.visualMode = 2;
    this.clozeMode = true;
    this.paused = false;
    this.active = false;
    this.timeout = null;
    this.container = null;
    this.onExit = options.onExit || null;
    this.onItemDismiss = options.onItemDismiss || null;

    this.revealKeys = [];
    this.revealIndex = -1;

    // Progressive speed: effective showMs, decreases per round
    this.effectiveShowMs = this.options.showMs;

    // Audio bus
    this.audioCtx = null;
    this.masterGain = null;
    this.reverbSend = null;
    this.reverbReturn = null;
    this.convolver = null;
    this._nodePool = []; // prevent GC of active nodes

    // Particles
    this.particleCanvas = null;
    this.particleCtx = null;
    this.particles = [];
    this.particleRAF = null;

    this._boundKeyHandler = this._handleKeyDown.bind(this);
  }

  static COLORS = [
    { main: '#fbbf24', glow: 'rgba(251,191,36,0.5)',  far: 'rgba(245,158,11,0.3)' },
    { main: '#22d3ee', glow: 'rgba(34,211,238,0.5)',   far: 'rgba(6,182,212,0.3)' },
    { main: '#a78bfa', glow: 'rgba(167,139,250,0.5)',  far: 'rgba(139,92,246,0.3)' },
    { main: '#f97316', glow: 'rgba(249,115,22,0.5)',   far: 'rgba(234,88,12,0.3)' },
    { main: '#34d399', glow: 'rgba(52,211,153,0.5)',   far: 'rgba(16,185,129,0.3)' },
    { main: '#fb7185', glow: 'rgba(251,113,133,0.5)',  far: 'rgba(244,63,94,0.3)' },
  ];

  _getColor(ki) { return Hypnagog.COLORS[ki % Hypnagog.COLORS.length]; }

  // ==================== AUDIO ====================
  //
  // Architecture: osc → gain → masterGain → destination
  //                                ↘ reverbSend → convolver → reverbReturn → destination
  //
  // Single bus, no per-tone routing. Nodes held in pool until expired.

  _initAudio() {
    if (this.audioCtx) return;
    try {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const ctx = this.audioCtx;

      // Master output
      this.masterGain = ctx.createGain();
      this.masterGain.gain.value = 1.0;
      this.masterGain.connect(ctx.destination);

      // Reverb send/return bus
      this.reverbSend = ctx.createGain();
      this.reverbSend.gain.value = 0.35;

      this.reverbReturn = ctx.createGain();
      this.reverbReturn.gain.value = 1.0;
      this.reverbReturn.connect(ctx.destination);

      // Convolver impulse: 2.5s tail, exponential decay, stereo
      const len = ctx.sampleRate * 2.5;
      const impulse = ctx.createBuffer(2, len, ctx.sampleRate);
      for (let ch = 0; ch < 2; ch++) {
        const data = impulse.getChannelData(ch);
        for (let i = 0; i < len; i++) {
          const t = i / len;
          // Smooth exponential decay with some early reflections
          const decay = Math.exp(-t * 4) * 0.6 + Math.exp(-t * 1.5) * 0.4;
          data[i] = (Math.random() * 2 - 1) * decay;
        }
      }

      this.convolver = ctx.createConvolver();
      this.convolver.buffer = impulse;

      this.reverbSend.connect(this.convolver);
      this.convolver.connect(this.reverbReturn);
    } catch (e) {}
  }

  /**
   * Clean tone synthesis. All nodes connect to the master bus.
   * Uses D minor pentatonic intervals for reveals.
   */
  _playTone(freq, vol, dur, opts = {}) {
    if (!this.audioCtx || this.visualMode !== 2) return;
    try {
      const ctx = this.audioCtx;
      const now = ctx.currentTime;
      const type = opts.type || 'sine';
      const reverb = opts.reverb !== false;
      const attack = opts.attack || 0.03;
      const release = opts.release || dur * 0.6;
      const hold = dur - attack - release;

      const osc = ctx.createOscillator();
      const env = ctx.createGain();

      osc.type = type;
      osc.frequency.setValueAtTime(freq, now);
      if (opts.freqEnd) {
        osc.frequency.exponentialRampToValueAtTime(opts.freqEnd, now + dur);
      }

      // ADSR-ish envelope: attack → hold → release
      env.gain.setValueAtTime(0, now);
      env.gain.linearRampToValueAtTime(vol, now + attack);
      if (hold > 0) {
        env.gain.setValueAtTime(vol, now + attack + Math.max(hold, 0));
      }
      env.gain.exponentialRampToValueAtTime(0.0001, now + dur);

      osc.connect(env);
      env.connect(this.masterGain);

      if (reverb && this.reverbSend) {
        env.connect(this.reverbSend);
      }

      osc.start(now);
      osc.stop(now + dur + 0.05);

      // Hold refs to prevent GC until done + reverb tail
      const entry = { osc, env };
      this._nodePool.push(entry);
      setTimeout(() => {
        const idx = this._nodePool.indexOf(entry);
        if (idx >= 0) this._nodePool.splice(idx, 1);
      }, (dur + 3) * 1000); // keep alive through reverb tail
    } catch (e) {}
  }

  // --- Sound design ---

  // D minor pentatonic, octave 3-4: audible on laptop speakers (150-600Hz)
  // D3 E3 F3 A3 Bb3 D4
  static REVEAL_NOTES = [
    146.83,  // D3
    164.81,  // E3
    174.61,  // F3
    220.00,  // A3
    233.08,  // Bb3
    293.66,  // D4
  ];

  /**
   * Scale factor for tone durations based on current show time.
   * At 2500ms (base) = 1.0, at 5000ms = 1.6, at 1000ms = 0.7.
   * Clamped so tones don't get absurdly long or short.
   */
  _durScale() {
    const base = 2500;
    const ratio = this.effectiveShowMs / base;
    return Math.max(0.5, Math.min(ratio, 3.0));
  }

  _playPrime() {
    this._playTone(65, 0.10, 0.15, { type: 'sine', reverb: true, attack: 0.005, release: 0.12 });
    this._playTone(220, 0.08, 0.1, { type: 'triangle', reverb: true, attack: 0.003, release: 0.08 });
    this._playTone(1200, 0.04, 0.02, { type: 'square', reverb: false, attack: 0.001, release: 0.015 });
  }

  _playReveal(keyIndex) {
    const s = this._durScale();
    const note = Hypnagog.REVEAL_NOTES[keyIndex % Hypnagog.REVEAL_NOTES.length];
    this._playTone(note / 2, 0.10, 0.6 * s, { type: 'sine', reverb: true, attack: 0.01, release: 0.5 * s });
    this._playTone(note, 0.14, 0.8 * s, { type: 'sine', reverb: true, attack: 0.015, release: 0.6 * s });
    this._playTone(note * 1.498, 0.04, 0.5 * s, { type: 'sine', reverb: true, attack: 0.03, release: 0.4 * s });
    this._playTone(note * 2, 0.03, 0.35 * s, { type: 'sine', reverb: true, attack: 0.025, release: 0.3 * s });
    this._playTone(800, 0.03, 0.025, { type: 'square', reverb: false, attack: 0.001, release: 0.02 });
  }

  _playAppear() {
    const s = this._durScale();
    this._playTone(73.42, 0.07, 0.6 * s, { type: 'sine', reverb: true, attack: 0.04, release: 0.5 * s });
    this._playTone(146.83, 0.08, 0.6 * s, { type: 'sine', reverb: true, attack: 0.05, release: 0.5 * s });
    this._playTone(220, 0.05, 0.4 * s, { type: 'sine', reverb: true, attack: 0.06, release: 0.3 * s });
  }

  _playFadeOut() {
    const s = this._durScale();
    // Closing chord: D3 + A3 perfect fifth (mid, all speakers)
    this._playTone(146.83, 0.07, 1.5 * s, { type: 'sine', reverb: true, attack: 0.01, release: 1.4 * s });
    this._playTone(220.00, 0.05, 1.3 * s, { type: 'sine', reverb: true, attack: 0.01, release: 1.2 * s });
    // Octave lower: D2 + A2 for warmth/body (headphones, good speakers)
    this._playTone(73.42, 0.06, 1.6 * s, { type: 'sine', reverb: true, attack: 0.02, release: 1.5 * s });
    this._playTone(110.00, 0.04, 1.4 * s, { type: 'sine', reverb: true, attack: 0.02, release: 1.3 * s });
    // Soft low click to mark the moment (triangle, 300Hz — warm, not sharp)
    this._playTone(300, 0.03, 0.04, { type: 'triangle', reverb: true, attack: 0.002, release: 0.035 });
  }

  // ==================== PARTICLES ====================

  _initParticles() {
    this.particleCanvas = document.createElement('canvas');
    this.particleCanvas.className = 'psycho-particles';
    this.particleCtx = this.particleCanvas.getContext('2d');
    this.particles = [];
    this._resizeParticles();
    window.addEventListener('resize', () => this._resizeParticles());
    for (let i = 0; i < 80; i++) this._spawnParticle(true);
    this._animateParticles();
  }

  _resizeParticles() {
    if (!this.particleCanvas) return;
    this.particleCanvas.width = window.innerWidth;
    this.particleCanvas.height = window.innerHeight;
  }

  _spawnParticle(random) {
    const w = this.particleCanvas ? this.particleCanvas.width : window.innerWidth;
    const h = this.particleCanvas ? this.particleCanvas.height : window.innerHeight;
    this.particles.push({
      x: Math.random() * w,
      y: random ? Math.random() * h : h + 10,
      vx: (Math.random() - 0.5) * 0.4,
      vy: -(Math.random() * 0.5 + 0.15),
      size: Math.random() * 1.8 + 0.4,
      alpha: Math.random() * 0.12 + 0.02,
      life: random ? Math.random() : 0,
      maxLife: Math.random() * 0.5 + 0.5,
      hue: Math.random() * 60 + 30,
    });
  }

  _animateParticles() {
    if (!this.active || !this.particleCtx) return;
    const ctx = this.particleCtx;
    const w = this.particleCanvas.width;
    const h = this.particleCanvas.height;
    ctx.clearRect(0, 0, w, h);

    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.x += p.vx; p.y += p.vy; p.life += 0.002;
      if (p.life > p.maxLife || p.y < -10 || p.x < -10 || p.x > w + 10) {
        this.particles.splice(i, 1); continue;
      }
      const lf = p.life / p.maxLife;
      let op = p.alpha;
      if (lf < 0.1) op *= lf / 0.1;
      if (lf > 0.7) op *= (1 - lf) / 0.3;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${p.hue}, 60%, 75%, ${op})`;
      ctx.fill();
    }

    while (this.particles.length < 80) this._spawnParticle(false);
    this.particleRAF = requestAnimationFrame(() => this._animateParticles());
  }

  _destroyParticles() {
    if (this.particleRAF) { cancelAnimationFrame(this.particleRAF); this.particleRAF = null; }
    if (this.particleCanvas && this.particleCanvas.parentNode) this.particleCanvas.remove();
    this.particleCanvas = null; this.particleCtx = null; this.particles = [];
  }

  _destroyAudio() {
    this._nodePool = [];
    if (this.audioCtx) {
      try { this.audioCtx.close(); } catch (e) {}
      this.audioCtx = null;
      this.masterGain = null;
      this.reverbSend = null;
      this.reverbReturn = null;
      this.convolver = null;
    }
  }

  // ==================== LIFECYCLE ====================

  start(items) {
    if (!items || items.length === 0) return;
    this.items = this._shuffle([...items]);
    this.currentIndex = 0;
    this.round = 1;
    this.phase = 'idle';
    this.paused = false;
    this.active = true;
    this.revealIndex = -1;
    this.revealKeys = [];

    this._initAudio();
    this._createContainer();
    this._injectStyles();
    this._bindKeys();
    this._render();

    if (this.visualMode === 2) {
      this._initParticles();
      this.container.appendChild(this.particleCanvas);
    }

    setTimeout(() => { if (this.active) this._runSequence(); }, 1500);
  }

  stop() {
    this.active = false;
    this.paused = false;
    if (this.timeout) { clearTimeout(this.timeout); this.timeout = null; }
    this._unbindKeys();
    this._destroyParticles();
    this._destroyAudio();
    this._removeContainer();
    if (this.onExit) this.onExit({ itemsRemaining: this.items.length, roundsCompleted: this.round - 1 });
  }

  setMode(mode) {
    this.visualMode = mode === 1 ? 1 : 2;
    if (this.visualMode === 2 && !this.particleCanvas && this.container) {
      this._initParticles();
      this.container.appendChild(this.particleCanvas);
    } else if (this.visualMode === 1) {
      this._destroyParticles();
    }
    this._render();
  }

  toggleCloze() { this.clozeMode = !this.clozeMode; this._render(); }

  goTo(index) {
    if (!this.active) return;
    if (this.timeout) clearTimeout(this.timeout);
    this.currentIndex = Math.max(0, Math.min(index, this.items.length - 1));
    const item = this.items[this.currentIndex];
    this.revealKeys = this._getKeys(item);
    this.revealIndex = 999;
    this.phase = 'show';
    this._render();
    if (!this.paused) this._scheduleConsolidate();
  }

  /** Go back: move to previous card and restart the full sequence (prime → blanks → reveal) */
  goBack() {
    if (!this.active) return;
    if (this.timeout) clearTimeout(this.timeout);
    this.currentIndex = Math.max(0, this.currentIndex - 1);
    this.revealIndex = -1;
    this.revealKeys = [];
    if (this.paused) {
      this.paused = false;
    }
    this._runSequence();
  }

  dismiss() {
    if (!this.active || this.items.length <= 1) return;
    if (this.timeout) clearTimeout(this.timeout);
    const dismissed = this.items[this.currentIndex];
    this.items.splice(this.currentIndex, 1);
    if (this.currentIndex >= this.items.length) this.currentIndex = this.items.length - 1;
    if (this.onItemDismiss) this.onItemDismiss(dismissed);
    this.revealIndex = -1;
    this._render();
    if (!this.paused) {
      setTimeout(() => { if (this.active && !this.paused) this._runSequence(); }, 300);
    }
  }

  /** Arrow Up: re-insert current card 2-4 positions ahead for another look */
  prioritize() {
    if (!this.active || this.items.length <= 1) return;
    const item = this.items[this.currentIndex];
    const insertAt = Math.min(
      this.currentIndex + 2 + Math.floor(Math.random() * 3),
      this.items.length
    );
    this.items.splice(insertAt, 0, { ...item });
  }

  togglePause() {
    if (!this.active) return;
    this.paused = !this.paused;
    if (this.paused) {
      if (this.timeout) { clearTimeout(this.timeout); this.timeout = null; }
    } else {
      if (this.phase === 'show') {
        const item = this.items[this.currentIndex];
        if (this._isBasic(item)) {
          const backEl = this.container && this.container.querySelector('.basic-back');
          if (backEl && backEl.classList.contains('basic-hidden')) {
            this._revealBack();
          } else {
            this._startBreathing();
            this._scheduleConsolidate();
          }
        } else if (this._hasClozeReveal() && this.revealIndex < this.revealKeys.length - 1) {
          this._scheduleNextReveal();
        } else {
          this._startBreathing();
          this._scheduleConsolidate();
        }
      } else if (this.phase === 'consolidate') {
        this.timeout = setTimeout(() => {
          this._advanceToNext();
        }, 1000);
      } else {
        this._runSequence();
      }
    }
    this._renderPauseOnly();
  }

  // ==================== UTILS ====================

  _shuffle(arr) {
    const a = [...arr];
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  _createContainer() {
    this.container = document.createElement('div');
    this.container.id = 'hypnagog-container';
    document.body.appendChild(this.container);
  }

  _removeContainer() {
    if (this.container) { this.container.remove(); this.container = null; }
    const s = document.getElementById('hypnagog-styles');
    if (s) s.remove();
  }

  _bindKeys() { window.addEventListener('keydown', this._boundKeyHandler); }
  _unbindKeys() { window.removeEventListener('keydown', this._boundKeyHandler); }

  _handleKeyDown(e) {
    if (!this.active) return;
    switch (e.key) {
      case 'Escape':     this.stop(); break;
      case '1':          this.setMode(1); break;
      case '2':          this.setMode(2); break;
      case '3':          this.toggleCloze(); break;
      case 'ArrowLeft':  e.preventDefault(); this.goBack(); break;
      case 'ArrowRight': e.preventDefault(); this.goTo(this.currentIndex + 1); break;
      case 'ArrowUp':    e.preventDefault(); this.prioritize(); break;
      case 'ArrowDown':  e.preventDefault(); this.dismiss(); break;
      case ' ':          e.preventDefault(); this.togglePause(); break;
    }
  }

  _getKeys(item) {
    if (!item || !item.key) return [];
    return item.key.split(' / ').map(k => k.trim()).filter(k => k.length > 0);
  }

  _hasClozeReveal() {
    return this.clozeMode && this.revealKeys.length > 0;
  }

  _isBasic(item) {
    return item && item.front && item.back && !item.cloze;
  }

  _escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  // --- Contextual anchor: unique hue per card ---

  _cardHue(item) {
    const str = item.text || item.key || '';
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
    }
    return Math.abs(hash) % 360;
  }

  _startBreathing() {
    if (!this.container) return;
    const showEl = this.container.querySelector('.psycho-show');
    if (showEl) showEl.classList.add('px-breathing');
  }

  // ==================== SEQUENCE ====================

  _runSequence() {
    if (!this.active || this.paused) return;

    if (this.currentIndex >= this.items.length) {
      this.round++;
      this.items = this._shuffle(this.items);
      this.currentIndex = 0;
      this.revealIndex = -1;

      if (this.options.progressiveSpeed) {
        this.effectiveShowMs = Math.max(800, Math.round(this.effectiveShowMs * 0.9));
      }

      this._render();
      this.timeout = setTimeout(() => {
        if (this.active && !this.paused) this._runSequence();
      }, this.options.roundPauseMs);
      return;
    }

    const item = this.items[this.currentIndex];
    this.revealKeys = this._getKeys(item);
    this.revealIndex = this.clozeMode ? -1 : 999;

    this.phase = 'prime';
    this._render();
    this._playPrime();

    this.timeout = setTimeout(() => {
      if (!this.active || this.paused) return;

      this.phase = 'show';
      this._render();
      this._playAppear();

      if (this._isBasic(item)) {
        this.timeout = setTimeout(() => {
          if (!this.active || this.paused) return;
          this._revealBack();
        }, this.options.blankMs);
      } else if (this._hasClozeReveal()) {
        this.timeout = setTimeout(() => {
          if (!this.active || this.paused) return;
          this._revealNext();
        }, this.options.blankMs);
      } else {
        this._startBreathing();
        this._scheduleConsolidate();
      }
    }, this.options.primeMs);
  }

  /** Reveal back side of a basic card */
  _revealBack() {
    if (!this.active || this.paused) return;
    if (this.container) {
      const backEl = this.container.querySelector('.basic-back');
      if (backEl) {
        backEl.classList.remove('basic-hidden');
        backEl.classList.add('basic-revealed');
      }
    }
    this._playReveal(1);
    this._startBreathing();
    this._scheduleConsolidate();
  }

  _revealNext() {
    if (!this.active || this.paused) return;
    this.revealIndex++;
    this._updateSlots();
    this._playReveal(this.revealIndex);

    if (this.revealIndex < this.revealKeys.length - 1) {
      this._scheduleNextReveal();
    } else {
      this._startBreathing();
      this._scheduleConsolidate();
    }
  }

  _scheduleNextReveal() {
    this.timeout = setTimeout(() => {
      if (!this.active || this.paused) return;
      this._revealNext();
    }, this.options.revealMs);
  }

  _scheduleConsolidate() {
    const holdMs = Math.max(600, this.effectiveShowMs * 0.4);
    this.timeout = setTimeout(() => {
      if (!this.active || this.paused) return;
      this._doConsolidate();
    }, holdMs);
  }

  _doConsolidate() {
    this.phase = 'consolidate';

    if (this.container) {
      const showEl = this.container.querySelector('.psycho-show');
      if (showEl) {
        // Step 1: Remove breathing, add pulse (text flares up)
        showEl.classList.remove('px-breathing', 'psycho-show-clean', 'psycho-show-drift');
        void showEl.offsetWidth;
        showEl.classList.add('px-pulse');

        // Step 2: At pulse peak — tone + fade start simultaneously
        setTimeout(() => {
          if (!this.active) return;

          // Tone marks this exact moment
          this._playFadeOut();

          // Swap pulse → afterglow fade
          showEl.classList.remove('px-pulse');
          void showEl.offsetWidth;
          showEl.classList.add(this.visualMode === 2 ? 'psycho-consolidate-glow' : 'psycho-consolidate-clean');
        }, 700);
      }
    }

    // Pulse (700ms) + fade (1.4s) + dark gap (0.5s) = 2.6s
    this.timeout = setTimeout(() => {
      this._advanceToNext();
    }, 2600);
  }

  _advanceToNext() {
    if (!this.active) return;
    if (this.paused) {
      // If paused during dark gap, wait for unpause
      // togglePause will call _runSequence when resumed
      return;
    }
    this.currentIndex++;
    this._runSequence();
  }

  // ==================== RENDERING ====================

  _findKeySegments(text, keys) {
    const segments = [];
    const tl = text.toLowerCase();
    for (let ki = 0; ki < keys.length; ki++) {
      const kl = keys[ki].toLowerCase();
      let pos = 0;
      while (true) {
        const idx = tl.indexOf(kl, pos);
        if (idx === -1) break;
        segments.push({ start: idx, end: idx + keys[ki].length, keyIndex: ki, key: keys[ki] });
        pos = idx + keys[ki].length;
      }
    }
    if (!segments.length) return [];
    segments.sort((a, b) => a.start - b.start);
    const merged = [segments[0]];
    for (let i = 1; i < segments.length; i++) {
      if (segments[i].start >= merged[merged.length - 1].end) merged.push(segments[i]);
    }
    return merged;
  }

  // Colors in BOTH modes
  _renderShowContent(item) {
    if (this._isBasic(item)) {
      return this._renderBasicContent(item);
    }

    const text = item.text;
    const keys = this.revealKeys;
    const segs = keys.length ? this._findKeySegments(text, keys) : [];

    if (!segs.length) return this._escapeHtml(text);

    let result = '';
    let pos = 0;
    for (const seg of segs) {
      result += this._escapeHtml(text.slice(pos, seg.start));

      const answer = this._escapeHtml(text.slice(seg.start, seg.end));
      const col = this._getColor(seg.keyIndex);
      const styleAttr = ` style="--kc:${col.main};--kg:${col.glow};--kf:${col.far}"`;
      const revealed = seg.keyIndex <= this.revealIndex;
      const justNow = revealed && seg.keyIndex === this.revealIndex;

      if (this.clozeMode) {
        const cls = revealed
          ? `ps-slot ps-revealed${justNow ? ' ps-just-revealed' : ''}`
          : 'ps-slot ps-blanked';
        result += `<span class="${cls}" data-ki="${seg.keyIndex}"${styleAttr}>`;
        result += `<span class="ps-sizer">${answer}</span>`;
        result += `<span class="ps-dots">[...]</span>`;
        result += `</span>`;
      } else {
        result += `<span class="ps-hl"${styleAttr}>${answer}</span>`;
      }

      pos = seg.end;
    }
    result += this._escapeHtml(text.slice(pos));
    return result;
  }

  _updateSlots() {
    if (!this.container) return;
    const slots = this.container.querySelectorAll('.ps-slot');
    for (const slot of slots) {
      const ki = parseInt(slot.getAttribute('data-ki'), 10);
      if (ki <= this.revealIndex) {
        if (ki === this.revealIndex && slot.classList.contains('ps-blanked')) {
          slot.className = 'ps-slot ps-revealed ps-just-revealed';
        } else if (!slot.classList.contains('ps-revealed')) {
          slot.className = 'ps-slot ps-revealed';
        }
      }
    }
  }

  /**
   * Render basic (non-cloze) card as two blocks:
   *   Front (question) — visible immediately, colored
   *   Back (answer)    — starts hidden, revealed after delay
   */
  _renderBasicContent(item) {
    const backCol = this._getColor(0); // gold

    const front = this._escapeHtml(item.front);
    const back  = this._escapeHtml(item.back);

    return `<div class="basic-layout">` +
      `<div class="basic-front">${front}</div>` +
      `<div class="basic-divider"></div>` +
      `<div class="basic-back basic-hidden" style="--kc:${backCol.main};--kg:${backCol.glow};--kf:${backCol.far}">${back}</div>` +
    `</div>`;
  }

  // Colors in BOTH modes for prime
  _renderPrimeContent(item) {
    const keys = this._getKeys(item);
    if (!keys.length) return '';
    const parts = keys.map((k, i) => {
      const col = this._getColor(i);
      return `<span style="color:${col.main};text-shadow:0 0 30px ${col.glow}">${this._escapeHtml(k)}</span>`;
    });
    const label = parts.join(' <span class="ps-sep">/</span> ');
    if (item.cloze && this.clozeMode) {
      return `<span class="ps-bracket">[</span>${label}<span class="ps-bracket">]</span>`;
    }
    return label;
  }

  _renderPauseOnly() {
    if (!this.container) return;
    const existing = this.container.querySelector('.psycho-pause-indicator');
    if (this.paused && !existing) {
      const div = document.createElement('div');
      div.className = 'psycho-pause-indicator';
      div.innerHTML = '<div class="psycho-pause-bar"></div><div class="psycho-pause-bar"></div>';
      this.container.appendChild(div);
    } else if (!this.paused && existing) {
      existing.remove();
    }
  }

  _render() {
    if (!this.container) return;

    const item = this.items[this.currentIndex];
    const progress = ((this.currentIndex + 1) / this.items.length) * 100;
    const enh = this.visualMode === 2;

    this.container.className = enh ? 'px-mode-polybius' : 'px-mode-clean';

    let c = '';

    if (enh) {
      c += '<div class="px-vignette"></div>';
      c += '<div class="px-scanlines"></div>';
      c += '<div class="px-grain"></div>';
    }

    // Contextual anchor: unique subtle background hue per card
    if (item && this.phase === 'show') {
      const hue = this._cardHue(item);
      c += `<div class="px-anchor" style="background:radial-gradient(ellipse at center, hsla(${hue},40%,8%,0.35) 0%, transparent 65%)"></div>`;
    }

    c += `<div class="px-progress"><div class="px-progress-fill ${enh ? 'enh' : ''}" style="width:${progress}%"></div></div>`;

    if (this.paused) {
      c += '<div class="psycho-pause-indicator"><div class="psycho-pause-bar"></div><div class="psycho-pause-bar"></div></div>';
    }

    if (this.phase === 'idle') {
      c += `<div class="px-idle ${enh ? 'enh' : ''}"><div class="px-title">HYPNAGOG</div><div class="px-sub">${this.items.length} items</div></div>`;
    } else if (this.phase === 'prime' && item) {
      c += `<div class="psycho-prime ${enh ? 'enh' : ''}">${this._renderPrimeContent(item)}</div>`;
    } else if (this.phase === 'show' && item) {
      c += `<div class="psycho-show ${enh ? 'psycho-show-drift' : 'psycho-show-clean'}">${this._renderShowContent(item)}</div>`;
    }

    const modeLabel = `${this.visualMode} ${this.clozeMode ? 'CLOZE' : 'FULL'}`;
    c += `<div class="px-hud px-mode">${modeLabel}</div>`;
    const speedPct = this.options.progressiveSpeed
      ? ` ${Math.round(this.effectiveShowMs / this.options.showMs * 100)}%`
      : '';
    c += `<div class="px-hud px-round">R${this.round}${speedPct}</div>`;
    c += `<div class="px-hud px-counter">${this.currentIndex + 1}/${this.items.length}</div>`;

    this.container.innerHTML = c;

    if (enh && this.particleCanvas && this.container) {
      this.container.appendChild(this.particleCanvas);
    }
  }

  // ==================== STYLES ====================

  _injectStyles() {
    if (document.getElementById('hypnagog-styles')) return;
    const style = document.createElement('style');
    style.id = 'hypnagog-styles';
    style.textContent = `

/* ===== Container ===== */

#hypnagog-container {
  position: fixed; inset: 0;
  display: flex; align-items: center; justify-content: center;
  z-index: 999999; cursor: none;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}

.px-mode-clean {
  background: #000;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
}
.px-mode-polybius {
  background: #030305;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
}

/* ===== Mode 1 overrides ===== */

.px-mode-clean .psycho-show {
  color: #f0f0f0; font-size: 2.35rem;
  text-shadow: 0 0 30px rgba(255,255,255,0.15);
}
.px-mode-clean .ps-just-revealed .ps-sizer {
  animation: pxRevealClean 500ms ease-out forwards;
}
.px-mode-clean .psycho-prime {
  text-shadow: 0 0 30px rgba(255,255,255,0.5);
}
.px-mode-clean .px-idle .px-title {
  color: #fbbf24;
  text-shadow: 0 0 40px rgba(251,191,36,0.25);
}
.px-mode-clean .psycho-consolidate-clean,
.px-mode-clean .psycho-consolidate-glow {
  animation: pxFadeOut 1400ms ease-out forwards !important;
}
.px-mode-clean .basic-revealed {
  animation: pxBasicRevealClean 500ms ease-out forwards;
}
.px-mode-clean .px-pulse {
  animation: pxPulseClean 700ms cubic-bezier(0.0, 0, 0.2, 1) forwards !important;
}

/* ===== Atmosphere (mode 2 only) ===== */

.px-vignette {
  position: absolute; inset: 0; pointer-events: none;
  background: radial-gradient(ellipse at center,
    transparent 0%, transparent 40%,
    rgba(0,0,0,0.3) 65%, rgba(0,0,0,0.7) 100%);
  animation: pxVignette 10s ease-in-out infinite;
}

.px-scanlines { 
  position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(to bottom,
    transparent 0px, transparent 2px,
    rgba(0,0,0,0.15) 2px, rgba(0,0,0,0.15) 4px);
  z-index: 2;
}

.px-grain {
  position: absolute; inset: 0; pointer-events: none; z-index: 1;
  opacity: 0.035;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  background-size: 256px 256px;
  animation: pxGrain 0.4s steps(5) infinite;
}

.psycho-particles {
  position: absolute; inset: 0; pointer-events: none; z-index: 1;
}

/* ===== HUD ===== */

.px-progress { position: absolute; bottom: 0; left: 0; right: 0; height: 1px; background: #0a0a0f; z-index: 12; }
.px-progress-fill { height: 100%; background: #1f1f2e; transition: width 0.8s cubic-bezier(0.4,0,0.2,1); }
.px-progress-fill.enh { background: linear-gradient(90deg, #78350f, #b45309, #d97706); }

.px-hud {
  position: absolute; z-index: 12;
  color: #2a2a3a; font-size: 0.65rem; letter-spacing: 0.2em;
}
.px-mode  { top: 1rem; right: 1rem; }
.px-round { bottom: 1rem; left: 1rem; }
.px-counter { bottom: 1rem; right: 1rem; }

.psycho-pause-indicator {
  position: absolute; top: 50%; left: 32px; transform: translateY(-50%);
  display: flex; gap: 6px; opacity: 0.1; z-index: 12;
}
.psycho-pause-bar { width: 6px; height: 28px; background: #fff; border-radius: 1px; }

/* ===== Idle ===== */

.px-idle { text-align: center; z-index: 5; }
.px-title {
  color: #3a3a4a; font-size: 1.3rem; font-weight: 400;
  letter-spacing: 0.3em; margin-bottom: 0.75rem;
}
.px-idle.enh .px-title {
  color: #fbbf24;
  text-shadow: 0 0 50px rgba(251,191,36,0.2), 0 0 100px rgba(251,191,36,0.1);
  animation: pxIdlePulse 4s ease-in-out infinite;
}
.px-sub { color: #2a2a3a; font-size: 0.75rem; letter-spacing: 0.15em; }

/* ===== Prime ===== */

.psycho-prime {
  color: #fff; font-size: 3rem; font-weight: 600; letter-spacing: 0.15em; z-index: 5;
  text-shadow: 0 0 30px rgba(255,255,255,0.5);
  animation: pxPrime 80ms ease-out forwards;
}
.psycho-prime.enh {
  text-shadow: 0 0 40px rgba(255,255,255,0.5),
    -2px 0 0 rgba(255,100,80,0.3), 2px 0 0 rgba(80,180,255,0.3),
    0 0 80px rgba(251,191,36,0.2);
  animation: pxPrimeEnh 100ms ease-out forwards;
}
.ps-bracket { color: #d97706; opacity: 0.5; font-weight: 300; }
.ps-sep { color: #3a3a4a; opacity: 0.4; font-weight: 300; margin: 0 0.4em; }

/* ===== Show ===== */

.psycho-show {
  color: #c8c8d8; font-size: 2rem; font-weight: 300; letter-spacing: 0.04em;
  text-align: center; padding: 0 3.5rem; max-width: 62rem; line-height: 1.6;
  z-index: 5; position: relative;
}
.psycho-show-clean { animation: pxFadeIn 700ms cubic-bezier(0.16,1,0.3,1) forwards; }
.psycho-show-drift {
  text-shadow: 0 0 25px rgba(255,255,255,0.06);
  animation: pxShowEnh 1200ms cubic-bezier(0.16,1,0.3,1) forwards,
             pxDrift 16s ease-in-out 1.2s infinite;
}

/* ===== Cloze Slots ===== */

.ps-slot { position: relative; display: inline; }
.ps-sizer { display: inline; }
.ps-dots {
  position: absolute; left: 0; top: 0; width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  pointer-events: none; color: #4a4a5a; font-style: italic;
}

.ps-blanked .ps-sizer { visibility: hidden; }
.ps-blanked .ps-dots { visibility: visible; }

.ps-revealed .ps-sizer {
  visibility: visible;
  color: var(--kc, #fbbf24);
  text-shadow: 0 0 20px var(--kg, rgba(251,191,36,0.5)),
               0 0 50px var(--kf, rgba(245,158,11,0.3));
}
.ps-revealed .ps-dots { visibility: hidden; }

.ps-just-revealed .ps-sizer {
  animation: pxReveal 600ms cubic-bezier(0.16,1,0.3,1) forwards;
}

.ps-hl {
  color: var(--kc, #fbbf24);
  text-shadow: 0 0 20px var(--kg, rgba(251,191,36,0.5)),
               0 0 50px var(--kf, rgba(245,158,11,0.3));
}

/* ===== Basic Card (front/back) ===== */

.basic-layout {
  text-align: center; z-index: 5;
  display: flex; flex-direction: column; align-items: center; gap: 0;
  padding: 0 3.5rem; max-width: 62rem;
}

.basic-front {
  font-size: 1.4rem; font-weight: 500; letter-spacing: 0.06em;
  line-height: 1.5;
  color: #e5e7eb;
  text-shadow: 0 0 20px rgba(255,255,255,0.15);
  animation: pxFadeIn 600ms cubic-bezier(0.16,1,0.3,1) forwards;
}

.basic-divider {
  width: 60px; height: 1px; margin: 1.2rem 0;
  background: #2a2a3a;
  opacity: 0;
  transition: opacity 0.5s ease;
}

.basic-back {
  font-size: 1.9rem; font-weight: 300; letter-spacing: 0.04em;
  line-height: 1.6;
}

.basic-hidden {
  opacity: 0;
  transform: translateY(8px);
}

.basic-revealed {
  color: var(--kc, #fbbf24);
  text-shadow: 0 0 20px var(--kg, rgba(251,191,36,0.5)),
               0 0 50px var(--kf, rgba(245,158,11,0.3));
  animation: pxBasicReveal 700ms cubic-bezier(0.16,1,0.3,1) forwards;
}

.basic-layout:has(.basic-revealed) .basic-divider {
  opacity: 0.4;
}

/* ===== Contextual Anchor ===== */

.px-anchor {
  position: absolute; inset: 0; pointer-events: none; z-index: 0;
  transition: opacity 0.8s ease;
}

/* ===== Text Breathing ===== */

.px-breathing {
  animation: pxBreathe 4s ease-in-out infinite !important;
}

/* ===== Retention Pulse (text flares before fade) ===== */

.px-pulse {
  animation: pxPulse 700ms cubic-bezier(0.0, 0, 0.2, 1) forwards !important;
}

/* ===== Consolidate ===== */

.psycho-consolidate-clean {
  animation: pxFadeOut 1400ms ease-out forwards !important;
}
.psycho-consolidate-glow {
  animation: pxAfterglow 1400ms ease-out forwards !important;
}

/* ===== Animations ===== */

@keyframes pxPrime {
  0%   { opacity: 0; transform: scale(1.4); }
  50%  { opacity: 1; transform: scale(1.08); }
  100% { opacity: 0; transform: scale(1); }
}
@keyframes pxPrimeEnh {
  0%   { opacity: 0; transform: scale(1.6); }
  50%  { opacity: 0.9; transform: scale(1.1); }
  100% { opacity: 0; transform: scale(1); }
}
@keyframes pxFadeIn {
  0%   { opacity: 0; transform: translateY(6px); }
  100% { opacity: 1; transform: translateY(0); }
}
@keyframes pxFadeOut {
  0%   { opacity: 1; }
  100% { opacity: 0; }
}
@keyframes pxShowEnh {
  0%   { opacity: 0; transform: translateY(10px) scale(0.99); }
  70%  { opacity: 0.95; }
  100% { opacity: 1; transform: translateY(0) scale(1); }
}
@keyframes pxDrift {
  0%   { transform: translate(0,0); }
  25%  { transform: translate(1px,-0.8px); }
  50%  { transform: translate(-0.8px,1px); }
  75%  { transform: translate(-1px,-0.4px); }
  100% { transform: translate(0,0); }
}
@keyframes pxReveal {
  0%   { visibility: visible; color: #4a4a5a; opacity: 0.3; transform: scale(1.04); }
  30%  { color: var(--kc, #fbbf24); transform: scale(1.02);
         text-shadow: 0 0 50px var(--kg), 0 0 100px var(--kf); }
  100% { color: var(--kc, #fbbf24); transform: scale(1);
         text-shadow: 0 0 20px var(--kg), 0 0 50px var(--kf); }
}
@keyframes pxRevealClean {
  0%   { visibility: visible; color: #6b7280; opacity: 0; transform: scale(1.04); }
  40%  { color: var(--kc, #fbbf24); opacity: 1; transform: scale(1.01);
         text-shadow: 0 0 25px var(--kg); }
  100% { color: var(--kc, #fbbf24); opacity: 1; transform: scale(1);
         text-shadow: 0 0 15px var(--kg); }
}
@keyframes pxAfterglow {
  0%   { opacity: 1; transform: scale(1); }
  30%  { opacity: 0.7; transform: scale(1.002);
         text-shadow: 0 0 40px rgba(255,255,255,0.15); }
  60%  { opacity: 0.3; transform: scale(1.004); filter: blur(1.5px);
         text-shadow: 0 0 60px rgba(255,255,255,0.1); }
  85%  { opacity: 0.05; transform: scale(1.006); filter: blur(3px);
         text-shadow: 0 0 70px rgba(255,255,255,0.05); }
  100% { opacity: 0; transform: scale(1.008); filter: blur(4px);
         text-shadow: none; }
}
@keyframes pxVignette {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.85; }
}
@keyframes pxGrain {
  0%   { background-position: 0 0; }
  20%  { background-position: 100px 50px; }
  40%  { background-position: 50px 120px; }
  60%  { background-position: 180px 30px; }
  80%  { background-position: 30px 180px; }
  100% { background-position: 0 0; }
}
@keyframes pxIdlePulse {
  0%, 100% { text-shadow: 0 0 50px rgba(251,191,36,0.2), 0 0 100px rgba(251,191,36,0.1); }
  50%      { text-shadow: 0 0 60px rgba(251,191,36,0.3), 0 0 120px rgba(251,191,36,0.15); }
}
@keyframes pxBasicReveal {
  0%   { opacity: 0; transform: translateY(8px); }
  50%  { opacity: 0.8; transform: translateY(2px); }
  100% { opacity: 1; transform: translateY(0); }
}
@keyframes pxBasicRevealClean {
  0%   { opacity: 0; transform: translateY(6px); }
  100% { opacity: 1; transform: translateY(0); }
}
@keyframes pxPulseClean {
  0%   { color: #f0f0f0; text-shadow: none; }
  10%  { color: #fff; text-shadow: none; }
  100% { color: #fff; text-shadow: none; }
}
@keyframes pxBreathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.96; transform: scale(1.003); }
}
@keyframes pxPulse {
  0%   { filter: brightness(1); text-shadow: none; }
  10%  { filter: brightness(2.5); text-shadow: none; }
  100% { filter: brightness(2.2); text-shadow: none; }
}
    `;
    document.head.appendChild(style);
  }
}

if (typeof module !== 'undefined' && module.exports) module.exports = Hypnagog;
if (typeof window !== 'undefined') window.Hypnagog = Hypnagog;
