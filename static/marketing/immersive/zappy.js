/* ============================================================================
   Zappy — one continuous entity.
   A constellation of connected signal-nodes with a stable signature that
   morphs between states (idle / greet / listen / recommend / connect / secure).
   Pure 2D canvas. Render loop is gated: it only runs while a short transition
   is animating or the pointer is active near it — never a permanent 60fps loop.
   ========================================================================== */
(function () {
  "use strict";

  var N = 16;                 // node count — recognizable, not busy
  var STATES = {
    idle:      circle(0.62, 0.0),
    greet:     circle(0.92, 0.35),
    listen:    lean(),
    recommend: arrow(),
    connect:   bridge(),
    secure:    lattice()
  };

  function circle(r, jitter) {
    var pts = [];
    for (var i = 0; i < N; i++) {
      var a = (i / N) * Math.PI * 2;
      var rr = r * (1 - jitter * (i % 3 === 0 ? 1 : 0));
      pts.push([Math.cos(a) * rr, Math.sin(a) * rr]);
    }
    return pts;
  }
  function lean() {
    var pts = circle(0.6, 0);
    for (var i = 0; i < pts.length; i++) pts[i][0] += 0.28;
    return pts;
  }
  function arrow() {
    var pts = [];
    for (var i = 0; i < N; i++) {
      var t = i / (N - 1);
      if (i < N / 2) pts.push([-0.7 + t * 1.4, -0.55 + t * 1.1]);
      else pts.push([0.7 - (t - 0.5) * 1.4, 0.55 - (t - 0.5) * 1.1]);
    }
    return pts;
  }
  function bridge() {
    var pts = [];
    for (var i = 0; i < N; i++) {
      var t = i / (N - 1);
      pts.push([-0.85 + t * 1.7, Math.sin(t * Math.PI) * -0.45]);
    }
    return pts;
  }
  function lattice() {
    var pts = [], g = 4;
    for (var i = 0; i < N; i++) {
      var x = (i % g) / (g - 1) - 0.5;
      var y = Math.floor(i / g) / (g - 1) - 0.5;
      pts.push([x * 1.1, y * 1.1]);
    }
    return pts;
  }

  function Zappy(canvas) {
    this.c = canvas;
    this.ctx = canvas.getContext("2d");
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.cur = STATES.idle.map(function (p) { return p.slice(); });
    this.target = STATES.idle;
    this.raf = 0;
    this.settleFrames = 0;
    this.hue = 205;           // cool-warm signature — shifts slightly by state
    this.resize();
    window.addEventListener("resize", this.resize.bind(this), { passive: true });
    this.paint();             // first static frame immediately
  }

  Zappy.prototype.resize = function () {
    var r = this.c.getBoundingClientRect();
    this.c.width = Math.max(1, r.width * this.dpr);
    this.c.height = Math.max(1, r.height * this.dpr);
    this.paint();
  };

  Zappy.prototype.setState = function (name, hue) {
    if (!STATES[name]) return;
    this.target = STATES[name];
    if (typeof hue === "number") this.hue = hue;
    this.settleFrames = 90;             // run the loop briefly, then stop
    if (!this.raf) this.loop();
  };

  Zappy.prototype.loop = function () {
    var self = this;
    var moved = 0;
    for (var i = 0; i < N; i++) {
      var dx = self.target[i][0] - self.cur[i][0];
      var dy = self.target[i][1] - self.cur[i][1];
      self.cur[i][0] += dx * 0.12;
      self.cur[i][1] += dy * 0.12;
      moved += Math.abs(dx) + Math.abs(dy);
    }
    self.paint();
    self.settleFrames--;
    if (moved > 0.004 || self.settleFrames > 0) {
      self.raf = requestAnimationFrame(self.loop.bind(self));
    } else {
      self.raf = 0;                    // fully idle — no loop
    }
  };

  Zappy.prototype.paint = function () {
    var ctx = this.ctx, w = this.c.width, h = this.c.height;
    var cx = w / 2, cy = h / 2, s = Math.min(w, h) * 0.42;
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = Math.max(1, this.dpr);
    // links
    ctx.strokeStyle = "hsla(" + this.hue + ", 80%, 68%, 0.28)";
    ctx.beginPath();
    for (var i = 0; i < N; i++) {
      var a = this.cur[i], b = this.cur[(i + 1) % N];
      ctx.moveTo(cx + a[0] * s, cy + a[1] * s);
      ctx.lineTo(cx + b[0] * s, cy + b[1] * s);
      if (i % 4 === 0) {
        var d = this.cur[(i + 5) % N];
        ctx.moveTo(cx + a[0] * s, cy + a[1] * s);
        ctx.lineTo(cx + d[0] * s, cy + d[1] * s);
      }
    }
    ctx.stroke();
    // nodes
    for (var j = 0; j < N; j++) {
      var p = this.cur[j];
      var rad = (j % 3 === 0 ? 2.6 : 1.7) * this.dpr;
      ctx.beginPath();
      ctx.arc(cx + p[0] * s, cy + p[1] * s, rad, 0, Math.PI * 2);
      ctx.fillStyle = "hsla(" + this.hue + ", 90%, 74%, " + (j % 3 === 0 ? 0.95 : 0.6) + ")";
      ctx.fill();
    }
    // core
    ctx.beginPath();
    ctx.arc(cx, cy, 3 * this.dpr, 0, Math.PI * 2);
    ctx.fillStyle = "hsla(" + this.hue + ", 95%, 82%, 0.95)";
    ctx.fill();
  };

  window.LUXitZappy = Zappy;
})();
