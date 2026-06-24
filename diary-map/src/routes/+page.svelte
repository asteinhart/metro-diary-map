<script>
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import STYLE_URL from './toner_style.json';

	// Manhattan-ish center
	const CENTER = [-73.97, 40.758];
	const ZOOM = 11.5;

	// The three placements process.py emits — each its own GeoJSON of the same entries,
	// differing only in *where* the dot lands. Switching views swaps the points source.
	const VIEWS = [
		{ id: 'specific', label: 'Specific', file: `${base}/data/locations.geojson` },
		{ id: 'subway', label: 'Subway', file: `${base}/data/subway.geojson` },
		{ id: 'borough', label: 'Borough', file: `${base}/data/areas.geojson` }
	];

	const VIEW_DESC = {
		specific: 'Dropped at the exact place each diary names.',
		subway: 'Placed along the subway line each diary mentions.',
		borough: 'Scattered through the borough each diary names.'
	};

	const EMPTY = { type: 'FeatureCollection', features: [] };

	let mapEl;
	let map;
	let activeView = $state('specific');
	let loading = $state(true);
	let count = $state(0);
	let total = $state(0);
	// debug control: hide geocoded dots below this confidence (null confidence always shows)
	let minConfidence = $state(0);
	// year range — bounds are learned from the data the first time a view loads
	let yearFloor = $state(1976);
	let yearCeil = $state(2026);
	let yearLo = $state(1976);
	let yearHi = $state(2026);
	let yearInit = false;

	const filtered = $derived(
		minConfidence > 0 || yearLo > yearFloor || yearHi < yearCeil
	);
	const countLabel = $derived(
		loading
			? 'Loading…'
			: filtered
				? `${count.toLocaleString()} of ${total.toLocaleString()} shown`
				: `${total.toLocaleString()} entries`
	);

	// caches so toggling a view (or re-hovering) never re-fetches
	const viewCache = {}; // view id -> FeatureCollection
	let bodies = {}; // entry_id -> body text (loaded once, looked up on hover)

	async function loadView(id) {
		if (!viewCache[id]) {
			const view = VIEWS.find((v) => v.id === id);
			viewCache[id] = await fetch(view.file).then((r) => r.json());
		}
		return viewCache[id];
	}

	let loadToken = 0;
	async function showView(id) {
		activeView = id;
		loading = true;
		const token = ++loadToken;
		const data = await loadView(id);
		if (token !== loadToken) return; // a newer click superseded this one
		map.getSource('diary-points')?.setData(data);
		setLayerVisible('subway-lines', id === 'subway');
		setLayerVisible('borough-outline', id === 'borough');
		total = data.features.length;
		initYearBounds(data);
		applyFilters();
		loading = false;
	}

	function setLayerVisible(layerId, visible) {
		if (map.getLayer(layerId)) {
			map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none');
		}
	}

	// --- map filters (year range + confidence debug) -------------------------------
	function applyFilters() {
		if (!map) return;
		// keep points within the year range whose geocode confidence clears the threshold;
		// points with no confidence (subway/borough/neighborhood) coalesce to 1 and always show
		const filter = [
			'all',
			['>=', ['coalesce', ['get', 'confidence'], 1], minConfidence],
			['>=', ['coalesce', ['get', 'pub_year'], 0], yearLo],
			['<=', ['coalesce', ['get', 'pub_year'], 9999], yearHi]
		];
		if (map.getLayer('diary-points-dot')) map.setFilter('diary-points-dot', filter);
		if (map.getLayer('diary-points-glow')) map.setFilter('diary-points-glow', filter);
		const data = viewCache[activeView];
		count = data
			? data.features.filter((f) => {
					const c = f.properties.confidence ?? 1;
					const y = f.properties.pub_year ?? 0;
					return c >= minConfidence && y >= yearLo && y <= yearHi;
				}).length
			: 0;
	}

	function initYearBounds(data) {
		if (yearInit) return;
		const years = data.features.map((f) => f.properties.pub_year).filter((y) => y != null);
		if (years.length) {
			yearFloor = yearLo = Math.min(...years);
			yearCeil = yearHi = Math.max(...years);
		}
		yearInit = true;
	}

	// ---- hover popup (stays open while the cursor is over the popup, so the body
	//      can be scrolled and the link clicked) -----------------------------------
	let currentId = null;
	let closeTimer;

	function buildPopupContent(props) {
		const wrap = document.createElement('div');
		wrap.className = 'diary-popup';

		const title = document.createElement('h3');
		title.className = 'dp-title';
		title.textContent = props.title || 'Untitled entry';
		wrap.appendChild(title);

		const meta = [props.author && `By ${props.author}`, props.pub_year]
			.filter(Boolean)
			.join(' · ');
		if (meta) {
			const p = document.createElement('p');
			p.className = 'dp-meta';
			p.textContent = meta;
			wrap.appendChild(p);
		}

		// where this dot was actually placed — the specific spot, subway line, or borough
		// the entry resolved to, depending on the active view
		if (props.place_label) {
			const place = document.createElement('p');
			place.className = 'dp-place';
			place.innerHTML =
				'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6-5.3-6-10a6 6 0 1112 0c0 4.7-6 10-6 10z" fill="none" stroke="currentColor" stroke-width="1.6" /><circle cx="12" cy="11" r="2.2" fill="currentColor" /></svg>';
			const label = document.createElement('span');
			label.textContent = props.place_label;
			place.appendChild(label);
			wrap.appendChild(place);
		}

		if (props.web_url) {
			const link = document.createElement('a');
			link.className = 'dp-link';
			link.href = props.web_url;
			link.target = '_blank';
			link.rel = 'noopener noreferrer';
			link.textContent = 'Read in The Times ›';
			wrap.appendChild(link);
		}

		const body = document.createElement('div');
		body.className = 'dp-body';
		body.textContent = bodies[props.entry_id] ?? 'Loading…';
		// keep wheel events on the body from zooming the map underneath
		body.addEventListener('wheel', (e) => e.stopPropagation(), { passive: true });
		wrap.appendChild(body);

		return wrap;
	}

	function openPopup(popup, feature) {
		cancelClose();
		popup.setLngLat(feature.geometry.coordinates).setDOMContent(buildPopupContent(feature.properties));
		if (!popup.isOpen()) popup.addTo(map);
		const node = popup.getElement();
		if (node && !node.dataset.hoverWired) {
			node.dataset.hoverWired = '1';
			node.addEventListener('mouseenter', cancelClose);
			node.addEventListener('mouseleave', scheduleClose);
		}
	}

	function scheduleClose() {
		clearTimeout(closeTimer);
		closeTimer = setTimeout(() => {
			popup?.remove();
			currentId = null;
		}, 220);
	}

	function cancelClose() {
		clearTimeout(closeTimer);
	}

	let popup;

	onMount(() => {
		map = new maplibregl.Map({
			container: mapEl,
			style: STYLE_URL,
			center: CENTER,
			zoom: ZOOM,
			attributionControl: { compact: true }
		});

		map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

		map.on('load', async () => {
			// contextual basemap layers — loaded once, shown only for their view
			const [subwayLines, boroughs] = await Promise.all([
				fetch(`${base}/data/subway_lines.geojson`).then((r) => r.json()),
				fetch(`${base}/data/boroughs.geojson`).then((r) => r.json())
			]);

			map.addSource('subway-lines', { type: 'geojson', data: subwayLines });
			map.addLayer({
				id: 'subway-lines',
				type: 'line',
				source: 'subway-lines',
				layout: { visibility: 'none', 'line-cap': 'round', 'line-join': 'round' },
				paint: {
					'line-color': '#6f6f6f',
					'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.8, 14, 2],
					'line-opacity': 0.7
				}
			});

			map.addSource('boroughs', { type: 'geojson', data: boroughs });
			map.addLayer({
				id: 'borough-outline',
				type: 'line',
				source: 'boroughs',
				layout: { visibility: 'none' },
				paint: { 'line-color': '#5a5a5a', 'line-width': 1, 'line-opacity': 0.8 }
			});

			// the diary points — white dots with a soft glow
			map.addSource('diary-points', { type: 'geojson', data: EMPTY });
			map.addLayer({
				id: 'diary-points-glow',
				type: 'circle',
				source: 'diary-points',
				paint: {
					'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 8, 14, 16, 16, 22],
					'circle-color': '#ffffff',
					'circle-opacity': 0.1,
					'circle-blur': 0.6
				}
			});
			map.addLayer({
				id: 'diary-points-dot',
				type: 'circle',
				source: 'diary-points',
				paint: {
					'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2.2, 13, 3.5, 16, 6],
					'circle-color': '#ffffff',
					'circle-opacity': 0.92,
					'circle-stroke-width': 0.75,
					'circle-stroke-color': 'rgba(0, 0, 0, 0.55)'
				}
			});

			// body text lives in its own file; fetch once and look up on hover
			fetch(`${base}/data/entries.json`)
				.then((r) => r.json())
				.then((b) => (bodies = b));

			popup = new maplibregl.Popup({
				closeButton: false,
				closeOnClick: false,
				maxWidth: '340px',
				offset: 12
			});
			popup.on('close', () => (currentId = null));

			map.on('mousemove', 'diary-points-dot', (e) => {
				map.getCanvas().style.cursor = 'pointer';
				cancelClose();
				const f = e.features[0];
				if (f.properties.entry_id === currentId) return;
				currentId = f.properties.entry_id;
				openPopup(popup, f);
			});
			map.on('mouseleave', 'diary-points-dot', () => {
				map.getCanvas().style.cursor = '';
				scheduleClose();
			});

			await showView(activeView);
		});
	});

	onDestroy(() => {
		clearTimeout(closeTimer);
		map?.remove();
	});
</script>

<div class="map-shell">
	<div class="map" bind:this={mapEl}></div>

	<aside class="panel">
		<header class="panel-header">
			<h1>An Extremely Detailed<br />Metropolitan Diary Map</h1>
			<p class="byline">By the Metro Desk</p>
			<p class="date">June 22, 2026</p>
		</header>

		<div class="views">
			{#each VIEWS as v}
				<button class="view" class:active={activeView === v.id} onclick={() => showView(v.id)}>
					{v.label}
				</button>
			{/each}
		</div>
		<p class="view-desc">{VIEW_DESC[activeView]}</p>
		<p class="view-count">{countLabel}</p>

		<div class="year">
			<label class="range-label">
				Year <span class="range-val">{yearLo}&ndash;{yearHi}</span>
			</label>
			<div class="range-pair">
				<input
					type="range"
					aria-label="Earliest year"
					min={yearFloor}
					max={yearCeil}
					step="1"
					value={yearLo}
					oninput={(e) => {
						yearLo = Math.min(+e.currentTarget.value, yearHi);
						applyFilters();
					}}
				/>
				<input
					type="range"
					aria-label="Latest year"
					min={yearFloor}
					max={yearCeil}
					step="1"
					value={yearHi}
					oninput={(e) => {
						yearHi = Math.max(+e.currentTarget.value, yearLo);
						applyFilters();
					}}
				/>
			</div>
		</div>

		<div class="debug">
			<label class="range-label" for="conf">
				Min confidence <span class="range-val">{minConfidence.toFixed(2)}</span>
			</label>
			<input
				id="conf"
				type="range"
				min="0"
				max="1"
				step="0.01"
				value={minConfidence}
				oninput={(e) => {
					minConfidence = +e.currentTarget.value;
					applyFilters();
				}}
			/>
			<p class="debug-note">Debug: hides geocoded points below this confidence.</p>
		</div>
	</aside>
</div>

<style>
	.map-shell {
		position: fixed;
		inset: 0;
		overflow: hidden;
	}

	.map {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
	}

	.panel {
		position: absolute;
		top: 16px;
		left: 16px;
		width: 330px;
		max-width: calc(100vw - 32px);
		background: var(--color-panel);
		border: 1px solid var(--color-panel-border);
		border-radius: var(--panel-radius);
		padding: var(--panel-pad);
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.5);
		z-index: 5;
	}

	.panel-header h1 {
		font-size: 1.4rem;
		line-height: 1.15;
		font-weight: 700;
		letter-spacing: -0.01em;
	}

	.byline {
		margin: 10px 0 0;
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--color-text);
	}

	.date {
		margin: 2px 0 0;
		font-size: 0.78rem;
		color: var(--color-text-muted);
	}

	.views {
		display: flex;
		gap: 6px;
		margin-top: 16px;
	}

	.view {
		flex: 1;
		padding: 8px 0;
		border: none;
		border-radius: 4px;
		background: var(--color-toggle-bg);
		color: var(--color-text-muted);
		font-family: var(--font-sans);
		font-size: 0.82rem;
		font-weight: 600;
		cursor: pointer;
		transition:
			background 0.12s ease,
			color 0.12s ease;
	}

	.view:hover {
		color: var(--color-text);
	}

	.view.active {
		background: #ffffff;
		color: #000;
	}

	.view-desc {
		margin: 10px 0 0;
		font-size: 0.78rem;
		line-height: 1.35;
		color: var(--color-text-muted);
	}

	.view-count {
		margin: 4px 0 0;
		font-size: 0.74rem;
		color: var(--color-text-dim);
	}

	.year {
		margin-top: 16px;
	}

	.debug {
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid var(--color-panel-border);
	}

	.range-label {
		display: flex;
		justify-content: space-between;
		font-size: 0.78rem;
		font-weight: 600;
		color: var(--color-text-muted);
	}

	.range-val {
		color: var(--color-text);
		font-variant-numeric: tabular-nums;
	}

	.range-pair {
		display: grid;
		gap: 2px;
		margin-top: 8px;
	}

	input[type='range'] {
		width: 100%;
		accent-color: var(--color-accent);
		cursor: pointer;
	}

	.debug input[type='range'] {
		margin-top: 8px;
	}

	.debug-note {
		margin: 6px 0 0;
		font-size: 0.72rem;
		color: var(--color-text-dim);
	}

	/* ---- Mobile: panel spans the top ---- */
	@media (max-width: 640px) {
		.panel {
			top: 0;
			left: 0;
			right: 0;
			width: 100%;
			max-width: 100%;
			border-radius: 0;
			border-left: none;
			border-right: none;
			border-top: none;
			padding: 14px;
		}

		.panel-header h1 {
			font-size: 1.15rem;
		}

		.byline,
		.date {
			display: none;
		}
	}
</style>
