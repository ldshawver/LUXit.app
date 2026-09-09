/* ============================================================================
   LUXit immersive experience — orchestrator.
   Progressive enhancement: if GSAP is missing, reduced-motion is set, Save-Data
   is on, or ?immersive=0 — this bails and the semantic page stands on its own.
   Content-first: the DOM headline/CTAs are already painted before this runs.
   Render loops are visibility-gated; no permanent decorative animation.
   ========================================================================== */
(function () {
  "use strict";

  var root = document.querySelector(".imx");
  if (!root) return;

  var params = new URLSearchParams(location.search);
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var saveData = (navigator.connection && navigator.connection.saveData) === true;
  var optOut = params.get("immersive") === "0";
  var hasGSAP = !!(window.gsap && window.ScrollTrigger);

  // Always wire the lightweight bits (Zappy static frame, product hover) even
  // in reduced motion; only the scroll-scrubbed camera work needs GSAP.
  initZappy();
  initStorefrontHover();

  if (optOut || saveData) { root.setAttribute("data-motion", "off"); return; }
  if (reduced || !hasGSAP) {
    root.setAttribute("data-motion", "off");
    // still show cluster labels + a settled segmentation frame
    settleSegments();
    revealZappyStatic();
    return;
  }

  root.setAttribute("data-enhanced", "1");
  gsap.registerPlugin(ScrollTrigger);

  var zappy = window.__imxZappy;
  var zLine = document.querySelector(".imx-zappy-line");
  var zWrap = document.querySelector(".imx-zappy");

  function say(text, state, hue) {
    if (zappy && state) zappy.setState(state, hue);
    if (!zLine) return;
    zLine.textContent = text;
    zLine.classList.add("is-on");
    clearTimeout(say._t);
    say._t = setTimeout(function () { zLine.classList.remove("is-on"); }, 4200);
  }
  function zappyOn() { if (zWrap) zWrap.classList.add("is-on"); }

  /* ---- pointer parallax (desktop, pointer:fine only) -------------------- */
  var fine = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  if (fine) {
    var store = root.querySelector(".imx-store");
    var px = 0, py = 0, tx = 0, ty = 0, parRaf = 0, parActive = false;
    window.addEventListener("pointermove", function (e) {
      tx = (e.clientX / window.innerWidth - 0.5);
      ty = (e.clientY / window.innerHeight - 0.5);
      if (!parRaf && parActive) parRaf = requestAnimationFrame(par);
    }, { passive: true });
    function par() {
      px += (tx - px) * 0.06; py += (ty - py) * 0.06;
      if (store) store.style.transform =
        "rotateX(" + (6 - py * 6) + "deg) rotateY(" + (-16 - px * 8) + "deg)";
      if (Math.abs(tx - px) + Math.abs(ty - py) > 0.001) parRaf = requestAnimationFrame(par);
      else parRaf = 0;
    }
    // only run parallax while scene 1 is on screen
    io(root.querySelector('[data-scene="storefront"]'), function (on) {
      parActive = on;
      if (on && !parRaf) parRaf = requestAnimationFrame(par);
    });
  }

  /* ---- SCENE 1 — storefront reveal ------------------------------------- */
  var s1 = root.querySelector('[data-scene="storefront"]');
  gsap.set(s1.querySelectorAll(".imx-product"), { z: -140, opacity: 0 });
  gsap.timeline({
    scrollTrigger: { trigger: s1, start: "top 78%", once: true }
  })
    .to(s1.querySelectorAll(".imx-product"),
        { z: function (i) { return [0, 30, 12, 40, 8, 24][i] || 0; }, opacity: 1,
          duration: 0.9, stagger: 0.08, ease: "power3.out" })
    .add(function () { zappyOn(); say("Welcome. Let me show you around.", "greet", 30); }, "-=0.4");

  /* scripted mini-exchange plays once when hero is centred */
  var exch = s1.querySelector(".imx-exchange");
  if (exch) {
    ScrollTrigger.create({
      trigger: s1, start: "top 30%", once: true,
      onEnter: function () { playExchange(exch); }
    });
  }

  /* ---- SCENE 2 — customer / one history ------------------------------- */
  var s2 = root.querySelector('[data-scene="customer"]');
  var panels = s2.querySelectorAll(".imx-panel");
  gsap.timeline({
    scrollTrigger: { trigger: s2, start: "top 68%", end: "bottom 60%", scrub: 0.6 }
  })
    .fromTo(panels,
      { opacity: 0, scale: 0.7 },
      { opacity: 1, scale: 1, stagger: 0.12, ease: "power2.out" });
  io(s2, function (on) {
    if (on) say("Every conversation starts with knowing who you're talking to.", "connect", 205);
  });
  drawThreads(s2);

  /* ---- SCENE 3 — segmentation --------------------------------------- */
  var s3 = root.querySelector('[data-scene="segmentation"]');
  var field = mountSegments(s3);
  var labels = s3.querySelectorAll(".imx-clabel");
  ScrollTrigger.create({
    trigger: s3, start: "top 70%", end: "bottom 70%", scrub: 0.5,
    onUpdate: function (self) { if (field) field.setProgress(self.progress); },
    onEnter: function () { say("I've narrowed this down.", "recommend", 213); },
  });
  ScrollTrigger.create({
    trigger: s3, start: "center 60%", once: true,
    onEnter: function () {
      if (field) field.setHighlight(3); // "Follow Up"
      labels.forEach(function (l, i) { l.setAttribute("data-active", i === 3 ? "1" : "1"); });
      labels[3] && labels[3].setAttribute("data-active", "1");
    }
  });

  /* ---- ENTREPRENEUR SCALE beat ------------------------------------- */
  var scale = root.querySelector(".imx-scale-count");
  if (scale) {
    var seq = ["1", "10", "100", "1,000"];
    ScrollTrigger.create({
      trigger: scale, start: "top 80%", once: true,
      onEnter: function () {
        var i = 0;
        scale.textContent = seq[0];
        var iv = setInterval(function () {
          i++; if (i >= seq.length) { clearInterval(iv); return; }
          scale.textContent = seq[i];
        }, 520);
      }
    });
  }

  /* ---- SCENE 4 — security x-ray ----------------------------------- */
  var s4 = root.querySelector('[data-scene="security"]');
  var layers = s4.querySelectorAll(".imx-layer");
  var stack = s4.querySelector(".imx-stack");
  gsap.timeline({
    scrollTrigger: { trigger: s4, start: "top 75%", end: "bottom bottom", scrub: 0.6 }
  })
    .fromTo(stack, { rotateX: 62 }, { rotateX: 48, ease: "none" }, 0)
    .fromTo(layers,
      { opacity: 0, yPercent: 12 },
      { opacity: 1, yPercent: 0, stagger: 0.14, ease: "power1.out" }, 0);
  io(s4, function (on) {
    if (on) say("I can assist. The system still decides.", "secure", 218);
  });

  ScrollTrigger.refresh();

  /* ===================== helpers ===================== */

  function io(el, cb) {
    if (!el || !("IntersectionObserver" in window)) { cb(true); return; }
    new IntersectionObserver(function (ents) {
      cb(ents[0].isIntersecting);
    }, { threshold: 0.25 }).observe(el);
  }

  function initZappy() {
    var cv = root.querySelector(".imx-zappy canvas");
    if (cv && window.LUXitZappy) window.__imxZappy = new window.LUXitZappy(cv);
  }
  function revealZappyStatic() {
    var w = root.querySelector(".imx-zappy");
    if (w) w.classList.add("is-on");
  }

  function initStorefrontHover() {
    var prods = root.querySelectorAll(".imx-product");
    prods.forEach(function (p) {
      p.addEventListener("pointerenter", function () {
        prods.forEach(function (q) { q.setAttribute("data-emph", q === p ? "1" : "0"); });
        if (window.__imxZappy) window.__imxZappy.setState("listen", 205);
        var line = document.querySelector(".imx-zappy-line");
        if (line && root.getAttribute("data-motion") !== "off") {
          line.textContent = "Looking for something specific?";
          line.classList.add("is-on");
          clearTimeout(initStorefrontHover._t);
          initStorefrontHover._t = setTimeout(function () { line.classList.remove("is-on"); }, 2600);
        }
      });
      p.addEventListener("focus", function () { p.dispatchEvent(new Event("pointerenter")); });
    });
  }

  function playExchange(el) {
    var bubbles = el.querySelectorAll(".imx-bubble");
    el.style.opacity = 1;
    var delay = 0;
    bubbles.forEach(function (b, i) {
      setTimeout(function () {
        b.style.transition = "opacity .5s ease, transform .5s ease";
        b.style.opacity = 1; b.style.transform = "none";
        if (i === 1 && window.__imxZappy) window.__imxZappy.setState("recommend", 30);
      }, delay);
      delay += 900;
    });
    // after the exchange, nudge products + emphasise one
    setTimeout(function () {
      var prods = root.querySelectorAll(".imx-product");
      if (!window.gsap) { prods[0] && prods[0].setAttribute("data-emph", "1"); return; }
      gsap.to(prods, { z: function (i) { return [70, 10, 40, 6, 30, 4][i] || 0; },
        duration: 0.8, stagger: 0.05, ease: "power2.out" });
      prods.forEach(function (q, i) { q.setAttribute("data-emph", i === 0 ? "1" : "0"); });
    }, delay + 200);
  }

  function drawThreads(scene) {
    var svg = scene.querySelector(".imx-thread");
    var person = scene.querySelector(".imx-person");
    var panels = scene.querySelectorAll(".imx-panel");
    if (!svg || !person) return;
    function redraw() {
      var sb = scene.getBoundingClientRect();
      var pb = person.getBoundingClientRect();
      var cx = pb.left - sb.left + pb.width / 2;
      var cy = pb.top - sb.top + pb.height / 2;
      svg.innerHTML = "";
      panels.forEach(function (pl) {
        var b = pl.getBoundingClientRect();
        var x = b.left - sb.left + b.width / 2;
        var y = b.top - sb.top + b.height / 2;
        var ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
        ln.setAttribute("x1", cx); ln.setAttribute("y1", cy);
        ln.setAttribute("x2", x); ln.setAttribute("y2", y);
        svg.appendChild(ln);
      });
    }
    io(scene, function (on) { if (on) redraw(); });
    window.addEventListener("resize", debounce(redraw, 200), { passive: true });
    setTimeout(redraw, 400);
  }

  function mountSegments(scene) {
    var holder = scene.querySelector(".imx-audience");
    if (!holder || !window.LUXitSegments) return null;
    var cv = document.createElement("canvas");
    holder.appendChild(cv);
    var isMobile = window.matchMedia("(max-width: 860px)").matches;
    var f = new window.LUXitSegments(cv, { count: isMobile ? 150 : 460 });
    io(scene, function (on) { if (on) f.setProgress(f.progress); }); // wake to settle
    return f;
  }
  function settleSegments() {
    var scene = root.querySelector('[data-scene="segmentation"]');
    var f = mountSegments(scene);
    if (f) { f.setHighlight(3); f.setProgress(1); }
    var labels = scene ? scene.querySelectorAll(".imx-clabel") : [];
    labels.forEach(function (l) { l.setAttribute("data-active", "1"); });
  }

  function debounce(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }
})();
