(function () {
  var savedTheme = window.localStorage.getItem("control-horario-theme");
  if (savedTheme) {
    document.documentElement.setAttribute("data-theme", savedTheme);
  }
})();

