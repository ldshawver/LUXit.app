/* ============================================================================
   Segmentation field — ~450 lightweight points (desktop) that reorganize from
   one crowd into 4 labelled clusters as scroll progress goes 0 -> 1.
   2D canvas. Render loop gated: runs only while `progress` is changing or the
   field is visible AND settling. No permanent loop.
   ========================================================================== */
(function () {
  "use strict";

  var CLUSTERS = [
    { key: "recent",   name: "Recent",     x: 0.28, y: 0.30 },
    { key: "inactive", name: "Inactive",   x: 0.72, y: 0.30 },
    { key: "optedin",  name: "Opted In",   x: 0.30, y: 0.72 },
    { key: "followup", name: "Follow Up",  x: 0.70, y: 0.72 }
  ];

  function Field(canvas, opts) {
    opts = opts || {};
    this.c = canvas;
    this.ctx = canvas.getContext("2d");
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.count = opts.count || 460;
    this.progress = 0;
    this.shown = 0;              // eased progress actually rendered
    this.highlight = -1;
    this.raf = 0;
    this.pts = [];
    this.build();
    this.resize();
    window.addEventListener("resize", this.resize.bind(this), { passive: true });
  }

  Field.prototype.build = function () {
    for (var i = 0; i < this.count; i++) {
      var cl = i % CLUSTERS.length;
      this.pts.push({
        // crowd position (near centre, mild spread)
        cx: 0.5 + (Math.random() - 0.5) * 0.32,
        cy: 0.5 + (Math.random() - 0.5) * 0.36,
        // cluster target (with local jitter)
        tx: CLUSTERS[cl].x + (Math.random() - 0.5) * 0.14,
        ty: CLUSTERS[cl].y + (Math.random() - 0.5) * 0.14,
        cl: cl,
        r: Math.random() < 0.12 ? 2.1 : 1.3
      });
    }
  };

  Field.prototype.resize = function () {
    var r = this.c.getBoundingClientRect();
    this.w = Math.max(1, r.width);
    this.h = Math.max(1, r.height);
    this.c.width = this.w * this.dpr;
    this.c.height = this.h * this.dpr;
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
    } else {
      this.shown = this.progress;
      this.paint();
      this.raf = 0;             // settled — stop
    }
  };

  Field.prototype.paint = function () {
    var ctx = this.ctx, dpr = this.dpr, w = this.w, h = this.h, t = ease(this.shown);
    ctx.clearRect(0, 0, w * dpr, h * dpr);
    for (var i = 0; i < this.pts.length; i++) {
      var p = this.pts[i];
      var x = (p.cx + (p.tx - p.cx) * t) * w * dpr;
      var y = (p.cy + (p.ty - p.cy) * t) * h * dpr;
      var active = this.highlight === -1 || this.highlight === p.cl;
      var alpha = active ? (0.35 + 0.5 * t) : (0.12 * (1 - t) + 0.06);
      ctx.beginPath();
      ctx.arc(x, y, p.r * dpr, 0, Math.PI * 2);
      if (this.highlight === p.cl && t > 0.5) {
        ctx.fillStyle = "hsla(213, 90%, 72%, " + alpha + ")";
      } else {
        ctx.fillStyle = "hsla(220, 20%, 80%, " + alpha + ")";
      }
      ctx.fill();
    }
    // faint ring around highlighted cluster
    if (this.highlight >= 0 && t > 0.6) {
      var cl = CLUSTERS[this.highlight];
      ctx.beginPath();
      ctx.arc(cl.x * w * dpr, cl.y * h * dpr, 62 * dpr * t, 0, Math.PI * 2);
      ctx.strokeStyle = "hsla(213, 90%, 70%, 0.5)";
      ctx.lineWidth = 1.4 * dpr;
      ctx.stroke();
    }
  };

  Field.prototype.labelPositions = function () {
    return CLUSTERS.map(function (cl) {
      return { name: cl.name, left: (cl.x * 100) + "%", top: (cl.y * 100) + "%" };
    });
  };

  function ease(x) { return x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2; }

  window.LUXitSegments = Field;
  window.LUXitSegments.CLUSTERS = CLUSTERS;
})();
