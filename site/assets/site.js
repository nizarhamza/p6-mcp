// A page opened from a link starts at its top. The preview viewer can carry the previous page's
// scroll position over to the next page; this puts the new page back at the top, unless the link
// pointed at a section (#...), the visitor came back with the Back button or reloaded the page
// (the browser then restores where they were reading).
(function () {
  if (location.hash) return;
  var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
  if (nav && (nav.type === "back_forward" || nav.type === "reload")) return;
  var touched = false;
  ["wheel", "touchstart", "keydown", "mousedown"].forEach(function (t) {
    window.addEventListener(t, function () { touched = true; }, { once: true, passive: true });
  });
  function top() { if (!touched && window.scrollY > 0) window.scrollTo(0, 0); }
  top();
  window.addEventListener("load", top);
  [120, 400, 900].forEach(function (ms) { setTimeout(top, ms); });
})();

// Copy buttons on prompts and snippets (pre[data-copy]). The page works without this script.
(function () {
  if (!navigator.clipboard) return;
  var COPY = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>';
  var DONE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';
  document.querySelectorAll("pre[data-copy]").forEach(function (pre) {
    var text = pre.innerText.trim(); // read before the button is added inside the block
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.setAttribute("aria-label", "Copy " + (pre.dataset.copy || "text"));
    function show(icon, label) { btn.innerHTML = icon + "<span>" + label + "</span>"; }
    show(COPY, "Copy");
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(text).then(function () {
        show(DONE, "Copied");
        btn.classList.add("is-copied");
        setTimeout(function () { show(COPY, "Copy"); btn.classList.remove("is-copied"); }, 1600);
      }, function () {
        show(COPY, "Select and copy");
      });
    });
    pre.appendChild(btn);
  });
})();

// Menu button on phones: opens the links as a panel under the top bar.
(function () {
  var btn = document.querySelector(".menu-toggle");
  var nav = document.getElementById("site-nav");
  if (!btn || !nav) return;
  function set(open) {
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.querySelector(".menu-label").textContent = open ? "Close" : "Menu";
    nav.classList.toggle("is-open", open);
  }
  btn.addEventListener("click", function () { set(btn.getAttribute("aria-expanded") !== "true"); });
  nav.addEventListener("click", function (e) { if (e.target.closest("a")) set(false); });
  document.addEventListener("click", function (e) {
    if (nav.classList.contains("is-open") && !nav.contains(e.target) && !btn.contains(e.target)) set(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && nav.classList.contains("is-open")) { set(false); btn.focus(); }
  });
  var wide = window.matchMedia("(min-width: 48rem)");
  if (wide.addEventListener) wide.addEventListener("change", function (m) { if (m.matches) set(false); });
})();

// Top bar: hidden while scrolling down, shown again as soon as you scroll up.
(function () {
  var bar = document.querySelector(".masthead");
  if (!bar) return;
  var over = bar.classList.contains("masthead-over"); // home: the bar sits on the photograph
  var nav = document.getElementById("site-nav");
  var lastY = window.scrollY;
  var ticking = false;
  function menuOpen() { return !!(nav && nav.classList.contains("is-open")); }
  function update() {
    ticking = false;
    var y = Math.max(0, window.scrollY);
    var h = bar.offsetHeight;
    var dy = y - lastY;
    if (Math.abs(dy) < 6 && y > 0) return; // ignore tiny movements
    lastY = y;
    bar.classList.toggle("is-scrolled", y > 0);
    if (over) {
      if (y <= 0) { bar.classList.remove("is-pinned", "is-hidden"); return; }
      if (!bar.classList.contains("is-pinned")) {
        if (y <= h) return; // still on the photograph: the bar scrolls with the page
        // pin it out of sight, without a slide, so the switch is invisible
        bar.classList.add("no-anim", "is-pinned", "is-hidden");
        void bar.offsetHeight;
        bar.classList.remove("no-anim");
      }
    }
    if (y <= h) { bar.classList.remove("is-hidden"); return; }
    if (dy > 0 && !menuOpen()) bar.classList.add("is-hidden");
    else if (dy < 0) bar.classList.remove("is-hidden");
  }
  window.addEventListener("scroll", function () {
    if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
  }, { passive: true });
  // keyboard users: a focused link in the bar is always visible
  bar.addEventListener("focusin", function () { bar.classList.remove("is-hidden"); });
  update();
})();

// Scroll story, with GSAP and ScrollTrigger served from /assets/vendor/gsap/.
// Every page is complete without it: this only adds motion and position cues.
(function () {
  var gsap = window.gsap;
  var ScrollTrigger = window.ScrollTrigger;
  if (!gsap || !ScrollTrigger) return;
  gsap.registerPlugin(ScrollTrigger);

  var MOTION = "(prefers-reduced-motion: no-preference)";
  var WIDE = "(min-width: 52rem)";
  var mm = gsap.matchMedia();

  // Day 0 of the demo calendar is 1 Oct 2026; the data date is day 25 (26 Oct 2026).
  var ORIGIN = Date.UTC(2026, 9, 1);
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function dayToDate(day) {
    var d = new Date(ORIGIN + Math.round(day) * 86400000);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear();
  }

  /* ------------------------------------------------------------ home */

  // The hero photograph drifts slower than the page.
  var heroPhoto = document.querySelector(".hero-photo");
  if (heroPhoto) {
    mm.add(MOTION, function () {
      gsap.to(heroPhoto, { yPercent: 8, ease: "none", scrollTrigger: { trigger: ".hero", start: "top top", end: "bottom top", scrub: true } });
    });
  }

  // The driving path: pin the stage and let scrolling move the data date through the calendar.
  var path = document.querySelector(".path");
  if (path) {
    mm.add({ motion: MOTION, wide: WIDE }, function (ctx) {
      if (!ctx.conditions.motion || !ctx.conditions.wide) return;
      var stage = path.querySelector(".path-stage");
      var frames = Array.prototype.slice.call(path.querySelectorAll(".path-frames > li"));
      var notes = Array.prototype.slice.call(path.querySelectorAll(".path-notes > li"));
      var label = path.querySelector(".playhead-date");
      var starts = frames.map(function (li) { return Number(li.dataset.start); });
      var state = { day: starts[0] };

      path.classList.add("is-scrolly");
      function render() {
        stage.style.setProperty("--day", state.day.toFixed(2));
        if (label) label.textContent = dayToDate(state.day);
        frames.forEach(function (li, i) { li.classList.toggle("is-shown", state.day >= starts[i] - 0.5); });
        // the note beside the photograph explains the question it shows
        var current = 0;
        starts.forEach(function (st, i) { if (state.day >= st - 0.5) current = i; });
        notes.forEach(function (li, i) { li.classList.toggle("is-current", i === current); });
      }
      render();

      // One even stretch of scrolling per photograph, so the long manufacturing run
      // passes quickly and the short tests still get their moment.
      var tl = gsap.timeline({
        defaults: { ease: "none" },
        onUpdate: render,
        scrollTrigger: { trigger: stage, start: "top top", end: function () { return "+=" + Math.round(window.innerHeight * 2.6); }, pin: true, scrub: 0.6, invalidateOnRefresh: true }
      });
      tl.to(state, { day: starts[0], duration: 0.25 });
      for (var i = 1; i < starts.length; i++) tl.to(state, { day: starts[i], duration: 1 });
      tl.to(state, { day: starts[starts.length - 1], duration: 0.35 });

      return function () {
        path.classList.remove("is-scrolly");
        stage.style.removeProperty("--day");
        frames.forEach(function (li) { li.classList.remove("is-shown"); });
        notes.forEach(function (li, i) { li.classList.toggle("is-current", i === 0); });
        if (label) label.textContent = dayToDate(starts[0]);
      };
    });
  }

  // Questions: mark the one being read.
  var questions = document.querySelectorAll(".qa > div");
  if (questions.length) {
    mm.add(MOTION, function () {
      questions.forEach(function (row) {
        ScrollTrigger.create({ trigger: row, start: "top 55%", end: "bottom 55%", toggleClass: "is-active" });
      });
    });
  }

  // Write path: the line runs through the gates as they are read; each gate turns amber when passed.
  var gates = document.querySelector(".write-path");
  if (gates) {
    mm.add(MOTION, function () {
      gsap.fromTo(gates, { "--p": 0 }, { "--p": 1, ease: "none", scrollTrigger: { trigger: gates, start: "top 62%", end: "bottom 62%", scrub: 0.4 } });
      gates.querySelectorAll("li").forEach(function (li) {
        ScrollTrigger.create({ trigger: li, start: "top 62%", end: "max", toggleClass: "is-passed" });
      });
      return function () { gates.style.removeProperty("--p"); };
    });
  }

  /* ------------------------------------------------------------ guides */

  var article = document.querySelector(".article");
  if (article) {
    // Reading progress, drawn like a timescale under the top edge of the window.
    var scale = document.createElement("div");
    scale.className = "read-scale";
    scale.setAttribute("aria-hidden", "true");
    scale.appendChild(document.createElement("span"));
    document.body.appendChild(scale);
    gsap.to(scale.firstChild, { scaleX: 1, ease: "none", scrollTrigger: { trigger: article, start: "top 70%", end: "bottom bottom", scrub: true } });
    ScrollTrigger.create({ trigger: ".doc-head", start: "bottom top", end: "max", toggleClass: { targets: scale, className: "is-on" } });

    // The current section is marked in "On this page", and its diamond turns amber once reached.
    var heads = Array.prototype.slice.call(article.querySelectorAll("h2[id]"));
    var links = {};
    document.querySelectorAll('.doc-nav a[href^="#"]').forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
    function setCurrent(id) {
      Object.keys(links).forEach(function (k) {
        var on = k === id;
        links[k].classList.toggle("is-current", on);
        if (on) links[k].setAttribute("aria-current", "location"); else links[k].removeAttribute("aria-current");
      });
    }
    heads.forEach(function (h, i) {
      var next = heads[i + 1];
      ScrollTrigger.create({
        trigger: h, start: "top 35%",
        endTrigger: next || article, end: next ? "top 35%" : "bottom 35%",
        onToggle: function (self) { if (self.isActive) setCurrent(h.id); }
      });
      ScrollTrigger.create({ trigger: h, start: "top 80%", end: "max", toggleClass: "is-passed" });
    });
  }
})();
