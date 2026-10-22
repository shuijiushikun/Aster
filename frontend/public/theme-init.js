// 首次绘制前应用主题；默认浅色，保留用户主动选择的深色主题。
try {
  if (localStorage.getItem("acme.theme") === "dark") document.documentElement.classList.add("dark");
} catch (e) { /* 存储不可用时使用浅色。 */ }
