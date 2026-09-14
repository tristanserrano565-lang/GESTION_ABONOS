const getCollapseSelector = (toggle) => {
  const target = toggle.getAttribute("data-bs-target") || toggle.getAttribute("href");
  if (!target) return null;
  if (target.startsWith("#") || target.startsWith(".")) {
    return target;
  }
  if (target.includes("#")) {
    return `#${target.split("#")[1]}`;
  }
  return null;
};

const syncCollapseToggles = (selector, expanded) => {
  document.querySelectorAll('[data-bs-toggle="collapse"]').forEach((toggle) => {
    if (getCollapseSelector(toggle) !== selector) return;
    toggle.classList.toggle("collapsed", !expanded);
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
};

const setCollapseState = (collapseEl, expanded) => {
  collapseEl.classList.toggle("show", expanded);
  if (!collapseEl.id) return;
  syncCollapseToggles(`#${collapseEl.id}`, expanded);
};

const centerClientMatchHistory = (collapseEl) => {
  const scroller = collapseEl.querySelector("[data-client-match-scroll]");
  const anchor = scroller?.querySelector("[data-client-match-anchor]");
  if (!scroller || !anchor) return;
  const scrollerRect = scroller.getBoundingClientRect();
  const anchorRect = anchor.getBoundingClientRect();
  scroller.scrollTop +=
    anchorRect.top - scrollerRect.top - (scroller.clientHeight - anchorRect.height) / 2;
};

const closeAllDropdowns = () => {
  document.querySelectorAll(".dropdown-menu.show").forEach((menu) => {
    menu.classList.remove("show");
  });
  document.querySelectorAll('[data-bs-toggle="dropdown"][aria-expanded="true"]').forEach((toggle) => {
    toggle.setAttribute("aria-expanded", "false");
  });
};

const deleteDialog = document.querySelector("[data-delete-dialog]");
const deleteDialogTitle = deleteDialog?.querySelector("[data-delete-dialog-title]");
const deleteDialogMessage = deleteDialog?.querySelector("[data-delete-dialog-message]");
const deleteDialogCancel = deleteDialog?.querySelector("[data-delete-dialog-cancel]");
const deleteDialogSubmit = deleteDialog?.querySelector("[data-delete-dialog-submit]");
let pendingDeleteTrigger = null;
let deleteDialogPreviousFocus = null;

const closeDeleteDialog = () => {
  if (!deleteDialog || deleteDialog.hidden) return;
  deleteDialog.hidden = true;
  document.body.classList.remove("delete-confirm-open");
  pendingDeleteTrigger = null;
  deleteDialogPreviousFocus?.focus();
  deleteDialogPreviousFocus = null;
};

const openDeleteDialog = (trigger) => {
  if (!deleteDialog || !deleteDialogTitle || !deleteDialogMessage || !deleteDialogSubmit) {
    return false;
  }
  pendingDeleteTrigger = trigger;
  deleteDialogPreviousFocus = document.activeElement;
  deleteDialogTitle.textContent = trigger.dataset.deleteTitle || "Confirmar eliminación";
  deleteDialogMessage.textContent = trigger.dataset.deleteConfirm
    || "Al eliminar este elemento se perderán sus datos asociados. Esta acción no se puede deshacer.";
  deleteDialog.hidden = false;
  document.body.classList.add("delete-confirm-open");
  deleteDialogCancel?.focus();
  return true;
};

deleteDialogCancel?.addEventListener("click", closeDeleteDialog);
deleteDialog?.addEventListener("click", (event) => {
  if (event.target === deleteDialog) closeDeleteDialog();
});
deleteDialogSubmit?.addEventListener("click", () => {
  const trigger = pendingDeleteTrigger;
  const form = trigger?.form;
  if (!trigger || !form) return closeDeleteDialog();
  deleteDialog.hidden = true;
  document.body.classList.remove("delete-confirm-open");
  pendingDeleteTrigger = null;
  form.requestSubmit(trigger);
});

document.addEventListener("click", (event) => {
  const deleteTrigger = event.target.closest("[data-delete-confirm]");
  if (deleteTrigger && !deleteTrigger.disabled) {
    event.preventDefault();
    event.stopPropagation();
    const message = deleteTrigger.dataset.deleteConfirm || "¿Confirmas la eliminación?";
    if (!openDeleteDialog(deleteTrigger) && window.confirm(message)) {
      deleteTrigger.form?.requestSubmit(deleteTrigger);
    }
    return;
  }

  const confirmTrigger = event.target.closest("[data-confirm]");
  if (!confirmTrigger) return;
  const message = confirmTrigger.getAttribute("data-confirm") || "¿Estás seguro?";
  if (!window.confirm(message)) {
    event.preventDefault();
    event.stopPropagation();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('[data-bs-toggle="dropdown"]').forEach((toggle) => {
    if (!toggle.hasAttribute("aria-expanded")) {
      toggle.setAttribute("aria-expanded", "false");
    }
  });

  document.querySelectorAll('[data-bs-toggle="collapse"]').forEach((toggle) => {
    const selector = getCollapseSelector(toggle);
    if (!selector) return;
    const collapseEl = document.querySelector(selector);
    if (!collapseEl) return;
    const expanded = collapseEl.classList.contains("show");
    toggle.classList.toggle("collapsed", !expanded);
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  });

  const waitEl = document.querySelector("[data-wait-seconds]");
  if (waitEl) {
    let remaining = parseInt(waitEl.dataset.waitSeconds, 10) || 0;
    const label = waitEl.querySelector("[data-wait-label]");
    const blockedForm = document.querySelector("form[data-login-blocked='true']");
    const submitButton = blockedForm
      ? blockedForm.querySelector("[data-login-submit]")
      : null;
    const setBlockedState = (blocked) => {
      if (!blockedForm) return;
      blockedForm.dataset.loginBlocked = blocked ? "true" : "false";
      if (submitButton) {
        submitButton.disabled = blocked;
      }
    };

    if (blockedForm) {
      blockedForm.addEventListener("submit", (event) => {
        if (blockedForm.dataset.loginBlocked === "true") {
          event.preventDefault();
        }
      });
    }

    const tick = () => {
      if (!label) return;
      if (remaining <= 0) {
        setBlockedState(false);
        label.textContent = "";
        document.querySelectorAll("[data-remove-on-wait]").forEach((el) => {
          el.remove();
        });
        return;
      }
      setBlockedState(true);
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
    const usesInlineIcon = toggle.hasAttribute("data-inline-password-icon");

    const updateIcon = (src) => {
      toggle.replaceChildren();
      const icon = document.createElement("img");
      icon.src = src;
      icon.alt = "toggle";
      icon.width = 18;
      icon.height = 18;
      toggle.appendChild(icon);
    };

    if (!usesInlineIcon) updateIcon(openIcon);
    toggle.addEventListener("click", () => {
      const field = document.querySelector(targetSelector);
      if (!field) return;
      const showing = field.type === "text";
      field.type = showing ? "password" : "text";
      if (usesInlineIcon) {
        toggle.classList.toggle("is-password-visible", !showing);
        toggle.setAttribute("aria-label", showing ? "Mostrar contraseña" : "Ocultar contraseña");
      } else {
        updateIcon(showing ? openIcon : closeIcon);
      }
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
  if (mobileNav) {
    window.addEventListener("resize", () => {
      if (window.innerWidth >= 992 && mobileNav.classList.contains("show")) {
        setCollapseState(mobileNav, false);
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
        row.classList.toggle("d-none", Boolean(query) && !name.includes(query));
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
        item.classList.add("d-none");
      });
      filtered.slice(0, visibleCount).forEach((item) => {
        item.classList.remove("d-none");
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
        item.classList.toggle("d-none", index >= visibleCount);
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
  const dismissTrigger = event.target.closest('[data-bs-dismiss="alert"]');
  if (dismissTrigger) {
    const alert = dismissTrigger.closest(".alert");
    if (alert) {
      alert.remove();
    }
    return;
  }

  const dropdownToggle = event.target.closest('[data-bs-toggle="dropdown"]');
  if (dropdownToggle) {
    event.preventDefault();
    const dropdown = dropdownToggle.closest(".dropdown");
    const menu = dropdown ? dropdown.querySelector(".dropdown-menu") : null;
    if (!menu) return;
    const expanded = dropdownToggle.getAttribute("aria-expanded") === "true";
    closeAllDropdowns();
    if (!expanded) {
      menu.classList.add("show");
      dropdownToggle.setAttribute("aria-expanded", "true");
    }
    return;
  }

  if (!event.target.closest(".dropdown")) {
    closeAllDropdowns();
  }

  const collapseToggle = event.target.closest('[data-bs-toggle="collapse"]');
  if (!collapseToggle) return;
  event.preventDefault();
  const selector = getCollapseSelector(collapseToggle);
  if (!selector) return;
  const collapseEl = document.querySelector(selector);
  if (!collapseEl) return;

  const expanded = !collapseEl.classList.contains("show");
  const parentSelector = collapseEl.getAttribute("data-bs-parent");
  if (expanded && parentSelector) {
    const parent = document.querySelector(parentSelector);
    if (parent) {
      parent.querySelectorAll(".collapse.show").forEach((openCollapse) => {
        if (openCollapse !== collapseEl) {
          setCollapseState(openCollapse, false);
        }
      });
    }
  }

  setCollapseState(collapseEl, expanded);
  if (expanded) {
    window.requestAnimationFrame(() => centerClientMatchHistory(collapseEl));
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (deleteDialog && !deleteDialog.hidden) {
      closeDeleteDialog();
      return;
    }
    closeAllDropdowns();
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

// Validación del lote y asociación revisable antes de guardar.
(() => {
  const form = document.querySelector('[data-match-pdf-form]');
  if (!form) return;
  const input = form.querySelector('[name="pdf_files"]');
  const rows = form.querySelector('[data-match-pdf-rows]');
  const template = form.querySelector('[data-match-pdf-row]');
  const feedback = form.querySelector('[data-match-pdf-error]');
  const submit = form.querySelector('[data-match-pdf-submit]');

  function validateBatch() {
    const files = Array.from(input.files || []);
    const selected = Array.from(rows.querySelectorAll('select')).map(select => select.value);
    let error = '';
    if (!files.length) error = 'Selecciona al menos un PDF.';
    else if (files.length > Number(form.dataset.maxFiles)) error = 'El lote tiene demasiados archivos.';
    else if (files.some(file => !file.name.toLowerCase().endsWith('.pdf') || file.size === 0 || file.size > Number(form.dataset.maxFile))) error = 'Revisa la extensión y el tamaño de los PDFs.';
    else if (files.reduce((total, file) => total + file.size, 0) > Number(form.dataset.maxTotal)) error = 'El lote supera el tamaño total permitido.';
    else if (selected.length !== files.length || selected.some(value => !value)) error = 'Hay PDFs sin asignar. Selecciona manualmente su abono o parking.';
    else if (new Set(selected).size !== selected.length) error = 'No puedes seleccionar el mismo recurso para varios PDFs.';
    feedback.textContent = error;
    submit.disabled = Boolean(error);
    return !error;
  }

  input.addEventListener('change', () => {
    rows.replaceChildren();
    const files = Array.from(input.files || []);
    if (files.length <= Number(form.dataset.maxFiles)) {
      files.forEach((file, index) => {
        const row = template.content.cloneNode(true);
        const select = row.querySelector('select');
        const label = row.querySelector('[data-file-label]');
        select.id = `match-pdf-abono-${index}`;
        label.htmlFor = select.id;
        label.textContent = file.name;
        rows.append(row);
      });
    }
    validateBatch();
  });
  rows.addEventListener('change', validateBatch);
  form.addEventListener('submit', event => {
    if (!validateBatch()) event.preventDefault();
  });
})();
