(function () {
  var body = document.body;
  var toggleBtn = document.getElementById("nav-toggle-btn");
  var overlay = document.getElementById("mobile-nav-overlay");
  var siteNav = document.getElementById("site-nav");
  if (!body || !toggleBtn || !overlay || !siteNav) {
    return;
  }

  function isMobileNav() {
    return window.matchMedia("(max-width: 980px)").matches;
  }

  function closeMobileNav() {
    body.classList.remove("nav-open");
    toggleBtn.setAttribute("aria-expanded", "false");
  }

  function openMobileNav() {
    body.classList.add("nav-open");
    toggleBtn.setAttribute("aria-expanded", "true");
  }

  toggleBtn.addEventListener("click", function () {
    if (!isMobileNav()) {
      return;
    }
    if (body.classList.contains("nav-open")) {
      closeMobileNav();
      return;
    }
    openMobileNav();
  });

  overlay.addEventListener("click", closeMobileNav);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeMobileNav();
    }
  });

  siteNav.querySelectorAll("a, button").forEach(function (node) {
    node.addEventListener("click", function () {
      if (isMobileNav()) {
        closeMobileNav();
      }
    });
  });

  window.addEventListener("resize", function () {
    if (!isMobileNav()) {
      closeMobileNav();
    }
  });
})();

