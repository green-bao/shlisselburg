'use strict';
const $ = id => document.getElementById(id);
const svgNS = 'http://www.w3.org/2000/svg';
const fmt = value => value == null ? 'Потребность 0' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(value);
const pct = value => value == null ? 'Потребность 0' : `${fmt(value * 100)} %`;
const modeName = mode => mode === 'walk' ? 'Пешком' : 'На автомобиле';
const state = {mode: 'walk', boot: null, result: null, additions: [], selected: null, view: 'baseline', busy: false, job: null, saved: [], mapBox: [0, 0, 900, 650]};
const controls = ['mode', 'add-school', 'calculate', 'reset', 'search', 'save-scenario', 'import-scenarios'];

function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function message(text, error = false) { $('status').textContent = text; $('status').className = error ? 'error' : ''; }
function busy(value) { state.busy = value; controls.forEach(id => $(id).disabled = value); }
async function request(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Ошибка запроса: ${response.status}`);
  return result;
}
function capacity(id) {
  const raw = $(id).value.trim();
  if (!/^[1-9]\d*$/.test(raw) || !Number.isSafeInteger(Number(raw))) throw new Error('Вместимость должна быть положительным целым числом.');
  return Number(raw);
}
function original() { return state.boot.baselines[state.mode]; }
function baseMap() { return new Map(original().blocks.map(row => [row.block_id, row])); }
function currentMap() { return new Map(state.result.blocks.map(row => [row.block_id, row])); }
function copyAdditions(items) { return items.map(item => ({block_id: item.block_id, capacity: item.capacity})); }
function hasPending() { return JSON.stringify(state.additions) !== JSON.stringify(state.result.additions); }
function renderAdditions() {
  $('additions').replaceChildren();
  if (!state.additions.length) $('additions').append(element('li', 'Новых школ пока нет.', 'hint'));
  state.additions.forEach((item, index) => {
    const li = element('li'); li.append(element('span', `Квартал ${item.block_id}: ${fmt(item.capacity)} мест`));
    const remove = element('button', 'Убрать'); remove.type = 'button'; remove.disabled = state.busy;
    remove.addEventListener('click', () => { state.additions.splice(index, 1); renderAdditions(); });
    li.append(remove); $('additions').append(li);
  });
  $('pending').textContent = hasPending() ? 'Список изменён. Нажмите «Рассчитать сценарий», чтобы обновить карту и показатели.' : '';
}
function selectBlock(id) {
  state.selected = Number(id);
  $('block-id').value = String(id);
  renderCard(); renderMap();
  document.querySelectorAll('#results-table tbody tr').forEach(row => row.classList.toggle('selected', Number(row.dataset.blockId) === state.selected));
}
function renderCard() {
  $('block-card').replaceChildren();
  if (state.selected == null) return;
  const row = currentMap().get(state.selected); const base = baseMap().get(state.selected);
  $('selected-title').textContent = `Квартал ${row.block_id}`;
  [['Население', `${fmt(row.population)} чел.`], ['Потребность', `${fmt(row.demand)} мест`], ['Вместимость исходная', `${fmt(base.capacity)} мест`], ['Вместимость сценария', `${fmt(row.capacity)} мест`], ['Обеспеченность исходная', pct(base.provision)], ['Обеспеченность сценария', pct(row.provision)], ['Дефицит сценария', `${fmt(row.deficit)} мест`]].forEach(([label, value]) => {
    const line = element('div'); line.append(element('dt', label), element('dd', value)); $('block-card').append(line);
  });
}
function renderResults() {
  const totals = state.result.totals;
  $('metric-demand').textContent = fmt(totals.demand);
  $('metric-capacity').textContent = fmt(totals.capacity);
  $('metric-new').textContent = fmt(totals.added_capacity);
  $('metric-provision').textContent = pct(totals.provision);
  $('metric-original-provision').textContent = pct(original().totals.provision);
  $('metric-deficit').textContent = fmt(totals.deficit);
  $('metric-reduction').textContent = fmt(totals.deficit_reduction);
  const tbody = $('results-table').tBodies[0]; tbody.replaceChildren();
  const bases = baseMap();
  state.result.blocks.forEach(row => {
    const base = bases.get(row.block_id); const tr = element('tr', undefined, 'clickable'); tr.dataset.blockId = row.block_id;
    [row.block_id, fmt(row.population), fmt(row.demand), fmt(base.capacity), fmt(row.capacity), base.provision == null ? 'Потребность 0' : fmt(base.provision * 100), row.provision == null ? 'Потребность 0' : fmt(row.provision * 100), fmt(base.deficit), fmt(row.deficit), fmt(base.deficit - row.deficit)].forEach(value => tr.append(element('td', value)));
    tr.classList.toggle('selected', state.selected === row.block_id); tr.addEventListener('click', () => selectBlock(row.block_id)); tbody.append(tr);
  });
  renderCard(); renderMap(); renderAdditions();
}

let projectPoint;
function allPoints(geometry) {
  if (!geometry) return [];
  if (geometry.type === 'GeometryCollection') return geometry.geometries.flatMap(allPoints);
  const flatten = value => typeof value[0] === 'number' ? [value] : value.flatMap(flatten);
  return flatten(geometry.coordinates);
}
function svgPath(geometry) {
  const ring = coordinates => coordinates.map((point, i) => `${i ? 'L' : 'M'}${projectPoint(point).join(',')}`).join(' ') + ' Z';
  const line = coordinates => coordinates.map((point, i) => `${i ? 'L' : 'M'}${projectPoint(point).join(',')}`).join(' ');
  if (geometry.type === 'Polygon') return geometry.coordinates.map(ring).join(' ');
  if (geometry.type === 'MultiPolygon') return geometry.coordinates.flatMap(polygon => polygon.map(ring)).join(' ');
  if (geometry.type === 'LineString') return line(geometry.coordinates);
  if (geometry.type === 'MultiLineString') return geometry.coordinates.map(line).join(' ');
  return '';
}
function svgElement(tag, attrs) { const node = document.createElementNS(svgNS, tag); Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value)); return node; }
function setupMap() {
  const points = state.boot.geometry.features.flatMap(feature => allPoints(feature.geometry));
  const minX = Math.min(...points.map(p => p[0])), maxX = Math.max(...points.map(p => p[0]));
  const minY = Math.min(...points.map(p => p[1])), maxY = Math.max(...points.map(p => p[1]));
  const correction = Math.cos((minY + maxY) / 2 * Math.PI / 180);
  const width = (maxX - minX) * correction, height = maxY - minY;
  const scale = Math.min(790 / width, 550 / height);
  const offsetX = (900 - width * scale) / 2, offsetY = (650 - height * scale) / 2;
  projectPoint = ([x, y]) => [offsetX + (x - minX) * correction * scale, 650 - offsetY - (y - minY) * scale];
  for (const [key, fill, stroke, sw] of [['water', '#cee3e8', '#b7d2da', .8], ['roads', 'none', '#aac0c3', .65]]) {
    for (const feature of state.boot[key]?.features || []) {
      const path = svgElement('path', {d: svgPath(feature.geometry), fill, stroke, 'stroke-width': sw, 'fill-rule': 'evenodd', 'vector-effect': 'non-scaling-stroke'});
      $('geography').append(path);
    }
  }
  for (const feature of state.boot.geometry.features) {
    const id = Number(feature.properties.block_id ?? feature.properties.id ?? feature.id);
    const path = svgElement('path', {d: svgPath(feature.geometry), class: 'block', 'data-block-id': id, tabindex: '0', role: 'button', 'aria-label': `Выбрать квартал ${id}`, 'fill-rule': 'evenodd'});
    path.append(svgElement('title')); path.addEventListener('click', () => selectBlock(id));
    path.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectBlock(id); } });
    $('blocks').append(path);
    const coordinates = allPoints(feature.geometry); const midpoint = [coordinates.reduce((sum, p) => sum + p[0], 0) / coordinates.length, coordinates.reduce((sum, p) => sum + p[1], 0) / coordinates.length];
    const [x, y] = projectPoint(midpoint); const label = svgElement('text', {x, y, class: 'map-label', 'data-label-id': id}); label.textContent = id; $('labels').append(label);
  }
  const north = svgElement('text', {x: 38, y: 43, fill: '#456d77', 'font-size': 14}); north.textContent = 'Север ↑'; $('labels').append(north);
  const streetNames = ['Красный проспект', 'Красный тракт', 'улица Староладожский канал', 'улица Новоладожский канал', 'улица Малоневский канал'];
  for (const name of streetNames) {
    const roads = (state.boot.roads?.features || []).filter(feature => feature.properties.name === name && feature.geometry.type === 'LineString');
    let longest = null, length = 0;
    for (const road of roads) {
      const pts = road.geometry.coordinates.map(projectPoint); let size = 0;
      for (let i = 1; i < pts.length; i++) size += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
      if (size > length) { longest = pts; length = size; }
    }
    if (longest) { const mid = longest[Math.floor(longest.length / 2)]; const label = svgElement('text', {x: mid[0] + 7, y: mid[1] - 6, class: 'street-label'}); label.textContent = name.replace('улица ', 'ул. '); $('labels').append(label); }
  }
  state.result.blocks.forEach(row => { const option = element('option', `Квартал ${row.block_id}`); option.value = row.block_id; $('block-id').append(option); });
  let drag = null;
  $('map').addEventListener('pointerdown', event => { if (event.target === $('map')) drag = [event.clientX, event.clientY, ...state.mapBox]; });
  $('map').addEventListener('pointermove', event => { if (!drag) return; const ratio = state.mapBox[2] / $('map').getBoundingClientRect().width; state.mapBox[0] = drag[2] - (event.clientX - drag[0]) * ratio; state.mapBox[1] = drag[3] - (event.clientY - drag[1]) * ratio; updateViewBox(); });
  document.addEventListener('pointerup', () => { drag = null; });
}
function updateViewBox() { $('map').setAttribute('viewBox', state.mapBox.join(' ')); renderMap(); }
function zoom(factor) { const [x, y, w, h] = state.mapBox; if (w * factor < 100 || w * factor > 1800) return; state.mapBox = [x + w * (1 - factor) / 2, y + h * (1 - factor) / 2, w * factor, h * factor]; updateViewBox(); }
function provisionColor(value) { return value === null ? '#d2d8dc' : value < .25 ? '#ca453f' : value < .5 ? '#ef996d' : value < .75 ? '#f4dda0' : value < 1 ? '#97be76' : '#329969'; }
function changeColor(value) { return value === null ? '#dbe1e3' : value < -1e-9 ? '#d6a261' : value > 1e-9 ? '#4f9e9b' : '#edf1ef'; }
function renderMap() {
  if (!state.boot || !state.result) return;
  const bases = baseMap(), rows = currentMap(), planned = new Set(state.result.additions.map(item => item.block_id));
  document.querySelectorAll('.block').forEach(path => {
    const id = Number(path.dataset.blockId), row = rows.get(id), base = bases.get(id);
    let color, text;
    if (state.view === 'change') {
      const delta = row.provision === null ? null : row.provision - base.provision;
      color = changeColor(delta); text = delta === null ? 'Нет расчётной потребности' : `Изменение обеспеченности: ${fmt(delta * 100)} п.п.`;
    } else { const value = state.view === 'baseline' ? base : row; color = provisionColor(value.provision); text = `Обеспеченность: ${pct(value.provision)}; дефицит: ${fmt(value.deficit)} мест`; }
    path.setAttribute('fill', color); path.querySelector('title').textContent = `Квартал ${id}. ${text}`;
    path.classList.toggle('selected', id === state.selected); path.classList.toggle('planned', planned.has(id));
  });
  document.querySelectorAll('[data-label-id]').forEach(label => { const id = Number(label.dataset.labelId); label.style.display = state.mapBox[2] < 500 || rows.get(id).demand > 0 || planned.has(id) || state.selected === id ? '' : 'none'; });
  const items = state.view === 'change' ? [['#d6a261', 'Снижение'], ['#edf1ef', 'Без изменения'], ['#4f9e9b', 'Рост'], ['#dbe1e3', 'Потребность 0']] : [['#ca453f', 'Менее 25 %'], ['#ef996d', '25 - менее 50 %'], ['#f4dda0', '50 - менее 75 %'], ['#97be76', '75 - менее 100 %'], ['#329969', '100 %'], ['#d2d8dc', 'Потребность 0']];
  $('legend').replaceChildren(); items.forEach(([color, title]) => { const span = element('span'); const swatch = element('i'); swatch.style.background = color; span.append(swatch, document.createTextNode(title)); $('legend').append(span); });
  $('map-description').textContent = state.view === 'change' ? 'Изменение обеспеченности относительно исходного состояния, в процентных пунктах. Фиолетовый контур обозначает квартал с новой школой.' : `${state.view === 'baseline' ? 'Исходное состояние' : 'Рассчитанный сценарий'}. Цвет показывает обеспеченность в процентах расчётной потребности. Нажмите на квартал, чтобы открыть его показатели.`;
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === state.view));
}

async function calculate(additions = state.additions, notification = 'Сценарий рассчитан для всей территории.') {
  busy(true); message('Расчёт обеспеченности...');
  try { state.result = await request('/api/calculate', {mode: state.mode, additions}); state.additions = copyAdditions(state.result.additions); state.view = 'scenario'; renderResults(); message(`${notification} ${modeName(state.mode)}.`); }
  catch (error) { message(error.message, true); }
  finally { busy(false); renderAdditions(); }
}
function clearSearch() { state.job = null; $('ranking-table').tBodies[0].replaceChildren(); $('best-summary').textContent = 'Здесь появятся все проверенные варианты. Выберите строку, чтобы посмотреть размещение на карте.'; $('search-status').textContent = 'Поиск ещё не выполнялся.'; $('search-progress').value = 0; $('export-ranking').disabled = true; }
function renderSearch(job) {
  state.job = job; $('search-progress').max = job.total; $('search-progress').value = job.checked;
  const errors = job.errors || []; const best = job.complete ? (job.best_block_ids || []) : (job.best_calculated_block_ids || []);
  $('search-status').textContent = `Проверено ${job.checked} из ${job.total}; ошибок: ${errors.length}. ${modeName(job.mode)}.`;
  $('best-summary').textContent = job.status === 'complete' ? (job.complete ? `Полный перебор завершён. Лучшие кварталы (${best.length}): ${best.join(', ')}. Вместимость школы: ${fmt(job.capacity)} мест.` : `Перебор завершён с ошибками. Лучшие среди успешно рассчитанных кварталов: ${best.join(', ') || 'нет'}. Проверка всех вариантов не выполнена.`) : 'Перебор выполняется. Таблица пополняется по мере расчёта.';
  const tbody = $('ranking-table').tBodies[0]; tbody.replaceChildren();
  let rank = 0, previousReduction = null, previousRank = 0;
  for (const item of job.ranking || []) {
    rank++; if (item.deficit_reduction !== previousReduction) previousRank = rank; previousReduction = item.deficit_reduction;
    const tr = element('tr', undefined, item.error ? 'error-row' : 'clickable');
    if (best.includes(item.block_id)) tr.classList.add('best');
    [item.error ? '-' : previousRank, item.block_id, item.error ? '-' : fmt(item.deficit), item.error ? '-' : fmt(item.deficit_reduction), item.error || (best.includes(item.block_id) ? (job.complete ? 'Лучший результат' : 'Лучший среди рассчитанных') : 'Рассчитано')].forEach(value => tr.append(element('td', value)));
    if (!item.error) tr.addEventListener('click', () => { if (state.busy) return; selectBlock(item.block_id); calculate([{block_id: item.block_id, capacity: job.capacity}], `Выбран вариант поиска: квартал ${item.block_id}.`); });
    tbody.append(tr);
  }
  $('export-ranking').disabled = !(job.ranking || []).length;
}
async function startSearch() {
  try {
    const places = capacity('auto-capacity'); busy(true); renderAdditions(); message('Автоматический поиск начат на основе исходного состояния.');
    const {job_id} = await request('/api/search', {mode: state.mode, capacity: places});
    let job;
    do { job = await request(`/api/search/${job_id}`); renderSearch(job); if (job.status === 'running') await new Promise(resolve => setTimeout(resolve, 250)); } while (job.status === 'running');
    if (job.status === 'failed') throw new Error(job.error || 'Автоматический поиск прерван.');
    busy(false);
    const best = job.complete ? job.best_block_ids : job.best_calculated_block_ids;
    if (best?.length) { selectBlock(best[0]); await calculate([{block_id: best[0], capacity: places}], job.complete ? 'На карте показан один из лучших вариантов полного перебора.' : 'На карте показан лучший из успешно рассчитанных вариантов.'); }
    else message('Успешно рассчитанных вариантов нет. Подробности приведены в рейтинге.', true);
  } catch (error) { message(error.message, true); }
  finally { busy(false); renderAdditions(); }
}
function download(blob, name) { const url = URL.createObjectURL(blob); const link = element('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function persistSaved() { try { localStorage.setItem('school-placement-scenarios-v1', JSON.stringify(state.saved)); } catch { message('Сценарии доступны в текущем окне. Для хранения сохраните JSON.', true); } }
function renderSaved() {
  const tbody = $('saved-table').tBodies[0]; tbody.replaceChildren();
  for (const item of state.saved) {
    const tr = element('tr');
    [item.name, modeName(item.mode), item.additions.map(a => `Квартал ${a.block_id}: ${a.capacity} мест`).join('; ') || 'Нет', fmt(item.totals.added_capacity), fmt(item.totals.deficit), fmt(item.totals.deficit_reduction)].forEach(value => tr.append(element('td', value)));
    const basis = element('td', item.basis || 'Не указано', 'basis-cell'); tr.append(basis);
    const actions = element('td'); const load = element('button', 'Открыть'); const remove = element('button', 'Удалить');
    load.addEventListener('click', async () => { if (state.busy) return; state.mode = item.mode; $('mode').value = item.mode; clearSearch(); await calculate(copyAdditions(item.additions), `Открыт сценарий «${item.name}».`); });
    remove.addEventListener('click', () => { state.saved = state.saved.filter(row => row !== item); persistSaved(); renderSaved(); });
    actions.append(load, remove); tr.append(actions); tbody.append(tr);
  }
}
function saveScenario() {
  const name = $('scenario-name').value.trim(); if (!name) return message('Введите название сценария.', true);
  if (hasPending()) return message('Сначала рассчитайте изменения из списка новых школ.', true);
  state.saved.push({name, basis: $('scenario-basis').value.trim(), mode: state.mode, additions: copyAdditions(state.result.additions), totals: {...state.result.totals}}); persistSaved(); renderSaved(); $('scenario-name').value = ''; $('scenario-basis').value = ''; message(`Сценарий «${name}» сохранён в браузере.`);
}
async function importScenarios(file) {
  if (!file) return;
  busy(true);
  try {
    const content = JSON.parse(await file.text()); const rows = Array.isArray(content) ? content : content.scenarios;
    if (!Array.isArray(rows) || rows.length > 100) throw new Error('Файл должен содержать не больше 100 сценариев.');
    const imported = [];
    for (const row of rows) {
      const scenario = row.result || row;
      if (typeof row.name !== 'string' || !row.name.trim() || row.name.length > 80 || !['walk', 'drive'].includes(scenario.mode) || !Array.isArray(scenario.additions)) throw new Error('Неверное описание сценария в JSON.');
      const result = await request('/api/calculate', {mode: scenario.mode, additions: scenario.additions});
      imported.push({name: row.name, basis: typeof row.basis === 'string' ? row.basis.slice(0, 2000) : '', mode: scenario.mode, additions: result.additions, totals: result.totals});
    }
    state.saved.push(...imported); persistSaved(); renderSaved(); message(`Загружено и пересчитано сценариев: ${imported.length}.`);
  } catch (error) { message(error.message, true); }
  finally { busy(false); $('import-scenarios').value = ''; }
}
function csvText(rows) { return '\uFEFF' + rows.map(row => row.map(value => `"${String(value ?? '').replaceAll('"', '""')}"`).join(';')).join('\r\n'); }
async function init() {
  busy(true);
  try {
    state.boot = await request('/api/bootstrap'); state.result = original(); setupMap(); renderResults();
    try { const saved = JSON.parse(localStorage.getItem('school-placement-scenarios-v1') || '[]'); state.saved = Array.isArray(saved) ? saved : []; } catch { state.saved = []; }
    renderSaved(); message(`Исходное состояние загружено. Кварталов: ${state.result.blocks.length}. ${modeName(state.mode)}.`);
  } catch (error) { message(`Не удалось загрузить данные: ${error.message}`, true); }
  finally { busy(false); }
}
$('block-id').addEventListener('change', event => { if (event.target.value !== '') selectBlock(event.target.value); });
$('add-school').addEventListener('click', () => { try { if (state.selected == null) throw new Error('Выберите квартал на карте или в списке.'); state.additions.push({block_id: state.selected, capacity: capacity('capacity')}); renderAdditions(); message('Школа добавлена в список. Теперь рассчитайте сценарий.'); } catch (error) { message(error.message, true); } });
$('calculate').addEventListener('click', () => calculate());
$('reset').addEventListener('click', () => { state.additions = []; state.result = original(); state.view = 'baseline'; clearSearch(); renderResults(); message(`Восстановлено исходное состояние. ${modeName(state.mode)}.`); });
$('mode').addEventListener('change', async event => { state.mode = event.target.value; clearSearch(); if (state.additions.length) await calculate(); else { state.result = original(); renderResults(); message(`Исходное состояние. ${modeName(state.mode)}.`); } });
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => { state.view = button.dataset.view; renderMap(); }));
$('zoom-in').addEventListener('click', () => zoom(.8)); $('zoom-out').addEventListener('click', () => zoom(1.25)); $('zoom-reset').addEventListener('click', () => { state.mapBox = [0, 0, 900, 650]; updateViewBox(); });
$('search').addEventListener('click', startSearch); $('save-scenario').addEventListener('click', saveScenario);
$('export-csv').addEventListener('click', async () => { try { const response = await fetch('/api/export-csv', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: state.mode, additions: state.result.additions})}); if (!response.ok) throw new Error((await response.json()).error); download(await response.blob(), `school-placement-${state.mode}.csv`); message('Таблица рассчитанного состояния сохранена в CSV.'); } catch (error) { message(error.message, true); } });
$('export-ranking').addEventListener('click', () => { if (!state.job) return; const rows = [['Режим', 'Вместимость школы, мест', 'Квартал', 'Дефицит, мест', 'Сокращение дефицита, мест', 'Ошибка', 'Полный перебор'], ...state.job.ranking.map(row => [modeName(state.job.mode), state.job.capacity, row.block_id, row.deficit, row.deficit_reduction, row.error || '', state.job.complete ? 'Да' : 'Нет'])]; download(new Blob([csvText(rows)], {type: 'text/csv;charset=utf-8'}), `school-search-${state.job.mode}.csv`); });
$('export-scenarios').addEventListener('click', () => download(new Blob([JSON.stringify({format: 'school-placement-v1', scenarios: state.saved}, null, 2)], {type: 'application/json'}), 'school-scenarios.json'));
$('import-scenarios').addEventListener('change', event => importScenarios(event.target.files[0]));
init();
