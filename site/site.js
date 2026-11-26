(function () {
  var root = document.documentElement, saved = null;
  try { saved = localStorage.getItem("lang"); } catch (e) {}
  function set(lang) {
    root.lang = lang;
    document.querySelectorAll(".lang button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.lang === lang)); });
    var t = root.dataset[lang === "fr" ? "titleFr" : "titleEn"];
    if (t) document.title = t;
    document.dispatchEvent(new Event("langchange"));
  }
  set(saved || ((navigator.language || "").toLowerCase().indexOf("fr") === 0 ? "fr" : "en"));
  document.querySelectorAll(".lang button").forEach(function (b) {
    b.addEventListener("click", function () { set(b.dataset.lang); try { localStorage.setItem("lang", b.dataset.lang); } catch (e) {} });
  });
  var y = document.getElementById("year");
  if (y) y.textContent = new Date().getFullYear();
})();
