/* Geometry and saved-list validation are independent of Leaflet. */
window.PulCore = (() => {
  const addressKey = value => value.replace(/\s+/g, '');
  const prettyAddress = value => value.split('-').map(part => part.trim()).join('-');
  const typeFamilies = [
    {code: 'I', label: 'Zupełne', color: '#b8d8e8'},
    {code: 'II', label: 'Częściowe', color: '#80b9d3'},
    {code: 'III', label: 'Gniazdowe', color: '#4c95b5'},
    {code: 'IV', label: 'Stopniowe', color: '#296f91'},
    {code: 'V', label: 'Przerębowe', color: '#164864'},
  ];
  const typeFamily = type => type.match(/^(III|II|IV|V|I)/)?.[0];
  function planColor(properties, mode) {
    if (mode === 'type') return typeFamilies.find(family => family.code === typeFamily(properties.type))?.color || '#777777';
    const fraction = Math.max(0, Math.min(100, properties.percent)) / 100;
    const light = [255, 240, 207], dark = [138, 50, 31];
    return '#' + light.map((start, i) => Math.round(start + (dark[i] - start) * fraction).toString(16).padStart(2, '0')).join('');
  }
  function formatArea(areaHa) {
    if (!Number.isFinite(areaHa) || areaHa < 0) return 'Brak danych';
    if (areaHa > 0 && areaHa < 0.01) return '< 0,01 ha';
    return new Intl.NumberFormat('pl-PL', {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(areaHa) + ' ha';
  }
  function ringContains(ring, x, y) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [ax, ay] = ring[j], [bx, by] = ring[i];
      const cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax);
      if (Math.abs(cross) < 1e-14 && x >= Math.min(ax, bx) && x <= Math.max(ax, bx) && y >= Math.min(ay, by) && y <= Math.max(ay, by)) return true;
      if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
    }
    return inside;
  }
  function contains(feature, lng, lat) {
    const b = feature.bbox;
    if (b && (lng < b[0] || lat < b[1] || lng > b[2] || lat > b[3])) return false;
    const geometry = feature.geometry;
    const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
    return polygons.some(rings => ringContains(rings[0], lng, lat) && !rings.slice(1).some(ring => ringContains(ring, lng, lat)));
  }
  function readSaved(raw, version, features) {
    if (raw === null) return [];
    const saved = JSON.parse(raw);
    if (saved.schema !== 1 || saved.version !== version || !Array.isArray(saved.entries)) throw new Error('Inna wersja danych lub niepoprawny zapis.');
    const keys = new Set();
    for (const entry of saved.entries) {
      if (!entry || typeof entry !== 'object' || !['bdl', 'rozplan'].includes(entry.source) || !['id', 'key', 'address', 'note'].every(key => typeof entry[key] === 'string')) throw new Error('Niepoprawne pola zapisanej pozycji.');
      const feature = features.get(`${entry.source}:${entry.id}`);
      if (!feature || typeof entry.address !== 'string' || typeof entry.note !== 'string' || entry.key !== addressKey(entry.address) || entry.key !== addressKey(feature.properties.address) || keys.has(entry.key)) throw new Error('Lista zawiera niezgodne pozycje.');
      keys.add(entry.key);
    }
    return saved.entries;
  }
  return {addressKey, prettyAddress, contains, readSaved, typeFamilies, typeFamily, planColor, formatArea};
})();
