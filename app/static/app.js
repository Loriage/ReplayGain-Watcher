(function () {
  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-confirm]");
    if (link && !window.confirm(link.dataset.confirm)) event.preventDefault();
  });
})();
