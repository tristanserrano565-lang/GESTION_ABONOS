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

  const pdfUploadForm = document.querySelector("[data-pdf-upload-form]");
  if (pdfUploadForm) {
    const pdfInput = pdfUploadForm.querySelector("[data-pdf-upload-input]");
    const pdfFeedback = pdfUploadForm.querySelector("[data-pdf-upload-feedback]");
    const suspiciousMarkers = [
      "/javascript",
      "/js",
      "/launch",
      "/openaction",
      "/embeddedfile",
      "/richmedia",
      "/xfa",
    ];

    const setPdfFeedback = (message) => {
      if (!pdfInput) return;
      pdfInput.setCustomValidity(message || "");
      pdfInput.classList.toggle("is-invalid", Boolean(message));
      if (pdfFeedback) {
        pdfFeedback.textContent = message || "";
      }
    };

    const readFileAsLatin1 = async (file) => {
      const buffer = await file.arrayBuffer();
      const decoder = new TextDecoder("latin1");
      return decoder.decode(buffer).toLowerCase();
    };

    const validatePdfFile = async () => {
      if (!pdfInput) return true;
      const file = pdfInput.files && pdfInput.files[0];
      if (!file) {
        setPdfFeedback("");
        return true;
      }

      const maxBytes = parseInt(pdfInput.dataset.maxBytes || "0", 10) || 0;
      const fileName = (file.name || "").toLowerCase();
      const fileType = (file.type || "").toLowerCase();

      if (!fileName.endsWith(".pdf")) {
        setPdfFeedback("El archivo debe tener extensión .pdf.");
        return false;
      }
      if (maxBytes && file.size > maxBytes) {
        const maxMb = Math.max(Math.floor(maxBytes / (1024 * 1024)), 1);
        setPdfFeedback(`El PDF supera el tamaño máximo permitido de ${maxMb} MB.`);
        return false;
      }
      if (
        fileType &&
        fileType !== "application/pdf" &&
        fileType !== "application/x-pdf"
      ) {
        setPdfFeedback("El fichero seleccionado no parece un PDF válido.");
        return false;
      }

      const headerText = await file.slice(0, 5).text();
      if (!headerText.startsWith("%PDF-")) {
        setPdfFeedback("La firma del fichero no corresponde a un PDF.");
        return false;
      }

      const tailStart = Math.max(0, file.size - 4096);
      const tailText = await file.slice(tailStart, file.size).text();
      if (!tailText.includes("%%EOF")) {
        setPdfFeedback("El PDF está incompleto o no tiene un cierre válido.");
        return false;
      }

      const fullText = await readFileAsLatin1(file);
      if (suspiciousMarkers.some((marker) => fullText.includes(marker))) {
        setPdfFeedback("El PDF contiene elementos activos o embebidos no permitidos.");
        return false;
      }

      setPdfFeedback("");
      return true;
    };

    if (pdfInput) {
      pdfInput.addEventListener("change", async () => {
        await validatePdfFile();
      });
    }

    pdfUploadForm.addEventListener("submit", async (event) => {
      if (pdfUploadForm.dataset.pdfValidated === "1") {
        return;
      }
      event.preventDefault();
      const isValid = await validatePdfFile();
      if (!isValid) {
        if (pdfInput) {
          pdfInput.reportValidity();
        }
        return;
      }
      pdfUploadForm.dataset.pdfValidated = "1";
      if (typeof pdfUploadForm.requestSubmit === "function") {
        pdfUploadForm.requestSubmit(event.submitter || undefined);
      } else {
        pdfUploadForm.submit();
      }
      window.requestAnimationFrame(() => {
        delete pdfUploadForm.dataset.pdfValidated;
      });
    });
  }

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

  const clientSearch = document.querySelector("[data-client-search]");
  if (clientSearch) {
    const rows = Array.from(document.querySelectorAll("[data-client-row]"));
    const normalize = (value) =>
      (value || "")
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "");

    const applyFilter = () => {
      const query = normalize(clientSearch.value.trim());
      rows.forEach((row) => {
        const name = normalize(row.getAttribute("data-client-name") || row.textContent);
        row.style.display = !query || name.includes(query) ? "" : "none";
      });
    };

    clientSearch.addEventListener("input", applyFilter);
    applyFilter();
  }

  const accordionSearch = document.querySelector("[data-client-search-accordion]");
  const accordionItems = Array.from(document.querySelectorAll("[data-client-accordion-item]"));
  const loadMoreBtn = document.querySelector("[data-clients-load-more]");
  const loadMoreWrap = document.getElementById("clientesLoadMoreWrap");
  if (accordionItems.length > 0) {
    const pageSize = 8;
    let visibleCount = pageSize;
    const normalize = (value) =>
      (value || "")
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "");

    const applyAccordionView = () => {
      const query = normalize(accordionSearch ? accordionSearch.value.trim() : "");
      const filtered = accordionItems.filter((item) => {
        const name = normalize(item.getAttribute("data-client-name") || item.textContent);
        return !query || name.includes(query);
      });

      accordionItems.forEach((item) => {
        item.style.display = "none";
      });
      filtered.slice(0, visibleCount).forEach((item) => {
        item.style.display = "";
      });

      if (loadMoreWrap) {
        loadMoreWrap.classList.toggle("d-none", filtered.length <= visibleCount);
      }
    };

    if (accordionSearch) {
      accordionSearch.addEventListener("input", () => {
        visibleCount = pageSize;
        applyAccordionView();
      });
    }

    if (loadMoreBtn) {
      loadMoreBtn.addEventListener("click", () => {
        visibleCount += pageSize;
        applyAccordionView();
      });
    }

    applyAccordionView();
  }

  const setupLoadMore = (itemSelector, buttonSelector, wrapId, pageSize = 8) => {
    const items = Array.from(document.querySelectorAll(itemSelector));
    const button = document.querySelector(buttonSelector);
    const wrap = document.getElementById(wrapId);
    if (!items.length || !button || !wrap) return;

    let visibleCount = pageSize;
    const applyView = () => {
      items.forEach((item, index) => {
        item.style.display = index < visibleCount ? "" : "none";
      });
      wrap.classList.toggle("d-none", items.length <= visibleCount);
    };

    button.addEventListener("click", () => {
      visibleCount += pageSize;
      applyView();
    });

    applyView();
  };

  setupLoadMore("[data-abonos-item]", "[data-abonos-load-more]", "abonosLoadMoreWrap");
  setupLoadMore("[data-parkings-item]", "[data-parkings-load-more]", "parkingsLoadMoreWrap");
  setupLoadMore("[data-partidos-item]", "[data-partidos-load-more]", "partidosLoadMoreWrap");

  const abonoField = document.querySelector("[data-abono-filter-field]");
  const abonoValue = document.querySelector("[data-abono-filter-value]");
  if (abonoField && abonoValue) {
    const abonoItems = Array.from(document.querySelectorAll("[data-abono-item]"));
    const applyAbonoFilter = () => {
      const field = abonoField.value;
      const value = (abonoValue.value || "").trim();
      abonoItems.forEach((item) => {
        if (!value) {
          item.classList.remove("d-none");
          return;
        }
        const current = (item.getAttribute(`data-${field}`) || "").trim();
        if (!current) {
          item.classList.add("d-none");
          return;
        }
        item.classList.toggle("d-none", !current.includes(value));
      });
    };
    abonoField.addEventListener("change", applyAbonoFilter);
    abonoValue.addEventListener("input", applyAbonoFilter);
    abonoValue.addEventListener("search", applyAbonoFilter);
    applyAbonoFilter();
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
