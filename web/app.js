/* Local selection never relies on the drawing order of overlays. */
(async () => {
  const $ = id => document.getElementById(id);
  const {addressKey, prettyAddress, contains, readSaved, typeFamilies, typeFamily, planColor, formatArea, toPlainText} = PulCore;
  const warnings = new Map();
  function warn(key, text) {
    if (text) warnings.set(key, text); else warnings.delete(key);
    $('warnings').replaceChildren(...[...warnings.values()].map(message => node('div', message)));
    $('warnings').hidden = warnings.size === 0;
  }
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function button(text, action, className) {
    const el = node('button', text, className); el.type = 'button'; el.onclick = action; return el;
  }
  if (!window.L) { $('status').textContent = 'Nie udało się wczytać biblioteki mapy. Odśwież stronę.'; return; }
  const map = L.map('map', {preferCanvas: true, zoomControl: false, maxZoom: 19}).setView([53.13, 23.12], 10);
  L.control.zoom({position: 'topright'}).addTo(map);
  L.control.scale({imperial: false, position: 'bottomright'}).addTo(map);
  const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'}).addTo(map);
  const ortho = L.tileLayer('https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMTS/StandardResolution?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTOFOTOMAPA&STYLE=default&FORMAT=image/jpeg&TILEMATRIXSET=EPSG:3857&TILEMATRIX=EPSG:3857:{z}&TILEROW={y}&TILECOL={x}', {maxZoom: 19, attribution: 'Ortofotomapa: <a href="https://www.geoportal.gov.pl/">GUGiK / Geoportal</a>'});
  const mbdl = L.tileLayer.wms('https://mapserver.bdl.lasy.gov.pl/arcgis/services/WMS_BDL/MapServer/WMSServer', {layers: '3,5', format: 'image/png', transparent: true, version: '1.1.1', maxZoom: 19, attribution: 'mBDL / Lasy Państwowe', opacity: .8});
  for (const [key, layer] of [['OSM', osm], ['Ortofotomapa', ortho], ['mBDL', mbdl]]) {
    layer.on('tileerror', () => warn(key, `${key}: nie udało się pobrać części mapy. Lokalne dane i lista nadal działają.`));
    layer.on('tileload', () => warn(key, null));
  }
  document.querySelectorAll('[name=base]').forEach(input => input.onchange = () => {
    if (!input.checked) return;
    map.removeLayer(input.value === 'osm' ? ortho : osm);
    (input.value === 'osm' ? osm : ortho).addTo(map).bringToBack();
    warn(input.value === 'osm' ? 'Ortofotomapa' : 'OSM', null);
  });
  $('mbdl-visible').onchange = e => { if (e.target.checked) mbdl.addTo(map); else { map.removeLayer(mbdl); warn('mBDL', null); } };
  function panel(name) {
    for (const value of ['layers', 'objects', 'list']) {
      $(`${value}-panel`).hidden = name !== value;
      $(`${value}-tab`).setAttribute('aria-pressed', String(name === value));
    }
    scrollActiveEntry();
  }
  $('layers-tab').onclick = () => panel('layers');
  $('objects-tab').onclick = () => panel('objects');
  $('list-tab').onclick = () => panel('list');
  let data;
  try {
    data = await Promise.all(['manifest.json', 'bdl.geojson', 'rozplan.geojson', 'boundary.geojson'].map(async name => {
      const response = await fetch(`layers/${name}`, {cache: 'no-cache'});
      if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
      return response.json();
    }));
  } catch (error) {
    $('status').textContent = 'Błąd wczytywania lokalnych danych. Odśwież stronę, aby ponowić.';
    warn('data', `${error.message}. Uruchom stronę przez serwer HTTP, nie jako plik file://.`);
    return;
  }
  const [manifest, bdl, rozplan, boundary] = data;
  const featureIndex = new Map([...bdl.features, ...rozplan.features].map(f => [`${f.properties.source}:${f.properties.id}`, f]));
  let colorMode = 'percent';
  const planStyle = f => ({color: planColor(f.properties, colorMode), weight: 1.4, fillOpacity: .55});
  let enabledTypes = new Set(manifest.types);
  let activeObjectId = null;
  let activeAddressKey = null;
  const selected = L.geoJSON(null, {interactive: false, style: {color: '#f4b526', weight: 4, fillOpacity: .05}}).addTo(map);
  const focus = L.geoJSON(null, {interactive: false, style: {color: '#102bff', weight: 4, fillOpacity: .13, dashArray: '7 4'}}).addTo(map);
  const bdlLayer = L.geoJSON(bdl, {interactive: false, style: {color: '#345b46', weight: .8, fillOpacity: .025}}).addTo(map);
  const planLayer = L.geoJSON(null, {interactive: false, style: planStyle}).addTo(map);
  const boundaryLayer = L.geoJSON(boundary, {interactive: false, style: {color: '#18392f', weight: 2.5, fill: false, dashArray: '8 6'}}).addTo(map);
  map.fitBounds(boundaryLayer.getBounds(), {padding: [20, 20]});
  $('home').onclick = () => map.fitBounds(boundaryLayer.getBounds(), {padding: [20, 20]});
  const storageKey = 'pul-dojlidy-list-v1';
  let entries = [], blocked = false, savedRaw = null;
  try { savedRaw = localStorage.getItem(storageKey); }
  catch (_) { warn('storage', 'Zapis lokalny jest niedostępny. Możesz pracować tylko w bieżącej sesji.'); }
  try { entries = readSaved(savedRaw, manifest.version, featureIndex); }
  catch (error) {
    blocked = true;
    warn('storage', 'Nie można odtworzyć listy: ' + error.message + ' Oryginalny zapis pozostaje nietknięty. Aby rozpocząć nową listę, użyj „Wyczyść listę”.');
  }
  let lastSavedRaw = savedRaw;
  function persist(reset = false) {
    if (blocked) return;
    function write() {
      if (blocked) return;
      try {
        if (!reset && localStorage.getItem(storageKey) !== lastSavedRaw) {
          blocked = true;
          warn('storage', 'Wykryto nowszy zapis z innej karty. Nie nadpisano go. Bieżące niezapisane notatki pozostają widoczne; zachowaj ich treść przed odświeżeniem.');
          map.closePopup(); renderList(); return;
        }
        const raw = JSON.stringify({schema: 1, version: manifest.version, entries});
        localStorage.setItem(storageKey, raw); lastSavedRaw = raw;
        warn('storage', null);
      } catch (_) { warn('storage', 'Nie udało się zapisać listy. Zmiany pozostają tylko w bieżącej sesji. Nie zamykaj strony, jeśli chcesz zachować notatki.'); }
    }
    // Serialize the snapshot check and write across tabs on HTTPS/localhost.
    if (navigator.locks) navigator.locks.request(storageKey, write).catch(() => warn('storage', 'Nie udało się zablokować zapisu. Bieżące zmiany pozostają tylko w sesji.'));
    else write();
  }
  function updateNote(key, value, origin) {
    if (blocked) return;
    const entry = entries.find(e => e.key === key);
    if (!entry) return;
    entry.note = value;
    document.querySelectorAll('textarea[data-note-key]').forEach(textarea => {
      if (textarea !== origin && textarea.dataset.noteKey === key) textarea.value = value;
    });
    persist();
  }
  function drawSelection() {
    selected.clearLayers();
    for (const entry of entries) {
      const f = featureIndex.get(`${entry.source}:${entry.id}`);
      if (f && (f.properties.source === 'bdl' ? $('bdl-visible').checked : $('plan-visible').checked && enabledTypes.has(f.properties.type))) selected.addData(f);
    }
    boundaryLayer.bringToFront(); selected.bringToFront(); focus.bringToFront();
  }
  function scrollActiveEntry() {
    const sidebar = $('sidebar');
    const active = sidebar.querySelector('section:not([hidden]) .is-active');
    if (!active) return;
    // Scroll only the sidebar, not the document or the map on mobile.
    const bounds = sidebar.getBoundingClientRect();
    const top = bounds.top + sidebar.querySelector('.tabs').getBoundingClientRect().height + 8;
    const bottom = bounds.bottom - 8;
    const item = active.getBoundingClientRect();
    if (item.top < top || item.height > bottom - top) sidebar.scrollTop += item.top - top;
    else if (item.bottom > bottom) sidebar.scrollTop += item.bottom - bottom;
  }
  function updateObjectStates(scroll = false) {
    const savedAddresses = new Set(entries.map(entry => entry.key));
    document.querySelectorAll('.object-button').forEach(item => {
      const active = item.dataset.objectId === activeObjectId;
      const saved = savedAddresses.has(item.dataset.addressKey);
      item.classList.toggle('is-active', active);
      item.classList.toggle('is-saved', saved);
      item.setAttribute('aria-pressed', String(active));
      item.querySelector('.object-saved').hidden = !saved;
    });
    document.querySelectorAll('.entry').forEach(card => {
      const active = card.dataset.addressKey === activeAddressKey;
      card.classList.toggle('is-active', active);
      if (active) card.setAttribute('aria-current', 'true'); else card.removeAttribute('aria-current');
    });
    if (scroll) scrollActiveEntry();
  }
  function renderObjects() {
    const visible = $('plan-visible').checked ? rozplan.features.filter(f => enabledTypes.has(f.properties.type)) : [];
    const collator = new Intl.Collator('pl', {numeric: true});
    visible.sort((a, b) => collator.compare(addressKey(a.properties.address), addressKey(b.properties.address)) || collator.compare(a.properties.compartment, b.properties.compartment) || collator.compare(a.properties.id, b.properties.id));
    $('objects-count').textContent = `Obiekty BULIG: ${visible.length}`;
    $('objects-empty').hidden = visible.length > 0;
    $('objects-empty').textContent = $('plan-visible').checked ? 'Brak obiektów dla wybranych rodzajów rębni.' : 'Włącz warstwę BULIG w zakładce „Warstwy”.';
    const list = document.createDocumentFragment();
    for (const feature of visible) {
      const p = feature.properties;
      const item = button('', () => {
        const bounds = L.geoJSON(feature).getBounds();
        map.fitBounds(bounds, {padding: [35, 35], maxZoom: 17});
        showResults([feature], bounds.getCenter());
      }, 'object-button');
      item.dataset.objectId = p.id;
      item.dataset.addressKey = addressKey(p.address);
      item.append(node('strong', prettyAddress(p.address)), node('span', `${p.type} · ${p.percent}% · działka ${p.compartment}`, 'object-meta'), node('span', 'W uwagach', 'object-saved'));
      const row = node('li'); row.append(item); list.append(row);
    }
    $('objects-list').replaceChildren(list);
    updateObjectStates();
  }
  function renderList() {
    $('count').textContent = entries.length;
    $('export-text').disabled = entries.length === 0;
    const container = $('entries'); container.replaceChildren();
    if (!entries.length) container.append(node('p', 'Lista jest pusta. Kliknij obiekt na mapie i wybierz „Dodaj ten adres do listy”.', 'empty'));
    for (const entry of entries) {
      const card = node('article', undefined, 'entry');
      card.dataset.addressKey = entry.key;
      card.tabIndex = 0;
      card.setAttribute('aria-label', `Uwaga do ${prettyAddress(entry.address)}`);
      const activate = event => {
        if (event.target.closest('button') || activeAddressKey === entry.key) return;
        const feature = featureIndex.get(`${entry.source}:${entry.id}`);
        $(entry.source === 'bdl' ? 'bdl-visible' : 'plan-visible').checked = true;
        if (entry.source === 'rozplan') enabledTypes.add(feature.properties.type);
        syncLayers();
        const bounds = L.geoJSON(feature).getBounds();
        map.fitBounds(bounds, {padding: [35, 35], maxZoom: 17});
        showResults([feature], bounds.getCenter());
      };
      card.onclick = activate;
      card.addEventListener('focusin', activate);
      card.append(node('h3', prettyAddress(entry.address)), node('small', entry.source === 'bdl' ? 'Źródło adresu: BDL 2026' : 'Źródło adresu: BULIG'));
      const label = node('label', 'Twoja notatka');
      const text = node('textarea'); text.value = entry.note; text.disabled = blocked; text.setAttribute('aria-label', `Notatka: ${prettyAddress(entry.address)}`);
      text.dataset.noteKey = entry.key;
      text.oninput = () => updateNote(entry.key, text.value, text);
      label.append(text); card.append(label);
      const actions = node('div', undefined, 'entry-actions');
      actions.append(button('Usuń', () => { if (blocked) return; entries = entries.filter(e => e !== entry); map.closePopup(); persist(); renderList(); }));
      card.append(actions); container.append(card);
    }
    drawSelection();
    updateObjectStates();
  }
  $('clear-list').onclick = () => {
    if (!confirm('Usunąć całą listę i wszystkie notatki z tej przeglądarki?')) return;
    entries = []; blocked = false; persist(true); renderList(); map.closePopup();
  };
  $('export-text').onclick = () => {
    if (!entries.length) return;
    const url = URL.createObjectURL(new Blob([toPlainText(entries)], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'uwagi-pul-dojlidy.txt';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  function add(feature) {
    if (blocked) return;
    const p = feature.properties, key = addressKey(p.address);
    if (!entries.some(e => e.key === key)) entries.push({key, address: p.address, source: p.source, id: p.id, note: ''});
    persist(); renderList(); panel('list');
  }
  function showResults(hits, latlng) {
    map.closePopup();
    const popup = L.popup({maxWidth: 350, minWidth: 220, autoPan: true});
    const root = node('div', undefined, 'popup');
    const addresses = new Set(hits.map(f => addressKey(f.properties.address)));
    if (addresses.size > 1) root.append(node('p', 'Obiekty nakładają się przestrzennie, ale mają różne adresy.', 'notice'));
    const detail = node('div', undefined, 'detail-view');
    const resultButtons = [];
    function choose(feature) {
      const p = feature.properties;
      for (const [btn, f] of resultButtons) btn.setAttribute('aria-pressed', String(f === feature));
      focus.clearLayers(); focus.addData(feature); focus.bringToFront();
      activeObjectId = p.source === 'rozplan' ? p.id : null;
      activeAddressKey = addressKey(p.address);
      updateObjectStates(true);
      detail.replaceChildren(node('div', p.source === 'bdl' ? 'BDL · stan na 2026 rok' : 'BULIG · plan rębni', 'source-label'), node('h3', prettyAddress(p.address)));
      const dl = node('dl');
      const rows = [['Powierzchnia elementu', formatArea(p.areaHa)], ...(p.source === 'bdl' ? Object.entries(p.details || {}) : [['Rodzaj rębni', p.type], ['Planowane pozyskanie', `${p.percent}%`]])];
      for (const [name, value] of rows) dl.append(node('dt', name), node('dd', value === '' || value == null ? 'Brak danych' : String(value)));
      detail.append(dl);
      const existing = entries.find(e => e.key === addressKey(p.address));
      const action = button(existing ? 'Adres już na liście · edytuj notatkę' : 'Dodaj ten adres do listy', () => {
        add(feature); action.textContent = 'Adres już na liście · edytuj notatkę';
      }, 'primary'); action.disabled = blocked;
      detail.append(node('small', existing ? `Adres zapisany ze źródła: ${existing.source === 'bdl' ? 'BDL 2026' : 'BULIG'}. Akcja otwiera istniejący wpis i nie zmienia jego źródła ani geometrii.` : `Źródło dodawanego adresu: ${p.source === 'bdl' ? 'BDL 2026' : 'BULIG'}`), action);
      if (existing) {
        const label = node('label', 'Notatka do adresu');
        const textarea = node('textarea'); textarea.value = existing.note;
        textarea.disabled = blocked;
        textarea.dataset.noteKey = existing.key;
        textarea.oninput = () => updateNote(existing.key, textarea.value, textarea);
        label.append(textarea); detail.append(label);
      }
      if (p.source === 'bdl') {
        const inventory = node('details', undefined, 'inventory');
        inventory.append(node('summary', 'Opis taksacyjny · BDL 2026'));
        inventory.ontoggle = () => {
          if (inventory.open && !inventory.querySelector('.inventory-content')) {
            const content = node('div', undefined, 'inventory-content');
            const number = new Intl.NumberFormat('pl-PL', {maximumFractionDigits: 2});
            function fields(parent, rows) {
              const list = node('dl');
              for (const [label, value] of rows) {
                const text = value == null || value === '' ? 'Brak danych' : typeof value === 'number' ? number.format(value) : value;
                list.append(node('dt', label), node('dd', text));
              }
              parent.append(list);
            }
            fields(content, [['Struktura drzewostanu (kod BDL)', p.inventory?.structure], ['Wiek rębności [lata]', p.inventory?.rotationAge]]);
            if (!p.inventory?.layers?.length) content.append(node('p', 'Brak danych o warstwach drzewostanu.', 'notice'));
            for (const layer of p.inventory?.layers || []) {
              const section = node('section', undefined, 'inventory-layer');
              section.append(node('h4', layer.name));
              fields(section, [['Zwarcie', layer.density], ['Zmieszanie', layer.mixture], ['Wskaźnik zadrzewienia', layer.stocking]]);
              const species = node('ul', undefined, 'inventory-species');
              if (!layer.species.length) section.append(node('p', 'Brak danych o gatunkach.', 'notice'));
              for (const item of layer.species) {
                const row = node('li'); row.append(node('h5', item.name));
                fields(row, [['Udział', item.share], ['Wiek [lata]', item.age], ['Bonitacja', item.siteClass], ['Wysokość [m]', item.height], ['Pierśnica [cm]', item.diameter]]);
                species.append(row);
              }
              section.append(species); content.append(section);
            }
            inventory.append(content);
          }
          popup.update();
        };
        detail.append(inventory);
      }
      popup.update();
    }
    for (const source of ['bdl', 'rozplan']) {
      const group = hits.filter(f => f.properties.source === source);
      if (!group.length) continue;
      const section = node('section');
      section.append(node('div', `${source === 'bdl' ? 'Wydzielenie BDL' : 'Plan rębni'} · ${group.length}`, 'source-label'));
      for (const f of group) {
        const p = f.properties;
        const btn = button(prettyAddress(p.address) + (source === 'rozplan' ? ` · ${p.type} · działka ${p.compartment}` : ''), () => choose(f), 'result-button');
        resultButtons.push([btn, f]); section.append(btn);
      }
      root.append(section);
    }
    root.append(detail);
    if (hits.length > 1) detail.append(node('p', 'Wybierz obiekt powyżej, aby zobaczyć jego dane.', 'notice'));
    popup.setLatLng(latlng).setContent(root).openOn(map);
    if (hits.length === 1) choose(hits[0]);
  }
  map.on('popupclose', () => { focus.clearLayers(); activeObjectId = null; activeAddressKey = null; updateObjectStates(); });
  map.on('click', e => {
    const hits = [];
    if ($('bdl-visible').checked) hits.push(...bdl.features.filter(f => contains(f, e.latlng.lng, e.latlng.lat)));
    if ($('plan-visible').checked) hits.push(...rozplan.features.filter(f => enabledTypes.has(f.properties.type) && contains(f, e.latlng.lng, e.latlng.lat)));
    focus.clearLayers();
    if (hits.length) { showResults(hits, e.latlng); $('status').textContent = `Znaleziono obiektów: ${hits.length}. Wybierz źródło w popupie.`; }
    else { map.closePopup(); $('status').textContent = 'Brak obiektów widocznych warstw w tym miejscu.'; }
  });
  function syncLayers() {
    map.closePopup(); focus.clearLayers();
    for (const [input, layer] of [['bdl-visible', bdlLayer], ['plan-visible', planLayer]]) {
      if ($(input).checked) layer.addTo(map); else map.removeLayer(layer);
    }
    planLayer.clearLayers();
    const visible = rozplan.features.filter(f => enabledTypes.has(f.properties.type));
    planLayer.addData(visible);
    $('visible-count').textContent = `${$('plan-visible').checked ? visible.length : 0} / ${rozplan.features.length}`;
    document.querySelectorAll('[data-type]').forEach(input => input.checked = enabledTypes.has(input.dataset.type));
    for (const group of filterGroups) {
      const count = group.types.filter(type => enabledTypes.has(type)).length;
      group.input.checked = count === group.types.length;
      group.input.indeterminate = count > 0 && count < group.types.length;
    }
    drawSelection();
    renderObjects();
  }
  const filterGroups = [
    {id: 'clearcut', label: 'Zupełne', types: manifest.types.filter(type => typeFamily(type) === 'I')},
    {id: 'complex', label: 'Złożone', types: manifest.types.filter(type => typeFamily(type) !== 'I')},
  ];
  for (const group of filterGroups) {
    const wrapper = node('div', undefined, 'type-group');
    const heading = node('div', undefined, 'group-heading');
    const input = node('input'); input.type = 'checkbox'; input.dataset.group = group.id;
    input.setAttribute('aria-label', `Wszystkie: ${group.label}`); group.input = input;
    input.onchange = () => {
      for (const type of group.types) { if (input.checked) enabledTypes.add(type); else enabledTypes.delete(type); }
      syncLayers();
    };
    const children = node('div', undefined, 'types'); children.id = `types-${group.id}`;
    const toggle = button(group.label, () => {
      children.hidden = !children.hidden;
      toggle.setAttribute('aria-expanded', String(!children.hidden));
    }, 'group-toggle');
    toggle.setAttribute('aria-controls', children.id); toggle.setAttribute('aria-expanded', 'true');
    heading.append(input, toggle); wrapper.append(heading, children);
    for (const type of group.types) {
      const label = node('label', undefined, 'type-label');
      const child = node('input'); child.type = 'checkbox'; child.checked = true; child.dataset.type = type;
      child.onchange = () => { if (child.checked) enabledTypes.add(type); else enabledTypes.delete(type); syncLayers(); };
      const swatch = node('span', undefined, 'swatch'); swatch.dataset.typeColor = type;
      swatch.style.backgroundColor = planColor({type}, 'type');
      label.append(child, swatch, document.createTextNode(type)); children.append(label);
    }
    $('types').append(wrapper);
  }
  function renderLegend() {
    const legend = $('plan-legend'); legend.replaceChildren();
    if (colorMode === 'percent') {
      legend.append(node('div', 'Procent pozyskania', 'legend-title'));
      const ramp = node('div', undefined, 'color-ramp');
      ramp.style.background = `linear-gradient(to right, ${planColor({percent: 0}, 'percent')}, ${planColor({percent: 100}, 'percent')})`;
      ramp.setAttribute('aria-hidden', 'true');
      const ticks = node('div', undefined, 'legend-ticks');
      for (const value of [0, 25, 50, 75, 100]) ticks.append(node('span', `${value}%`));
      legend.append(ramp, ticks);
    } else {
      legend.append(node('div', 'Rodzaj rębni', 'legend-title'));
      for (const family of typeFamilies) {
        const row = node('div', undefined, 'legend-row'), swatch = node('span', undefined, 'swatch');
        swatch.style.backgroundColor = family.color;
        row.append(swatch, document.createTextNode(`${family.code} · ${family.label}`)); legend.append(row);
      }
    }
    document.querySelectorAll('[data-type-color]').forEach(swatch => swatch.hidden = colorMode === 'percent');
  }
  $('color-mode').onchange = e => {
    colorMode = e.target.value; planLayer.setStyle(planStyle); renderLegend();
    selected.bringToFront(); focus.bringToFront();
  };
  for (const id of ['bdl-visible', 'plan-visible']) $(id).onchange = syncLayers;
  $('all-types').onclick = () => { enabledTypes = new Set(manifest.types); syncLayers(); };
  $('no-types').onclick = () => { enabledTypes.clear(); syncLayers(); };
  window.addEventListener('storage', e => {
    if (e.key === storageKey || e.key === null) {
      blocked = true;
      warn('storage', 'Lista zmieniła się w innej karcie. Odśwież stronę przed dalszą edycją, aby nie nadpisać zmian.');
      document.querySelectorAll('.entry textarea').forEach(el => el.disabled = true);
      map.closePopup();
    }
  });
  renderList(); syncLayers(); renderLegend();
  $('status').textContent = `${bdl.features.length} wydzieleń · ${rozplan.features.length} obiektów planu. Kliknij mapę, aby porównać źródła.`;
})();
