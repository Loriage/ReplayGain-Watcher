(function () {
  function cookie(name) {
    var prefix = name + "=";
    var value = document.cookie.split(";").map(function (item) { return item.trim(); }).find(function (item) { return item.indexOf(prefix) === 0; });
    return value ? decodeURIComponent(value.slice(prefix.length)) : "";
  }

  function feedback(form, message, kind) {
    var target = form.querySelector(".action-feedback");
    if (!target) return;
    target.textContent = message;
    target.className = "action-feedback " + kind;
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-confirm]");
    if (link && !window.confirm(link.dataset.confirm)) event.preventDefault();
  });

  document.addEventListener("submit", function (event) {
    var form = event.target.closest("form[data-admin-action]");
    if (!form) return;
    event.preventDefault();

    var button = form.querySelector("button[type=submit]");
    if (!button || button.disabled) return;
    var originalText = button.textContent;
    var token = cookie("rgw_csrf");
    if (!token) {
      feedback(form, "Security token unavailable", "error");
      return;
    }

    button.disabled = true;
    button.textContent = "Working…";
    feedback(form, "", "");
    fetch(form.action, {
      method: "POST",
      headers: { "Accept": "application/json", "X-CSRF-Token": token }
    }).then(async function (response) {
      var payload = {};
      try { payload = await response.json(); } catch (ignore) {}
      if (!response.ok) {
        var detail = payload.detail || payload.message || "Action failed";
        throw new Error(typeof detail === "string" ? detail : (detail.message || "Action failed"));
      }
      feedback(form, form.dataset.success || "Action queued", "success");
      window.setTimeout(function () { window.location.reload(); }, 1200);
    }).catch(function (error) {
      feedback(form, error.message || "Action failed", "error");
      button.disabled = false;
      button.textContent = originalText;
    });
  });
})();
