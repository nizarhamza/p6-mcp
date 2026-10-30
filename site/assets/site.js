// MCP for Primavera P6: site behaviour. No dependencies. Every page works without it: content is in the
// HTML, this adds the theme switch, navigation drawer, search, tabs, diagrams and replays.
(function () {
  "use strict";
  var doc = document;
  var root = doc.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var darkMq = window.matchMedia("(prefers-color-scheme: dark)");

  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function isDark() { var t = root.getAttribute("data-theme"); return t ? t === "dark" : darkMq.matches; }
  function onChange(mq, fn) { if (mq.addEventListener) mq.addEventListener("change", fn); else if (mq.addListener) mq.addListener(fn); }

  // If site.js arrives after the CSS fallback already showed the content, keep it shown.
  if (window.performance && performance.now() > 2400) {
    $$("[data-reveal], .reveal-chips").forEach(function (el) {
      if (el.getBoundingClientRect().top < window.innerHeight) el.classList.add("is-in", "is-settled");
    });
  }
  root.classList.add("ready");

  // Messages for screen readers (copied, search results...).
  var srStatus = $("#sr-status");
  function announce(text) {
    if (!srStatus) return;
    srStatus.textContent = "";
    setTimeout(function () { srStatus.textContent = text; }, 60);
  }

  // Go to a link of this site; a link to the current page and hash still scrolls and updates the tool map.
  function go(href) {
    var u = new URL(href, location.href);
    if (u.pathname === location.pathname && u.hash) {
      var target = doc.getElementById(decodeURIComponent(u.hash.slice(1)));
      if (location.hash !== u.hash) history.pushState(null, "", u.hash);
      window.dispatchEvent(new Event("hashchange"));
      if (target) target.scrollIntoView({ block: "start" });
      return;
    }
    location.href = u.href;
  }

  // Theme switch: light or dark, remembered in this browser; the system setting until the first click.
  (function () {
    var btn = $("[data-theme-toggle]");
    if (!btn) return;
    function label() { btn.setAttribute("aria-label", isDark() ? "Switch to the light theme" : "Switch to the dark theme"); }
    label();
    onChange(darkMq, label);
    btn.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      if (!reduce.matches) {
        root.classList.add("theme-anim");
        setTimeout(function () { root.classList.remove("theme-anim"); }, 400);
      }
      root.setAttribute("data-theme", next);
      $$('meta[name="theme-color"]').forEach(function (m) { m.setAttribute("content", next === "dark" ? "#000000" : "#ffffff"); });
      try { localStorage.setItem("theme", next); } catch (e) { /* not remembered */ }
      label();
    });
  })();

  // Navigation drawer on narrow screens; the rest of the page is inert while it is open.
  (function () {
    var btn = $(".menu-btn");
    var side = $("#sidebar");
    var scrim = $("[data-scrim]");
    if (!btn || !side) return;
    var wide = window.matchMedia("(min-width: 64.0625rem)");
    function set(open, refocus) {
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.setAttribute("aria-label", open ? "Close the navigation" : "Open the navigation");
      side.classList.toggle("is-open", open);
      if (scrim) scrim.classList.toggle("is-open", open);
      $$("main, .toc, .site-foot").forEach(function (el) { el.inert = open; });
      if (open) {
        var a = $('a[aria-current="page"], a.is-active', side) || $("a", side);
        setTimeout(function () { if (a) a.focus(); }, 60);
      } else if (refocus) {
        btn.focus();
      }
    }
    btn.addEventListener("click", function () { set(btn.getAttribute("aria-expanded") !== "true"); });
    if (scrim) scrim.addEventListener("click", function () { set(false); });
    side.addEventListener("click", function (e) { if (e.target.closest("a") && !wide.matches) set(false); });
    doc.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && side.classList.contains("is-open")) set(false, true);
    });
    onChange(wide, function (m) { if (m.matches) set(false); });
  })();

  // Top bar shadow once the page scrolls.
  (function () {
    var bar = $(".topbar");
    if (!bar) return;
    var ticking = false;
    function update() { ticking = false; bar.classList.toggle("is-scrolled", window.scrollY > 4); }
    window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    update();
  })();

  // "On this page" and the sidebar follow the section being read (sections taken in page order).
  (function () {
    var links = $$('.toc a[href^="#"], .sidebar a[data-spy]');
    if (!links.length) return;
    var ids = [];
    links.forEach(function (a) {
      var id = a.getAttribute("href").slice(1);
      if (ids.indexOf(id) < 0 && doc.getElementById(id)) ids.push(id);
    });
    ids.sort(function (a, b) {
      return doc.getElementById(a).compareDocumentPosition(doc.getElementById(b)) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });
    var targets = ids.map(function (id) { return doc.getElementById(id); });
    var ticking = false;
    var last = null;
    function update() {
      ticking = false;
      var cur = ids[0];
      for (var i = 0; i < targets.length; i++) {
        if (targets[i].getBoundingClientRect().top <= 140) cur = ids[i];
      }
      if (window.innerHeight + window.scrollY >= root.scrollHeight - 4) cur = ids[ids.length - 1];
      if (cur === last) return;
      last = cur;
      links.forEach(function (a) {
        var on = a.getAttribute("href") === "#" + cur;
        a.classList.toggle("is-active", on);
        if (on) a.setAttribute("aria-current", "location");
        else if (a.getAttribute("aria-current") === "location") a.removeAttribute("aria-current");
      });
    }
    window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    window.addEventListener("resize", update);
    update();
  })();

  // Entrance effects (once) and live diagrams (each time they come into view: their pulses play for a few seconds).
  (function () {
    var once = $$("[data-reveal], .reveal-chips");
    var live = $$("[data-anim]");
    if (!("IntersectionObserver" in window)) {
      once.forEach(function (el) { el.classList.add("is-in", "is-settled"); });
      live.forEach(function (el) { el.classList.add("is-in", "is-live"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        en.target.classList.add("is-in");
        io.unobserve(en.target);
        if (en.target.classList.contains("reveal-chips")) {
          setTimeout(function () { en.target.classList.add("is-settled"); }, 1600);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0 });
    once.forEach(function (el) { io.observe(el); });

    function replay(el) {
      el.classList.remove("is-live");
      void el.offsetWidth;
      el.classList.add("is-live");
    }
    var lo = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("is-in"); replay(en.target); }
        else en.target.classList.remove("is-live");
      });
    }, { rootMargin: "-10% 0px -10% 0px", threshold: 0 });
    live.forEach(function (el) {
      lo.observe(el);
      el.addEventListener("mouseenter", function () { if (!reduce.matches) replay(el); });
    });
  })();

  // Numbers that count up when they come into view.
  (function () {
    var els = $$("[data-count]");
    if (!els.length || reduce.matches || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        var el = en.target;
        var to = +el.getAttribute("data-count");
        var t0 = null;
        function step(t) {
          if (t0 === null) t0 = t;
          var k = Math.min(1, (t - t0) / 1100);
          el.textContent = Math.round(to * (1 - Math.pow(1 - k, 3)));
          if (k < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
      });
    }, { threshold: 0.6 });
    els.forEach(function (el) { io.observe(el); });
  })();

  // A Pause / Play button whose label says what it does next.
  function pauseButton(btn, paused, what) {
    btn.classList.toggle("is-paused", paused);
    btn.setAttribute("aria-label", (paused ? "Play " : "Pause ") + what);
    $("span", btn).textContent = paused ? "Play" : "Pause";
  }

  // Step-by-step diagrams: a loop that highlights one step after the other while the diagram is in view.
  // Elements with data-at="n" light up at step n; data-step elements are numbered in order.
  $$("[data-seq]").forEach(function (box) {
    var fig = box.closest("figure") || box;
    var btn = $("[data-seq-toggle]", fig);
    var items = $$("[data-step], [data-at]", box);
    var max = 0;
    items.forEach(function (el, i) {
      el._n = el.hasAttribute("data-at") ? +el.getAttribute("data-at") : i;
      if (el._n > max) max = el._n;
    });
    var every = +box.getAttribute("data-interval") || 1600;
    var hold = +box.getAttribute("data-hold") || 3;
    var cur = -1;
    var timer = null;
    var seen = false;
    var paused = false;
    function render() {
      items.forEach(function (el) {
        el.classList.toggle("is-on", el._n <= cur);
        el.classList.toggle("is-now", el._n === cur);
      });
    }
    function tick() {
      cur = cur >= max + hold ? 0 : cur + 1;
      render();
    }
    function start() {
      if (timer || paused || !seen || doc.hidden) return;
      tick();
      timer = setInterval(tick, every);
    }
    function stop() { clearInterval(timer); timer = null; }
    if (reduce.matches) {
      cur = max + 1;
      render();
      if (btn) btn.closest(".d-controls").hidden = true;
      return;
    }
    box.classList.add("is-ready");
    render();
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          seen = en.isIntersecting;
          if (seen) start(); else stop();
        });
      }, { rootMargin: "-12% 0px -12% 0px", threshold: 0 }).observe(box);
    } else {
      seen = true;
      start();
    }
    doc.addEventListener("visibilitychange", function () { if (doc.hidden) stop(); else start(); });
    if (btn) {
      btn.addEventListener("click", function () {
        paused = !paused;
        pauseButton(btn, paused, "the animation");
        fig.classList.toggle("paused", paused);
        if (paused) {
          stop();
          cur = max + 1; // show the whole diagram while paused
          render();
        } else {
          cur = -1;
          start();
        }
      });
    }
  });

  // Tool map: filters and the details of the selected tool.
  (function () {
    var tm = $("[data-toolmap]");
    if (!tm) return;
    var filters = $$(".seg button", tm);
    var tools = $$(".tm-tool", tm);
    var cats = $$(".tm-cat", tm);
    var detail = $(".tm-detail", tm);
    var count = $(".tm-count b", tm);
    var chips = $(".reveal-chips", tm);
    function match(t, f) {
      if (f === "all") return true;
      if (f === "basic") return t.hasAttribute("data-basic");
      return t.getAttribute("data-access") === f;
    }
    function filter(f) {
      var n = 0;
      filters.forEach(function (x) { x.setAttribute("aria-pressed", x.getAttribute("data-filter") === f ? "true" : "false"); });
      tools.forEach(function (t) { var m = match(t, f); t.parentNode.hidden = !m; if (m) n++; });
      cats.forEach(function (c) { c.hidden = !$("li:not([hidden])", c); });
      count.textContent = n;
    }
    filters.forEach(function (b) {
      b.addEventListener("click", function () { filter(b.getAttribute("data-filter")); });
    });
    function show(t) {
      tools.forEach(function (x) { x.setAttribute("aria-pressed", x === t ? "true" : "false"); });
      detail.textContent = "";
      var hd = doc.createElement("p");
      hd.className = "hd";
      var code = doc.createElement("code");
      code.textContent = t.getAttribute("data-name");
      var acc = doc.createElement("span");
      acc.className = "acc";
      acc.textContent = t.getAttribute("data-acc") + (t.hasAttribute("data-basic") ? ", in the Basic profile" : "");
      hd.appendChild(code);
      hd.appendChild(acc);
      var p = doc.createElement("p");
      p.textContent = t.getAttribute("data-desc");
      detail.appendChild(hd);
      detail.appendChild(p);
    }
    tools.forEach(function (t) {
      t.addEventListener("click", function () {
        show(t);
        if (history.replaceState) history.replaceState(null, "", "#" + t.id);
      });
    });
    function fromHash() {
      if (location.hash.indexOf("#t-") !== 0) return;
      var t = doc.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (!t || !t.classList.contains("tm-tool")) return;
      if (t.parentNode.hidden) filter("all");
      chips.classList.add("is-in", "is-settled");
      show(t);
    }
    window.addEventListener("hashchange", fromHash);
    fromHash();
  })();

  // Tabs (Settings pages, dashboard sheets).
  $$("[data-tabs]").forEach(function (box) {
    var tabs = $$('[role="tab"]', box);
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        var p = doc.getElementById(t.getAttribute("aria-controls"));
        if (!p) return;
        p.hidden = !on;
        if (on) { p.classList.remove("is-shown"); void p.offsetWidth; p.classList.add("is-shown"); }
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () { select(t); });
      t.addEventListener("keydown", function (e) {
        var j = null;
        if (e.key === "ArrowRight") j = (i + 1) % tabs.length;
        else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") j = 0;
        else if (e.key === "End") j = tabs.length - 1;
        if (j !== null) { e.preventDefault(); select(tabs[j], true); }
      });
    });
  });

  // Images at full size.
  (function () {
    var dlg = $("#lightbox");
    if (!dlg || !dlg.showModal) return;
    var img = $("img", dlg);
    var cap = $("p", dlg);
    $$("[data-zoom]").forEach(function (b) {
      b.addEventListener("click", function () {
        var shown = $$("img", b).filter(function (i) { return i.offsetParent !== null; })[0] || $("img", b);
        img.removeAttribute("src");
        img.setAttribute("width", shown.getAttribute("width"));
        img.setAttribute("height", shown.getAttribute("height"));
        img.src = (isDark() && b.getAttribute("data-zoom-dark")) || b.getAttribute("data-zoom");
        img.alt = shown.alt;
        cap.textContent = shown.alt;
        dlg.showModal();
      });
    });
    $("[data-lb-close]", dlg).addEventListener("click", function () { dlg.close(); });
    dlg.addEventListener("click", function (e) { if (e.target === dlg || e.target === img) dlg.close(); });
  })();

  // The recorded run: a still image until the visitor presses play.
  $$("[data-player]").forEach(function (p) {
    var img = $("img", p);
    var btn = $(".player-btn", p);
    var label = $("span", btn);
    var still = img.getAttribute("src");
    var anim = img.getAttribute("data-anim-src");
    btn.addEventListener("click", function () {
      var playing = !p.classList.contains("is-playing");
      p.classList.toggle("is-playing", playing);
      img.src = playing ? anim : still;
      label.textContent = playing ? "Stop the recording" : "Play the recording";
    });
  });

  // Chat replay: messages and tool calls appear in turn when the chat comes into view. Messages not shown yet
  // stay in the page for screen readers (visually hidden), so nothing is missing for them.
  $$("[data-chat]").forEach(function (chat) {
    var body = $(".chat-body", chat);
    var btn = $("[data-chat-toggle]", chat);
    var steps = [];
    Array.prototype.forEach.call(body.children, function (el) {
      if (el.classList.contains("typing")) steps.push({ el: el, typing: true, wait: 1200 });
      else if (el.classList.contains("calls")) {
        steps.push({ el: el, wait: 250 });
        $$(".call", el).forEach(function (c) { steps.push({ el: c, wait: 700 }); });
      } else steps.push({ el: el, wait: el.classList.contains("msg-user") ? 900 : 800 });
    });
    if (reduce.matches) { btn.hidden = true; return; }
    chat.classList.add("is-ready");
    var i = 0;
    var timer = null;
    var state = "idle";
    function setBtn() {
      if (state === "done") {
        btn.classList.add("is-paused");
        btn.setAttribute("aria-label", "Replay the conversation");
        $("span", btn).textContent = "Replay";
      } else {
        pauseButton(btn, state !== "playing", "the replay");
      }
      chat.classList.toggle("paused", state === "paused");
    }
    function scrollTo(el) {
      var top = el.classList.contains("msg-report")
        ? el.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - 12
        : body.scrollHeight;
      body.scrollTo({ top: top, behavior: "smooth" });
    }
    function next() {
      timer = null;
      if (i > 0 && steps[i - 1].typing) steps[i - 1].el.classList.remove("is-on");
      if (i >= steps.length) { state = "done"; setBtn(); return; }
      var s = steps[i++];
      s.el.classList.add("is-on");
      scrollTo(s.el);
      timer = setTimeout(next, s.wait);
    }
    function reset() {
      steps.forEach(function (s) { s.el.classList.remove("is-on"); });
      i = 0;
      body.scrollTop = 0;
    }
    function play() { state = "playing"; setBtn(); if (!timer) next(); }
    function pause() { state = "paused"; clearTimeout(timer); timer = null; setBtn(); }
    setBtn();
    btn.addEventListener("click", function () {
      if (state === "playing") pause();
      else if (state === "done") { reset(); play(); }
      else play();
    });
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting && state === "idle") { io.disconnect(); play(); }
        });
      }, { rootMargin: "-15% 0px -15% 0px", threshold: 0 });
      io.observe(chat);
    } else play();
  });

  // Copy buttons on prompts (pre[data-copy]).
  (function () {
    if (!navigator.clipboard) return;
    var COPY = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>';
    var DONE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';
    $$("pre[data-copy]").forEach(function (pre) {
      var text = pre.innerText.trim();
      var what = pre.getAttribute("data-copy") || "text";
      var btn = doc.createElement("button");
      btn.type = "button";
      btn.className = "copy-btn";
      function show(svg, label) {
        btn.innerHTML = svg + "<span>" + label + '</span><span class="visually-hidden"> the ' + what + "</span>";
      }
      show(COPY, "Copy");
      btn.addEventListener("click", function () {
        navigator.clipboard.writeText(text).then(function () {
          show(DONE, "Copied");
          btn.classList.add("is-copied");
          announce("The " + what + " is copied.");
          setTimeout(function () { show(COPY, "Copy"); btn.classList.remove("is-copied"); }, 1600);
        }, function () {
          show(COPY, "Copy");
          announce("Copying failed: select the " + what + " and copy it.");
        });
      });
      pre.appendChild(btn);
    });
  })();

  // Search: pages, sections, tools and skills (search.json, loaded on first use).
  (function () {
    var dlg = $("#search-dlg");
    if (!dlg || !dlg.showModal) return;
    var input = $("#search-q");
    var list = $("#search-res");
    var msg = $("#search-msg");
    var status = $("#search-status");
    var url = dlg.getAttribute("data-index") || "/assets/search.json";
    var data = null;
    var loading = null;
    var failed = false;
    var sel = -1;
    var ORDER = ["Pages", "Sections", "Tools", "Skills"];
    if (/Mac|iPhone|iPad/.test(navigator.platform || "")) $$("[data-kbd]").forEach(function (k) { k.textContent = "⌘ K"; });
    function load() {
      if (data) return Promise.resolve(data);
      if (!loading) {
        failed = false;
        loading = fetch(url).then(function (r) {
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        }).then(function (d) { data = d; return d; }, function (e) { loading = null; failed = true; throw e; });
      }
      return loading;
    }
    function norm(s) { return (s || "").toLowerCase(); }
    function score(item, terms) {
      var t = norm(item.t);
      var t2 = t.replace(/_/g, " ");
      var d = norm(item.d);
      var s = 0;
      for (var i = 0; i < terms.length; i++) {
        var w = terms[i];
        if (t.indexOf(w) === 0 || t2.indexOf(w) === 0) s += 8;
        else if (t2.indexOf(" " + w) >= 0 || t2.indexOf("(" + w) >= 0) s += 6;
        else if (t.indexOf(w) >= 0 || t2.indexOf(w) >= 0) s += 4;
        else if (d.indexOf(w) >= 0) s += 1;
        else return 0;
      }
      return s + (item.g === "Pages" ? 2 : item.g === "Sections" ? 1 : 0);
    }
    function option(item, n) {
      var a = doc.createElement("a");
      a.href = item.u;
      a.id = "sr-" + n;
      a.tabIndex = -1;
      a.setAttribute("role", "option");
      a.setAttribute("aria-selected", "false");
      if (item.m) a.className = "mono";
      var t = doc.createElement("span");
      t.className = "t";
      t.textContent = item.t;
      a.appendChild(t);
      if (item.d) { var s = doc.createElement("small"); s.textContent = item.d; a.appendChild(s); }
      a.addEventListener("click", function (e) { e.preventDefault(); dlg.close(); go(a.href); });
      return a;
    }
    function say(visible, spoken) {
      msg.textContent = visible || "";
      msg.hidden = !visible;
      status.textContent = spoken;
    }
    function render() {
      list.textContent = "";
      sel = -1;
      input.setAttribute("aria-activedescendant", "");
      input.setAttribute("aria-expanded", "false");
      if (!data) {
        if (failed) say("Search is unavailable. Check your connection and try again.", "Search is unavailable.");
        else say("Loading...", "Loading the search index.");
        return;
      }
      var q = input.value.trim();
      var terms = norm(q).split(/\s+/).filter(Boolean);
      var res;
      var best = {};
      if (!terms.length) res = data.filter(function (d) { return d.g === "Pages" || (d.g === "Sections" && d.u.indexOf("/#") === 0); });
      else {
        res = data.map(function (d) { return { d: d, s: score(d, terms) }; })
          .filter(function (x) { return x.s > 0; })
          .sort(function (a, b) { return b.s - a.s; })
          .slice(0, 40)
          .map(function (x) { if (!(x.d.g in best)) best[x.d.g] = x.s; return x.d; });
      }
      if (!res.length) { say('No result for "' + q + '".', 'No result for "' + q + '".'); return; }
      say("", terms.length ? res.length + (res.length === 1 ? " result" : " results") : "");
      var groups = ORDER.slice().sort(function (a, b) { return (best[b] || 0) - (best[a] || 0) || ORDER.indexOf(a) - ORDER.indexOf(b); });
      var n = 0;
      groups.forEach(function (g, gi) {
        var items = res.filter(function (d) { return d.g === g; });
        if (!items.length) return;
        var grp = doc.createElement("li");
        grp.setAttribute("role", "group");
        grp.setAttribute("aria-labelledby", "sg-" + gi);
        var h = doc.createElement("p");
        h.className = "grp";
        h.id = "sg-" + gi;
        h.setAttribute("aria-hidden", "true");
        h.textContent = g;
        grp.appendChild(h);
        items.forEach(function (it) { grp.appendChild(option(it, n++)); });
        list.appendChild(grp);
      });
      input.setAttribute("aria-expanded", "true");
      move(0);
    }
    function move(k) {
      var opts = $$('[role="option"]', list);
      if (!opts.length) return;
      sel = (k + opts.length) % opts.length;
      opts.forEach(function (o, j) { o.setAttribute("aria-selected", j === sel ? "true" : "false"); });
      input.setAttribute("aria-activedescendant", opts[sel].id);
      opts[sel].scrollIntoView({ block: "nearest" });
    }
    function open() {
      if (dlg.open) return;
      input.value = "";
      dlg.showModal();
      render();
      load().then(render, render);
      input.focus();
    }
    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(sel + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(sel - 1); }
      else if (e.key === "Enter") {
        var a = $('[aria-selected="true"]', list);
        if (a) { e.preventDefault(); dlg.close(); go(a.href); }
      }
    });
    $$("[data-search-open]").forEach(function (b) { b.addEventListener("click", open); });
    $("[data-search-close]", dlg).addEventListener("click", function () { dlg.close(); });
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
    doc.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        if (dlg.open) dlg.close(); else open();
      }
    });
  })();
})();
