// Interacciones exclusivas del detalle; los datos de archivos se muestran como texto.
(() => {
  const page = document.querySelector('.match-workspace');
  if (!page) return;
  const loadedToggle = page.querySelector('[data-toggle-loaded]');
  loadedToggle?.addEventListener('click', () => {
    const panel = page.querySelector('#loadedEntries');
    panel.hidden = !panel.hidden;
    loadedToggle.setAttribute('aria-expanded', String(!panel.hidden));
  });

  page.querySelectorAll('[data-resource-panel]').forEach(panel => {
    const search = panel.querySelector('[data-resource-search]');
    const toggle = panel.querySelector('[data-resource-filter-toggle]');
    const filters = panel.querySelector('.md-filters');
    const status = panel.querySelector('[data-resource-status]');
    const field = panel.querySelector('[data-location-field]');
    const value = panel.querySelector('[data-location-value]');
    const normalize = text => text.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    function filterResources() {
      const term = normalize(search.value.trim());
      panel.querySelectorAll('[data-resource-row]').forEach(row => {
        const state = status.value;
        const matchesState = state === 'all' || row.dataset.state === state
          || (state === 'sent' && row.dataset.sent === '1')
          || (state === 'missing' && row.dataset.hasPdf === '0');
        const location = value?.value.trim() || '';
        row.hidden = !normalize(row.dataset.search).includes(term) || !matchesState
          || (location && !(row.dataset[field.value] || '').includes(location));
      });
      panel.querySelectorAll('.md-resource-rows').forEach(list => {
        const rows = Array.from(list.querySelectorAll('[data-resource-row]'));
        list.querySelector('[data-no-results]').hidden = !rows.length || rows.some(row => !row.hidden);
      });
    }
    toggle.addEventListener('click', () => {
      filters.hidden = !filters.hidden;
      toggle.setAttribute('aria-expanded', String(!filters.hidden));
    });
    search.addEventListener('input', filterResources);
    filters.addEventListener('input', filterResources);
    filters.addEventListener('change', filterResources);
  });

  const checks = page.querySelectorAll('.bulk-assign-check');
  function updateSelection() {
    const count = Array.from(checks).filter(check => check.checked).length;
    page.querySelector('[data-selected-count]').textContent = count ? `(${count})` : '';
  }
  checks.forEach(check => check.addEventListener('change', updateSelection));

  const upload = page.querySelector('[data-match-pdf-form]');
  if (!upload) return;
  const input = upload.querySelector('[name="pdf_files"]');
  const dropzone = upload.querySelector('[data-pdf-dropzone]');
  const saved = upload.querySelector('[data-saved-file-preview]');
  const title = upload.querySelector('[data-files-title]');
  const count = upload.querySelector('[data-files-count]');
  const originalCount = count.textContent;
  const status = upload.querySelector('[data-upload-status]');
  const originalStatus = status.textContent;
  upload.querySelector('[data-match-pdf-rows]').addEventListener('change', () => {
    const selects = Array.from(upload.querySelectorAll('[data-match-pdf-rows] select'));
    const missing = selects.filter(select => !select.value).length;
    status.textContent = missing
      ? `${missing} PDF(s) sin asignar: selecciona manualmente su abono o parking.`
      : 'Vinculaciones completas. Revísalas antes de guardar.';
  });
  function refreshFiles() {
    const files = Array.from(input.files || []);
    saved.hidden = files.length > 0;
    title.textContent = files.length ? 'Archivos seleccionados' : 'Archivos cargados';
    count.textContent = files.length ? `${files.length} PDF${files.length === 1 ? '' : 's'}` : originalCount;
    const selects = Array.from(upload.querySelectorAll('[data-match-pdf-rows] select'));
    const used = new Set();
    let matched = 0;
    selects.forEach((select, index) => {
      const candidates = Array.from(select.options).filter(option => option.value).map(option => ({
        key: option.value, ...option.dataset,
      }));
      const key = suggestMatchDocument(files[index].name, candidates, used);
      select.value = key || '';
      if (key) { used.add(key); matched += 1; }
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    status.textContent = files.length
      ? (matched === files.length ? 'Vinculación automática completada. Revísala antes de guardar.'
        : `${files.length - matched} PDF(s) sin asignar: selecciona manualmente su abono o parking.`)
      : originalStatus;
    upload.querySelectorAll('[data-file-size]').forEach((label, index) => {
      label.textContent = `${(files[index].size / (1024 * 1024)).toFixed(1)} MB`;
    });
  }
  input.addEventListener('change', refreshFiles);
  dropzone.addEventListener('click', event => {
    if (event.target.closest('label, input')) return;
    input.click();
  });
  ['dragenter', 'dragover'].forEach(name => dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.add('is-dragging');
  }));
  ['dragleave', 'drop'].forEach(name => dropzone.addEventListener(name, event => {
    event.preventDefault();
    dropzone.classList.remove('is-dragging');
  }));
  dropzone.addEventListener('drop', event => {
    if (!event.dataTransfer.files.length) return;
    input.files = event.dataTransfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
})();
