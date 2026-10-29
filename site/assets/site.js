// Copy buttons on prompts and snippets (pre[data-copy]). The page works without this script.
(function () {
  if (!navigator.clipboard) return;
  document.querySelectorAll("pre[data-copy]").forEach(function (pre) {
    var text = pre.innerText.trim(); // read before the button is added inside the block
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy " + (pre.dataset.copy || "text"));
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = "Copy"; }, 1600);
      }, function () {
        btn.textContent = "Select and copy";
      });
    });
    pre.appendChild(btn);
  });
})();
