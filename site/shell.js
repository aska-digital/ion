/* Shared shell: side index, reveal on scroll, lattice backdrop, build stamp.
   Plain JavaScript, no dependencies. Every effect is decoration: the page
   reads the same with this file missing. */
(function () {
  "use strict";
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- Side index, built from the panels themselves ---- */
  var aside = document.querySelector("aside.index");
  var panels = Array.prototype.slice.call(document.querySelectorAll("main section[data-idx]"));
  if (aside && panels.length) {
    var title = aside.getAttribute("data-title") || "Index";
    var page = aside.getAttribute("data-page") || "";
    var html = '<div class="inner">' +
      '<button class="btn small index-toggle" type="button" aria-expanded="false">Index</button>' +
      '<div class="collapsible"><h2>' + title + "</h2><ol>";
    panels.forEach(function (s) {
      html += '<li><a href="#' + s.id + '"><span class="n">' + s.getAttribute("data-idx") +
        "</span><span>" + (s.getAttribute("data-short") || s.id) + "</span></a></li>";
    });
    html += '</ol><div class="sub">' +
      '<a href="index.html"' + (page === "abstract" ? ' aria-current="page"' : "") + ">Abstract</a>" +
      '<a href="guide.html"' + (page === "guide" ? ' aria-current="page"' : "") + ">Guide: run ion</a>" +
      '<a href="https://github.com/aska-digital/ion">Repository</a>' +
      '<a href="https://github.com/aska-digital/ion/blob/main/SPEC.md">Full spec (SPEC.md)</a>' +
      "</div>" +
      '<p class="key"><b class="ks">Cyan</b> the machine reads and proposes.<br>' +
      '<b class="kh">Amber</b> a maintainer decides.<br>Nothing structural moves without both.</p>' +
      "</div></div>";
    aside.innerHTML = html;

    var toggle = aside.querySelector(".index-toggle");
    toggle.addEventListener("click", function () {
      var open = aside.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });

    var links = {};
    aside.querySelectorAll("ol a").forEach(function (a) {
      links[a.getAttribute("href").slice(1)] = a;
    });
    if ("IntersectionObserver" in window) {
      var spy = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (!e.isIntersecting) return;
          Object.keys(links).forEach(function (k) { links[k].classList.remove("on"); });
          if (links[e.target.id]) links[e.target.id].classList.add("on");
        });
      }, { rootMargin: "-30% 0px -60% 0px" });
      panels.forEach(function (s) { spy.observe(s); });
    }
  }

  /* ---- Reveal on scroll ---- */
  var reveals = document.querySelectorAll(".reveal");
  if (reduced || !("IntersectionObserver" in window)) {
    reveals.forEach(function (el) { el.classList.add("in"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
          setTimeout(function () { e.target.classList.add("done"); }, 900);
        }
      });
    }, { threshold: 0.08 });
    reveals.forEach(function (el) { io.observe(el); });
  }

  /* ---- Lattice backdrop: a still grid of repos, a few ions drifting ---- */
  var cv = document.getElementById("lattice");
  if (cv && cv.getContext) {
    var ctx = cv.getContext("2d");
    var nodes = [], ions = [], W = 0, H = 0, gap = 132;
    function build() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = window.innerWidth; H = window.innerHeight;
      cv.width = W * dpr; cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      nodes = [];
      for (var y = gap / 2; y < H + gap; y += gap)
        for (var x = gap / 2; x < W + gap; x += gap)
          nodes.push({ x: x + ((x * 7 + y * 13) % 17) - 8, y: y + ((x * 3 + y * 11) % 13) - 6 });
      ions = [];
      for (var i = 0; i < 7; i++) ions.push(spawn());
    }
    function spawn() {
      var a = nodes[Math.floor(Math.random() * nodes.length)] || { x: 0, y: 0 };
      var near = nodes.filter(function (n) { return n !== a && Math.abs(n.x - a.x) + Math.abs(n.y - a.y) < gap * 1.6; });
      var b = near[Math.floor(Math.random() * near.length)] || a;
      return { a: a, b: b, t: Math.random(), sp: 0.0006 + Math.random() * 0.0011, amber: Math.random() < 0.3 };
    }
    function draw(now) {
      ctx.clearRect(0, 0, W, H);
      ctx.strokeStyle = "rgba(95,212,230,0.05)";
      ctx.lineWidth = 1;
      nodes.forEach(function (n) {
        nodes.forEach(function (m) {
          if (m === n) return;
          var d = Math.abs(m.x - n.x) + Math.abs(m.y - n.y);
          if (d > gap * 1.1 && d < gap * 1.3) { ctx.beginPath(); ctx.moveTo(n.x, n.y); ctx.lineTo(m.x, m.y); ctx.stroke(); }
        });
      });
      ctx.fillStyle = "rgba(148,178,205,0.20)";
      nodes.forEach(function (n) { ctx.beginPath(); ctx.arc(n.x, n.y, 1.6, 0, 6.283); ctx.fill(); });
      ions.forEach(function (ion, i) {
        if (!reduced) ion.t += ion.sp * 16;
        if (ion.t >= 1) { ions[i] = spawn(); return; }
        var x = ion.a.x + (ion.b.x - ion.a.x) * ion.t;
        var y = ion.a.y + (ion.b.y - ion.a.y) * ion.t;
        ctx.fillStyle = ion.amber ? "rgba(232,163,61,0.9)" : "rgba(95,212,230,0.9)";
        ctx.beginPath(); ctx.arc(x, y, 2.1, 0, 6.283); ctx.fill();
      });
      if (!reduced) requestAnimationFrame(draw);
    }
    var rt;
    window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { build(); if (reduced) draw(0); }, 200); });
    build();
    if (reduced) draw(0); else requestAnimationFrame(draw);
  }

  /* ---- Build stamp in the footer, when build.sh wrote one ---- */
  var stamp = document.getElementById("buildstamp");
  if (stamp && window.fetch) {
    fetch("data/build.json").then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j) return;
      stamp.textContent = "Built " + j.built + (j.commit ? " · " + j.commit.slice(0, 7) : "");
    }).catch(function () { /* local file preview: no stamp */ });
  }
})();
