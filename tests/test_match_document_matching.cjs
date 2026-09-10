const { test } = require('node:test');
const assert = require('node:assert/strict');
const { suggestMatchDocument: suggest } = require('../static/js/match_document_matching.js');
const seats = [1, 2].map(n => ({ key: `abono:${n}`, kind: 'abono', sector: '226', fila: '13', asiento: String(n), loaded: '0' }));
const parks = [1, 2].map(n => ({ key: `parking:${n}`, kind: 'parking', name: 'EXTERIOR OESTE', loaded: '0' }));
const seatName = n => `Atlético de Madrid - Villarreal CF-6-Liga-2260013000${n}-. Empresa .-583575.pdf`;
const parkName = 'Atlético de Madrid - Villarreal CF-2--P- EXTERIOR OESTE-. Empresa .-583575.pdf';

test('descompone sector/fila/asiento y evita confundir el identificador final', () => {
  assert.equal(suggest(seatName(1), seats), 'abono:1');
  assert.equal(suggest(seatName(2), seats), 'abono:2');
  assert.equal(suggest('entrada-583575.pdf', seats), null);
});
test('no adivina ubicaciones ambiguas, ocupadas o repetidas en el lote', () => {
  assert.equal(suggest(seatName(1), [...seats, { ...seats[0], key: 'abono:3' }]), null);
  assert.equal(suggest(seatName(1), [{ ...seats[0], loaded: '1' }]), null);
  assert.equal(suggest(seatName(1), seats, new Set(['abono:1'])), null);
  assert.equal(suggest(seatName(1), [...seats, { ...seats[0], key: 'abono:3', loaded: '1' }]), null);
});
test('parking elige el primero del mismo nombre sin PDF ni reserva en el lote', () => {
  assert.equal(suggest(parkName, parks), 'parking:1');
  assert.equal(suggest(parkName, parks, new Set(['parking:1'])), 'parking:2');
  assert.equal(suggest(parkName, [{ ...parks[0], loaded: '1' }, parks[1]]), 'parking:2');
  assert.equal(suggest(parkName, parks, new Set(['parking:1', 'parking:2'])), null);
});
test('normaliza nombres de parking sin coincidencias parciales', () => {
  assert.equal(suggest(parkName, [{ ...parks[0], name: ' extérior   oeste ' }]), 'parking:1');
  assert.equal(suggest(parkName, [{ ...parks[0], name: 'Parking Exterior Oeste' }]), null);
  assert.equal(suggest(parkName.replace('EXTERIOR OESTE', 'HYUNDAI KONA'), [{ ...parks[0], name: 'Hyundai Kona' }]), 'parking:1');
});
test('formato desconocido o código inválido requiere elección manual', () => {
  for (const name of ['entrada.pdf', seatName(1).replace('.pdf', '.txt'), seatName(1).replace('22600130001', '22600000001')]) {
    assert.equal(suggest(name, [...seats, ...parks]), null);
  }
});
