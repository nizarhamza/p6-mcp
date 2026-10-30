// Runs in <head> before the page paints (not deferred): applies the theme the visitor picked last time,
// so a dark page never flashes white, and marks the page as scripted (html.js) for the entrance effects.
(function () {
  var d = document.documentElement;
  d.classList.add("js");
  try {
    var t = localStorage.getItem("theme");
    if (t === "light" || t === "dark") {
      d.setAttribute("data-theme", t);
      var metas = document.querySelectorAll('meta[name="theme-color"]');
      for (var i = 0; i < metas.length; i++) metas[i].setAttribute("content", t === "dark" ? "#000000" : "#ffffff");
    }
  } catch (e) { /* storage blocked: follow the system setting */ }
})();
