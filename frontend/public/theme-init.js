// Apply the theme before first paint (avoids a light→dark flash). Dark is the
// default, matching the landing page; a saved "light" choice is respected.
// A separate file rather than inline so a strict CSP (script-src 'self') allows it.
try {
  if (localStorage.getItem("acme.theme") !== "light") document.documentElement.classList.add("dark");
} catch (e) {
  document.documentElement.classList.add("dark");
}
