/* Password visibility only; credentials are submitted by the native form. */
"use strict";
(() => {
  const password = document.getElementById("login-password");
  const eye = document.querySelector(".login-eye");
  if (!password || !eye) return;
  eye.hidden = false;
  eye.addEventListener("click", () => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    eye.setAttribute("aria-pressed", String(show));
    eye.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  });
})();
