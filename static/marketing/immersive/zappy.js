/* ============================================================================
   Zappy — one continuous entity.
   A recognizable signature: a bright pulsing CORE, one forward "prow" node that
   points the way, an outer ring of signal-nodes, and a slow base rotation that
   gives it its own rhythm. It morphs between states (idle / greet / listen /
   recommend / connect / secure) but stays clearly the same entity.
   2D canvas. Render loop is gated — runs ~1.5s after a state change or while a
   gentle idle rhythm is requested, then stops. Never a permanent 60fps loop.
   ========================================================================== */
(function () {
  "use strict";

  var RING = 13;             // outer signal nodes
  var PROW_I = 0;            // node index that acts as the "prow"

  function ring(r, prowExtra, squash) {
    var pts = [];
    for (var i = 0; i < RING; i++) {
      var a = (i / RING) * Math.PI * 2 - Math.PI / 2;
      var rr = r + (i === PROW_I ? prowExtra : 0);
      pts.push([Math.cos(a) * rr, Math.sin(a) * rr * (squash || 1)]);
    }
    return pts;
  }
  function arrow() {
    var pts = ring(0.55, 0.0, 1);
    // pull the leading third forward into a point
    for (var i = 0; i < RING; i++) {
      var lead = i <= 2 || i >= RING - 2;
      if (lead) { pts[i][0] += 0.5; pts[i][1] *= 0.5; }
    }
    pts[PROW_I][0] += 0.35;
    return pts;
  }
  function lattice() {
    var pts = [], g = 4, k = 0;
    for (var i = 0; i < RING; i++) {
      var x = (k % g) / (g - 1) - 0.5;
      var y = Math.floor(k / g) / (g - 1) - 0.5;
      pts.push([x * 1.0, y * 1.0]);
      k++;
    }
    return pts;
  }
  function bridge() {
    var pts = [];
    for (var i = 0; i < RING; i++) {
      var t = i / (RING - 1);
      pts.push([-0.85 + t * 1.7, Math.sin(t * Math.PI) * -0.4]);
    }
    return pts;
  }

  var STATES = {
    idle:      { pts: ring(0.62, 0.10, 1),    hue: 205, spin: 0.0025, core: 0.9 },
    greet:     { pts: ring(0.9, 0.18, 1),     hue: 32,  spin: 0.012,  core: 1.15 },
    listen:    { pts: ring(0.6, 0.34, 0.86),  hue: 205, spin: 0.001,  core: 1.0 },
    recommend: { pts: arrow(),                hue: 213, spin: 0.004,  core: 1.05 },
    connect:   { pts: bridge(),               hue: 205, spin: 0.0,    core: 1.0 },
    secure:    { pts: lattice(),              hue: 218, spin: 0.0,    core: 0.85 }
  };

  function Zappy(canvas) {
    this.c = canvas;
    this.ctx = canvas.getContext("2d");
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.cur = STATES.idle.pts.map(function (p) { return p.slice(); });
    this.st = STATES.idle;
    this.rot = 0;
    this.t = 0;
    this.raf = 0;
    this.frames = 0;
    this.resize();
    window.addEventListener("resize", this.resize.bind(this), { passive: true });
    this.paint();
  }

  Zappy.prototype.resize = function () {
    var r = this.c.getBoundingClientRect();
    this.c.width = Math.max(1, r.width * this.dpr);
    this.c.height = Math.max(1, r.height * this.dpr);
    this.paint();
  };

  Zappy.prototype.setState = function (name) {
    var s = STATES[name];
    if (!s) return;
    this.st = s;
    this.frames = 130;                 // ~2s of animation then settle
    if (!this.raf) this.loop();
  };

  Zappy.prototype.loop = function () {
    var self = this, moved = 0;
    self.t += 0.05;
    self.rot += self.st.spin;
    for (var i = 0; i < RING; i++) {
      var dx = self.st.pts[i][0] - self.cur[i][0];
      var dy = self.st.pts[i][1] - self.cur[i][1];
      self.cur[i][0] += dx * 0.11;
      self.cur[i][1] += dy * 0.11;
      moved += Math.abs(dx) + Math.abs(dy);
    }
    self.paint();
    self.frames--;
    if (moved > 0.004 || self.frames > 0) self.raf = requestAnimationFrame(self.loop.bind(self));
    else self.raf = 0;
  };

  Zappy.prototype.paint = function () {
    var ctx = this.ctx, w = this.c.width, h = this.c.height;
    var cx = w / 2, cy = h / 2, s = Math.min(w, h) * 0.40, hue = this.st.hue;
    var cos = Math.cos(this.rot), sin = Math.sin(this.rot);
    function P(p) { return [cx + (p[0] * cos - p[1] * sin) * s, cy + (p[0] * sin + p[1] * cos) * s]; }

    ctx.clearRect(0, 0, w, h);

    // links from core out to each node + a light chord ring
    ctx.lineWidth = Math.max(1, this.dpr);
    ctx.strokeStyle = "hsla(" + hue + ",82%,68%,0.20)";
    ctx.beginPath();
    for (var i = 0; i < RING; i++) {
      var a = P(this.cur[i]);
      ctx.moveTo(cx, cy); ctx.lineTo(a[0], a[1]);
      var b = P(this.cur[(i + 1) % RING]);
      ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
    }
    ctx.stroke();

    // nodes
    for (var j = 0; j < RING; j++) {
      var p = P(this.cur[j]);
      var prow = j === PROW_I;
      var rad = (prow ? 3.4 : 1.8) * this.dpr;
      ctx.beginPath();
      ctx.arc(p[0], p[1], rad, 0, Math.PI * 2);
      ctx.fillStyle = prow
        ? "hsla(" + hue + ",95%,80%,1)"
        : "hsla(" + hue + ",88%,72%," + (j % 3 === 0 ? 0.9 : 0.5) + ")";
      ctx.fill();
      if (prow) {                          // a soft halo on the prow — the "face"
        ctx.beginPath();
        ctx.arc(p[0], p[1], rad * 2.4, 0, Math.PI * 2);
        ctx.fillStyle = "hsla(" + hue + ",95%,80%,0.14)";
        ctx.fill();
      }
    }

    // pulsing core
    var pulse = 1 + Math.sin(this.t) * 0.12;
    var cr = 4.4 * this.dpr * this.st.core * pulse;
    var grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, cr * 2.6);
    grad.addColorStop(0, "hsla(" + hue + ",95%,86%,0.95)");
    grad.addColorStop(1, "hsla(" + hue + ",95%,70%,0)");
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.arc(cx, cy, cr * 2.6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "hsla(" + hue + ",98%,90%,0.98)";
    ctx.beginPath(); ctx.arc(cx, cy, cr, 0, Math.PI * 2); ctx.fill();
  };

  window.LUXitZappy = Zappy;
})();
