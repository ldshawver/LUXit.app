/* ============================================================================
   Segmentation field — a customer population that reorganizes from one crowd
   into 4 labelled clusters as scroll progress goes 0 -> 1.
   The points read as customers: varying sizes, a handful of "representative"
   nodes carry initials, and each cluster gets a soft boundary as it forms.
   2D canvas. Render loop gated — runs only while `progress` is settling.
   ========================================================================== */
(function () {
  "use strict";

  var CLUSTERS = [
    { key: "recent",   name: "Recent",    x: 0.28, y: 0.30 },
    { key: "inactive", name: "Inactive",  x: 0.72, y: 0.30 },
    { key: "optedin",  name: "Opted In",  x: 0.30, y: 0.72 },
    { key: "followup", name: "Follow Up", x: 0.70, y: 0.72 }
  ];
  var INITIALS = ["AR", "JT", "MP", "SL", "DK", "RC", "EN", "BW"];

  function Field(canvas, opts) {
    opts = opts || {};
    this.c = canvas;
    this.ctx = canvas.getContext("2d");
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.count = opts.count || 460;
    this.progress = 0;
    this.shown = 0;
    this.highlight = -1;
    this.raf = 0;
    this.pts = [];
    this.build();
    this.resize();
    window.addEventListener("resize", this.resize.bind(this), { passive: true });
  }

  Field.prototype.build = function () {
    // uneven cluster sizes so the groups feel real
    var weights = [0.30, 0.18, 0.32, 0.20], acc = [];
    var s = 0; weights.forEach(function (w) { s += w; acc.push(s); });
    for (var i = 0; i < this.count; i++) {
      var r = Math.random(), cl = 0;
      while (cl < acc.length - 1 && r > acc[cl]) cl++;
      var rep = i < 8;                       // first 8 are representative (with initials)
      this.pts.push({
        cx: 0.5 + (Math.random() - 0.5) * 0.34,
        cy: 0.5 + (Math.random() - 0.5) * 0.38,
        tx: CLUSTERS[cl].x + (Math.random() - 0.5) * (rep ? 0.06 : 0.15),
        ty: CLUSTERS[cl].y + (Math.random() - 0.5) * (rep ? 0.06 : 0.15),
        cl: cl,
        r: rep ? 6 : (Math.random() < 0.14 ? 2.3 : 1.35),
        rep: rep ? INITIALS[i] : null
      });
    }
  };

  Field.prototype.resize = function () {
    var r = this.c.getBoundingClientRect();
    this.w = Math.max(1, r.width); this.h = Math.max(1, r.height);
    this.c.width = this.w * this.dpr; this.c.height = this.h * this.dpr;
    this.paint();
  };

  Field.prototype.setProgress = function (p) {
    this.progress = Math.max(0, Math.min(1, p));
    if (!this.raf) this.loop();
  };
  Field.prototype.setHighlight = function (idx) {
    this.highlight = idx;
    if (!this.raf) this.loop();
  };

  Field.prototype.loop = function () {
    var d = this.progress - this.shown;
    this.shown += d * 0.12;
    this.paint();
    if (Math.abs(this.progress - this.shown) > 0.002) {
      this.raf = requestAnimationFrame(this.loop.bind(this));
    } else { this.shown = this.progress; this.paint(); this.raf = 0; }
  };

  Field.prototype.paint = function () {
    var ctx = this.ctx, dpr = this.dpr, w = this.w, h = this.h, t = ease(this.shown);
    ctx.clearRect(0, 0, w * dpr, h * dpr);

    // soft cluster boundaries as the groups form
    if (t > 0.35) {
      for (var k = 0; k < CLUSTERS.length; k++) {
        var cl = CLUSTERS[k];
        var on = this.highlight === -1 || this.highlight === k;
        ctx.beginPath();
        ctx.arc(cl.x * w * dpr, cl.y * h * dpr, (58 + 10 * Math.sin(k)) * dpr * t, 0, Math.PI * 2);
        ctx.fillStyle = (this.highlight === k)
          ? "hsla(213,90%,64%,0.10)"
          : "hsla(220,16%,70%," + (on ? 0.05 : 0.02) + ")";
        ctx.fill();
        if (this.highlight === k) {
          ctx.strokeStyle = "hsla(213,90%,72%,0.55)";
          ctx.lineWidth = 1.4 * dpr; ctx.stroke();
        }
      }
    }

    for (var i = 0; i < this.pts.length; i++) {
      var p = this.pts[i];
      var x = (p.cx + (p.tx - p.cx) * t) * w * dpr;
      var y = (p.cy + (p.ty - p.cy) * t) * h * dpr;
      var active = this.highlight === -1 || this.highlight === p.cl;
      var target = this.highlight === p.cl && t > 0.5;
      var alpha = active ? (0.32 + 0.5 * t) : (0.10 * (1 - t) + 0.05);
      ctx.beginPath();
      ctx.arc(x, y, p.r * dpr, 0, Math.PI * 2);
      ctx.fillStyle = target ? "hsla(213,92%,72%," + alpha + ")" : "hsla(220,18%,82%," + alpha + ")";
      ctx.fill();
      if (p.rep && t > 0.45) {
        ctx.strokeStyle = target ? "hsla(213,92%,80%,0.9)" : "hsla(220,20%,86%,0.5)";
        ctx.lineWidth = 1 * dpr; ctx.stroke();
        ctx.fillStyle = target ? "hsla(213,95%,88%,0.95)" : "hsla(220,25%,88%,0.7)";
        ctx.font = (6.5 * dpr) + "px system-ui, sans-serif";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(p.rep, x, y + 0.5 * dpr);
      }
    }
  };

  function ease(x) { return x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2; }

  window.LUXitSegments = Field;
  window.LUXitSegments.CLUSTERS = CLUSTERS;
})();
