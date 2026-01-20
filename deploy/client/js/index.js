document.addEventListener("DOMContentLoaded", () => {
  const enterBtn = document.querySelector(".btn-enter");

  if (enterBtn) {
    enterBtn.addEventListener("click", (e) => {
      e.preventDefault();

      document.body.style.transition = "opacity 0.5s ease";
      document.body.style.opacity = "0";

      setTimeout(() => {
        window.location.href = "login.html";
      }, 500);
    });
  }
});
