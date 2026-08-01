/* A tiny self-hosted subset for the dashboard's hx-get polling attributes. */
(function () {
  function seconds(trigger) {
    var match = /every\s+(\d+)s/.exec(trigger || "");
    return match ? Number(match[1]) * 1000 : null;
  }
  async function refresh(node) {
    var response = await fetch(node.getAttribute("hx-get"), { headers: { "HX-Request": "true" } });
    if (!response.ok) return;
    var html = await response.text();
    if (node.getAttribute("hx-swap") === "outerHTML") node.outerHTML = html;
    else node.innerHTML = html;
    bind();
  }
  function bind() {
    document.querySelectorAll("[hx-get][hx-trigger]").forEach(function (node) {
      if (node.dataset.hxBound) return;
      var delay = seconds(node.getAttribute("hx-trigger"));
      if (!delay) return;
      node.dataset.hxBound = "true";
      function tick() {
        if (!document.body.contains(node)) return;
        refresh(node).catch(function () {}).finally(function () { setTimeout(tick, delay); });
      }
      setTimeout(tick, delay);
    });
  }
  document.addEventListener("DOMContentLoaded", bind);
})();
