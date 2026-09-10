/** Sugiere una vinculación por nombre; nunca resuelve ubicaciones de abono ambiguas. */
function suggestMatchDocument(filename, resources, used = new Set()) {
  const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().trim().replace(/\s+/g, ' ');
  if (!/\.pdf$/i.test(filename)) return null;
  const parking = filename.match(/-P-\s*(.+?)-\s*\./i);
  if (parking) {
    const candidate = resources.find(resource => resource.kind === 'parking'
      && normalize(resource.name) === normalize(parking[1])
      && resource.loaded === '0' && !used.has(resource.key));
    return candidate?.key || null;
  }
  const codes = Array.from(filename.matchAll(/-(\d{9,12})(?=-)/g));
  if (codes.length !== 1) return null;
  const code = codes[0][1];
  const sector = Number(code.slice(0, -8));
  const fila = Number(code.slice(-8, -4));
  const asiento = Number(code.slice(-4));
  if (!sector || !fila || !asiento) return null;
  const candidates = resources.filter(resource => resource.kind === 'abono'
    && Number(resource.sector) === sector && Number(resource.fila) === fila
    && Number(resource.asiento) === asiento);
  if (candidates.length !== 1 || candidates[0].loaded !== '0' || used.has(candidates[0].key)) return null;
  return candidates[0].key;
}

if (typeof module !== 'undefined') module.exports = { suggestMatchDocument };
