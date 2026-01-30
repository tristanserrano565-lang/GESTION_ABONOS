document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-confirm]");
  if (!trigger) return;
  const message = trigger.getAttribute("data-confirm") || "¿Estás seguro?";
  if (!window.confirm(message)) {
    event.preventDefault();
    event.stopPropagation();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : null;
  if (token) {
    document.querySelectorAll('form[method="post"]').forEach((form) => {
      if (!form.querySelector('input[name="_csrf_token"]')) {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "_csrf_token";
        input.value = token;
        form.appendChild(input);
      }
    });
  }

  const waitEl = document.querySelector("[data-wait-seconds]");
  if (waitEl) {
    let remaining = parseInt(waitEl.dataset.waitSeconds, 10) || 0;
    const label = waitEl.querySelector("[data-wait-label]");
    const tick = () => {
      if (!label) return;
      if (remaining <= 0) {
        label.textContent = "";
        document.querySelectorAll("[data-remove-on-wait]").forEach((el) => {
          el.remove();
        });
        return;
      }
      label.textContent = `Vuelve a intentarlo en ${remaining}s`;
      remaining -= 1;
      setTimeout(tick, 1000);
    };
    tick();
  }

  document.querySelectorAll("[data-toggle-password]").forEach((toggle) => {
    const targetSelector = toggle.getAttribute("data-target") || "#password";
    const openIcon = toggle.getAttribute("data-open-icon") || "/static/img/open.png";
    const closeIcon = toggle.getAttribute("data-close-icon") || "/static/img/close.png";
    const updateIcon = (src) => {
      toggle.innerHTML = `<img src="${src}" alt="toggle" style="width:18px;height:18px;">`;
    };
    updateIcon(openIcon);
    toggle.addEventListener("click", () => {
      const field = document.querySelector(targetSelector);
      if (!field) return;
      const showing = field.type === "text";
      field.type = showing ? "password" : "text";
      updateIcon(showing ? openIcon : closeIcon);
    });
  });

  const mobileNav = document.getElementById("mobileNav");
  if (mobileNav && typeof bootstrap !== "undefined") {
    const mobileCollapse = new bootstrap.Collapse(mobileNav, {
      toggle: false,
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth >= 992 && mobileNav.classList.contains("show")) {
        mobileCollapse.hide();
      }
    });
  }
});

document.addEventListener("click", (event) => {
  const card = event.target.closest("[data-match-card]");
  if (!card) return;
  event.preventDefault();
  const url = card.dataset.targetUrl;
  if (url) {
    window.location.href = url;
  }
});
