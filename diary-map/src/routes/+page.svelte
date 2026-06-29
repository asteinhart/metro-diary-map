<script>
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import STYLE_URL from '../../static/watercolor-bw.json';
	import IntroScreen from '$lib/components/IntroScreen.svelte';

	// the collage intro overlays the map until the visitor clicks "Enter the map";
	// the map still mounts underneath while it's up, so it's ready on hand-off
	let showIntro = $state(true);
	// the chrome (panel, footer, nav) stays hidden under the intro and cross-fades
	// in the instant the visitor clicks — concurrently with the intro dissolving
	let chromeRevealed = $state(false);

	// --- SEO / social-share metadata --------------------------------------------
	// og:image and twitter:image must be absolute URLs for scrapers to resolve them,
	// so these are pinned to the deployed GitHub Pages origin rather than `base`.
	const SITE_URL = 'https://austinsteinhart.com/metro-diary-map';
	const PAGE_TITLE = 'Stories of New York City, Mapped';
	const PAGE_DESC =
		"An interactive map of the New York Times' Metropolitan Diary — reader stories of everyday life in New York City, placed where they happened, 1976–2026.";
	const SHARE_IMAGE = `${SITE_URL}/preview.png`;
	const SHARE_IMAGE_ALT = 'Charcoal sketch of New Yorkers waiting on a subway platform.';

	// Manhattan-ish center
	const CENTER = [-73.97, 40.758];
	const ZOOM = 11.5;

	// keep the view locked over NYC — panning can't wander past these bounds
	// [[west, south], [east, north]]
	const NYC_BOUNDS = [
		[-74.3, 40.47],
		[-73.65, 40.95]
	];

	// One base layer — areas.geojson places every entry at the most specific spot it resolved
	// to, and each feature's `placement` records the tier. The filter buttons narrow that set
	// to a placement category. "On the Subway" is the exception: it swaps to subway.geojson,
	// where subway-mentioning entries are dropped onto the line they named.
	const AREAS_FILE = `${base}/data/areas.geojson`;
	const SUBWAY_FILE = `${base}/data/subway.geojson`;

	// placement-tier tests, run against each feature's `placement` string
	const IS_PLACE = (p) => p === 'specific' || p.startsWith('area:');
	const IS_NEIGHBORHOOD = (p) => p === 'neighborhood' || p === 'borough';

	const VIEWS = [
		{ id: 'all', label: 'All', file: AREAS_FILE, match: null, subway: false },
		{ id: 'place', label: 'A specific location', file: AREAS_FILE, match: IS_PLACE, subway: false },
		{ id: 'subway', label: 'On the<br>subway', file: SUBWAY_FILE, match: null, subway: true },
		{
			id: 'neighborhood',
			label: 'In a neighborhood',
			file: AREAS_FILE,
			match: IS_NEIGHBORHOOD,
			subway: false
		}
	];

	const VIEW_DESC = {
		all: '',
		place: '',
		subway: '',
		neighborhood: ''
	};

	// "All" sits full-width on its own row above; the category filters share a row below
	const ALL_VIEW = VIEWS.find((v) => v.id === 'all');
	const CATEGORY_VIEWS = VIEWS.filter((v) => v.id !== 'all');

	const EMPTY = { type: 'FeatureCollection', features: [] };

	// dot styling shared between the layer paint and the click-to-select logic
	const SELECTED = ['boolean', ['feature-state', 'selected'], false];
	const DOT_OPACITY_IDLE = 0.92; // every dot equally visible when nothing is picked
	const DOT_OPACITY_FOCUS = ['case', SELECTED, 1, 0.3]; // others fade so the pick pops
	// clicking a dot eases to at least this zoom (only ever zooms in, never out)
	const SELECT_ZOOM = 14;

	let mapEl;
	let map;
	// mobile layout refs — the top header/filter bar and the bottom read-more bar,
	// measured so the map can be sized to the gap between them; the sheet hosts the
	// tapped entry as a bottom panel on small screens
	let panelEl;
	let footerEl;
	let sheetEl;
	let sheetOpen = $state(false);
	let insetObserver;
	let syncInsets;
	const isMobile = () => window.matchMedia('(max-width: 640px)').matches;
	let activeView = $state('all');
	let loading = $state(true);
	let count = $state(0);
	// total entries across the whole corpus (the "All" set); the count is shown as a fraction of it
	let grandTotal = $state(0);
	// debug control: hide geocoded dots below this confidence (null confidence always shows)
	const debug = false;
	let minConfidence = $state(0);
	// year range — bounds are learned from the data the first time a view loads
	let yearFloor = $state(1976);
	let yearCeil = $state(2026);
	let yearLo = $state(1976);
	let yearHi = $state(2026);
	let yearInit = false;

	// handle positions as a percentage of the track, for the filled segment between them
	const yearSpan = $derived(Math.max(1, yearCeil - yearFloor));
	const yearLoPct = $derived(((yearLo - yearFloor) / yearSpan) * 100);
	const yearHiPct = $derived(((yearHi - yearFloor) / yearSpan) * 100);

	const countLabel = $derived(
		loading
			? 'Loading…'
			: count >= grandTotal
				? `${grandTotal.toLocaleString()} entries`
				: `${count.toLocaleString()} of ${grandTotal.toLocaleString()} entries`
	);

	// cache loaded source files so toggling a view (or re-hovering) never re-fetches
	const fileCache = {}; // file url -> FeatureCollection
	let bodies = {}; // entry_id -> body text (loaded once, looked up on hover)
	// the features currently on the map: the source file, narrowed to the active view's tier
	let displayed = EMPTY;

	async function loadFile(file) {
		if (!fileCache[file]) {
			fileCache[file] = await fetch(file).then((r) => r.json());
		}
		return fileCache[file];
	}

	let loadToken = 0;
	async function showView(id) {
		activeView = id;
		dismissEntry(); // close any open entry; setData below clears its feature-state
		loading = true;
		const token = ++loadToken;
		const view = VIEWS.find((v) => v.id === id);
		const data = await loadFile(view.file);
		if (token !== loadToken) return; // a newer click superseded this one
		// "All" shows the whole file; the category views keep only their placement tier
		displayed = view.match
			? {
					type: 'FeatureCollection',
					features: data.features.filter((f) => view.match(f.properties.placement))
				}
			: data;
		map.getSource('diary-points')?.setData(displayed);
		setLayerVisible('subway-lines', view.subway);
		// the corpus total is the full "All" set (areas.geojson), regardless of the active filter
		if (view.file === AREAS_FILE) grandTotal = data.features.length;
		initYearBounds(data); // bounds come from the full source so they cover every tier
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
		count = displayed.features.filter((f) => {
			const c = f.properties.confidence ?? 1;
			const y = f.properties.pub_year ?? 0;
			return c >= minConfidence && y >= yearLo && y <= yearHi;
		}).length;
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

	// ---- popup: hover shows a transient preview; a click pins it open (centering
	//      the dot so the popup has room) until dismissed with the close X --------
	let currentId = null;
	let pinned = false;
	let closeTimer;

	// NYC names/acronyms that should keep their exact casing rather than be title-cased
	const TITLE_ACRONYMS = [
		'MTA',
		'NYC',
		'NY',
		'NJ',
		'NYPD',
		'FDNY',
		'EMS',
		'LIRR',
		'JFK',
		'LGA',
		'NYU',
		'CUNY',
		'PATH',
		'MoMA',
		'SoHo',
		'NoHo',
		'NoMad',
		'DUMBO',
		'FiDi',
		'TriBeCa'
	];
	const ACRONYM_MAP = Object.fromEntries(TITLE_ACRONYMS.map((a) => [a.toLowerCase(), a]));

	// title case — lowercase first (so all-caps titles get normalized) then capitalize
	// each word's leading letter; digit-led tokens (e.g. "5th") are left alone, and known
	// NYC acronyms (incl. possessives like "MTA's") are restored to their exact casing
	function toTitleCase(str) {
		const titled = str
			.toLowerCase()
			.split(/(\s+)/)
			.map((w) => (/^\d/.test(w) ? w : w.replace(/[a-z]/, (c) => c.toUpperCase())))
			.join('');
		return titled.replace(/[A-Za-z]+(?:'s)?/g, (word) => {
			const base = word.replace(/'s$/, '');
			const canon = ACRONYM_MAP[base.toLowerCase()];
			return canon ? canon + word.slice(base.length) : word;
		});
	}

	// header — title + the entry's facts (author · year · location); shared by the
	// hover preview and the full pinned popup
	function buildHeader(props) {
		const header = document.createElement('div');
		header.className = 'dp-header';

		const title = document.createElement('h3');
		title.className = 'dp-title';
		title.textContent = toTitleCase(props.title || 'Untitled diary');
		header.appendChild(title);

		// author · year on one line
		const metaText = [props.author && `By ${props.author}`, props.pub_year]
			.filter(Boolean)
			.join(' · ');
		if (metaText) {
			const p = document.createElement('p');
			p.className = 'dp-meta';
			p.textContent = metaText;
			header.appendChild(p);
		}

		// location on its own line below — where this dot was actually dropped (the
		// specific spot, subway line, or borough it resolved to)
		if (props.place_label) {
			const place = document.createElement('p');
			place.className = 'dp-place';
			place.innerHTML =
				'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6-5.3-6-10a6 6 0 1112 0c0 4.7-6 10-6 10z" fill="none" stroke="currentColor" stroke-width="1.6" /><circle cx="12" cy="11" r="2.2" fill="currentColor" /></svg>';
			const label = document.createElement('span');
			label.textContent = props.place_label;
			place.appendChild(label);
			header.appendChild(place);
		}

		return header;
	}

	function buildPopupContent(props, preview = false) {
		const wrap = document.createElement('div');
		wrap.className = 'diary-popup';

		// both states render the full entry; they differ only in the corner affordance —
		// hover gets a "click to read" prompt (and auto-closes), a pinned popup gets the X
		if (preview) {
			wrap.classList.add('dp-preview');
			const hint = document.createElement('p');
			hint.className = 'dp-hint';
			hint.textContent = 'Click to read →';
			wrap.appendChild(hint);
		} else {
			const close = document.createElement('button');
			close.type = 'button';
			close.className = 'dp-close';
			close.setAttribute('aria-label', 'Close');
			close.innerHTML =
				'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" /></svg>';
			close.addEventListener('click', dismissEntry);
			wrap.appendChild(close);
		}

		wrap.appendChild(buildHeader(props));

		// hairline divider between the header and the story body
		const rule = document.createElement('hr');
		rule.className = 'dp-rule';
		wrap.appendChild(rule);

		const body = document.createElement('div');
		body.className = 'dp-body';
		body.textContent = bodies[props.entry_id] ?? 'Loading…';
		// keep wheel events on the body from zooming the map underneath
		body.addEventListener('wheel', (e) => e.stopPropagation(), { passive: true });
		wrap.appendChild(body);

		if (props.web_url) {
			const link = document.createElement('a');
			link.className = 'dp-link';
			link.href = props.web_url;
			link.target = '_blank';
			link.rel = 'noopener noreferrer';
			link.textContent = 'Read in the New York Times ›';
			// let the link open NYT without the preview's click-to-pin also firing
			link.addEventListener('click', (e) => e.stopPropagation());
			wrap.appendChild(link);
		}

		return wrap;
	}

	function openPopup(popup, feature, preview = false) {
		cancelClose();
		popup
			.setLngLat(feature.geometry.coordinates)
			.setDOMContent(buildPopupContent(feature.properties, preview));
		if (!popup.isOpen()) popup.addTo(map);
		// let the cursor move onto the popup (to scroll/read/click) without it closing
		const node = popup.getElement();
		if (node && !node.dataset.hoverWired) {
			node.dataset.hoverWired = '1';
			node.addEventListener('mouseenter', cancelClose);
			node.addEventListener('mouseleave', scheduleClose);
		}
		// clicking the hover preview opens the full entry, same as clicking the dot
		if (node) node.onclick = preview ? () => pinFeature(feature) : null;
	}

	// highlight the clicked dot via feature-state and dim the rest so it pops
	let selectedId = null;
	function selectFeature(id) {
		clearSelected();
		selectedId = id;
		map.setFeatureState({ source: 'diary-points', id }, { selected: true });
		map.setPaintProperty('diary-points-dot', 'circle-opacity', DOT_OPACITY_FOCUS);
	}

	function clearSelected() {
		if (selectedId != null && map.getSource('diary-points')) {
			map.setFeatureState({ source: 'diary-points', id: selectedId }, { selected: false });
		}
		selectedId = null;
		if (map?.getLayer('diary-points-dot')) {
			map.setPaintProperty('diary-points-dot', 'circle-opacity', DOT_OPACITY_IDLE);
		}
	}

	// pin a feature open. On desktop: a maplibre popup eased into the lower-right
	// quadrant so it clears the top-left panel. On mobile: the entry slides up as a
	// bottom sheet and the dot is centered in the map band above it. Either way the
	// dot is highlighted and the map nudges in to SELECT_ZOOM.
	function pinFeature(feature) {
		pinned = true;
		currentId = feature.properties.entry_id;
		selectFeature(feature.id ?? feature.properties.entry_id);
		if (isMobile()) {
			openSheet(feature);
			return;
		}
		openPopup(popup, feature, false);
		const c = map.getContainer();
		map.easeTo({
			center: feature.geometry.coordinates,
			zoom: Math.max(map.getZoom(), SELECT_ZOOM),
			offset: [c.clientWidth * 0.2, c.clientHeight * 0.15],
			duration: 500
		});
	}

	// mobile: render the full entry into the bottom sheet, then center the dot
	// horizontally and lift it into the visible band between the header and the sheet
	function openSheet(feature) {
		if (!sheetEl) return;
		sheetEl.replaceChildren(buildPopupContent(feature.properties, false));
		sheetOpen = true;
		const screenH = window.innerHeight;
		const headerH = panelEl?.offsetHeight ?? 0;
		const footerH = footerEl?.offsetHeight ?? 0;
		const sheetH = sheetEl.offsetHeight;
		// the strip of map still visible above the sheet (header bottom → sheet top)
		const bandTop = headerH;
		const bandBottom = screenH - sheetH;
		// drop the dot a little above the middle of that strip so the sheet never covers it
		const targetY = bandTop + (bandBottom - bandTop) * 0.42;
		// the map container is that strip between the fixed header and footer, so the
		// easeTo offset is measured from its on-screen vertical center
		const containerCenterY = headerH + (screenH - headerH - footerH) / 2;
		map.easeTo({
			center: feature.geometry.coordinates,
			zoom: Math.max(map.getZoom(), SELECT_ZOOM),
			offset: [0, targetY - containerCenterY],
			duration: 500
		});
	}

	// shared close: tear down whichever surface is showing the pinned entry
	function dismissEntry() {
		pinned = false;
		currentId = null;
		clearSelected();
		popup?.remove();
		sheetOpen = false;
		if (sheetEl) sheetEl.replaceChildren();
	}

	// a pinned popup stays put; only an unpinned (hover) preview auto-closes
	function scheduleClose() {
		if (pinned) return;
		clearTimeout(closeTimer);
		closeTimer = setTimeout(() => popup?.remove(), 220);
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
			minZoom: 9.5,
			maxBounds: NYC_BOUNDS,
			// flat, north-up only: no rotation, no tilt
			dragRotate: false,
			pitchWithRotate: false,
			touchPitch: false,
			maxPitch: 0,
			attributionControl: { compact: true }
		});

		// belt-and-suspenders: kill the rotate gesture on touch as well
		map.touchZoomRotate.disableRotation();
		// drop the rotate/pitch keyboard shortcuts, keep pan + zoom
		map.keyboard.disableRotation?.();

		map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'bottom-right');

		// On mobile the header/filter bar and the read-more bar are fixed to the top and
		// bottom; measure them so the map exactly fills the gap (survives title wrap).
		syncInsets = () => {
			if (!panelEl || !footerEl) return;
			const shell = panelEl.parentElement;
			shell.style.setProperty('--mobile-header-h', `${panelEl.offsetHeight}px`);
			shell.style.setProperty('--mobile-footer-h', `${footerEl.offsetHeight}px`);
			map?.resize();
		};
		insetObserver = new ResizeObserver(syncInsets);
		insetObserver.observe(panelEl);
		insetObserver.observe(footerEl);
		window.addEventListener('resize', syncInsets);
		requestAnimationFrame(syncInsets);

		map.on('load', async () => {
			// contextual basemap layer — loaded once, shown only for the subway view
			const subwayLines = await fetch(`${base}/data/subway_lines.geojson`).then((r) => r.json());

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

			// the diary points — white dots with a soft glow. promoteId lifts entry_id to
			// the feature id so we can drive a "selected" feature-state when a dot is clicked
			map.addSource('diary-points', { type: 'geojson', data: EMPTY, promoteId: 'entry_id' });
			// map.addLayer({
			// 	id: 'diary-points-glow',
			// 	type: 'circle',
			// 	source: 'diary-points',
			// 	paint: {
			// 		'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 8, 14, 16, 16, 22],
			// 		'circle-color': '#000',
			// 		'circle-opacity': 0.1,
			// 		'circle-blur': 0.6
			// 	}
			// });
			map.addLayer({
				id: 'diary-points-dot',
				type: 'circle',
				source: 'diary-points',
				paint: {
					// a selected dot grows, turns copper and gets a crisp white halo.
					// only one zoom interpolate is allowed per expression, so the
					// selected/idle split lives inside each zoom stop
					'circle-radius': [
						'interpolate',
						['linear'],
						['zoom'],
						10,
						['case', SELECTED, 4.5, 2.2],
						13,
						['case', SELECTED, 6.5, 3.5],
						16,
						['case', SELECTED, 10, 6]
					],
					'circle-color': ['case', SELECTED, '#000', '#000'],
					// opacity is swapped at runtime: every dot full while idle; others fade
					// back once a dot is selected so the chosen one pops
					'circle-opacity': DOT_OPACITY_IDLE,
					'circle-stroke-width': ['case', SELECTED, 2, 0.75],
					'circle-stroke-color': ['case', SELECTED, '#ffffff', 'rgba(255, 255, 255, 0.55)']
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
			popup.on('close', () => {
				currentId = null;
				pinned = false;
			});

			// hover shows a "Click to read" preview (unless a pinned popup already owns the
			// screen). Touch devices skip the preview entirely — a tap pins straight away.
			map.on('mousemove', 'diary-points-dot', (e) => {
				map.getCanvas().style.cursor = 'pointer';
				if (pinned || isMobile()) return;
				cancelClose();
				const f = e.features[0];
				if (f.properties.entry_id === currentId) return;
				currentId = f.properties.entry_id;
				openPopup(popup, f, true);
			});
			map.on('mouseleave', 'diary-points-dot', () => {
				map.getCanvas().style.cursor = '';
				scheduleClose();
			});

			// click a dot to pin the full entry open
			map.on('click', 'diary-points-dot', (e) => pinFeature(e.features[0]));

			// mobile: tapping the map away from a dot dismisses the open bottom sheet
			map.on('click', (e) => {
				if (!isMobile() || !pinned) return;
				const hits = map.queryRenderedFeatures(e.point, { layers: ['diary-points-dot'] });
				if (!hits.length) dismissEntry();
			});

			await showView(activeView);
		});
	});

	onDestroy(() => {
		clearTimeout(closeTimer);
		insetObserver?.disconnect();
		if (syncInsets) window.removeEventListener('resize', syncInsets);
		map?.remove();
	});
</script>

<svelte:head>
	<title>{PAGE_TITLE}</title>
	<meta name="description" content={PAGE_DESC} />
	<link rel="canonical" href={SITE_URL} />
	<meta name="theme-color" content="#111111" />

	<!-- Open Graph (Facebook, LinkedIn, Slack, iMessage, …) -->
	<meta property="og:type" content="website" />
	<meta property="og:site_name" content={PAGE_TITLE} />
	<meta property="og:title" content={PAGE_TITLE} />
	<meta property="og:description" content={PAGE_DESC} />
	<meta property="og:url" content={SITE_URL} />
	<meta property="og:image" content={SHARE_IMAGE} />
	<meta property="og:image:type" content="image/jpeg" />
	<meta property="og:image:width" content="869" />
	<meta property="og:image:height" content="528" />
	<meta property="og:image:alt" content={SHARE_IMAGE_ALT} />

	<!-- Twitter / X -->
	<meta name="twitter:card" content="summary_large_image" />
	<meta name="twitter:title" content={PAGE_TITLE} />
	<meta name="twitter:description" content={PAGE_DESC} />
	<meta name="twitter:image" content={SHARE_IMAGE} />
	<meta name="twitter:image:alt" content={SHARE_IMAGE_ALT} />
</svelte:head>

{#if showIntro}
	<IntroScreen onLeaveStart={() => (chromeRevealed = true)} onEnter={() => (showIntro = false)} />
{/if}

<div class="map-shell" class:hide-chrome={!chromeRevealed}>
	<div class="map" bind:this={mapEl}></div>

	<aside class="panel" bind:this={panelEl}>
		<header class="panel-header">
			<h1>Stories of New York City, Mapped</h1>
			<p class="subtitle">
				As told by the New York Times' Metropolitan Diary column from 1976 to 2026
			</p>
			<p class="byline">By <a href="https://austinsteinhart.com"> Austin Steinhart</a></p>
		</header>

		<div class="views">
			<button
				class="view"
				class:active={activeView === ALL_VIEW.id}
				onclick={() => showView(ALL_VIEW.id)}
			>
				{ALL_VIEW.label}
			</button>
			<div class="view-row">
				{#each CATEGORY_VIEWS as v}
					<button class="view" class:active={activeView === v.id} onclick={() => showView(v.id)}>
						{@html v.label}
					</button>
				{/each}
			</div>
		</div>
		<p class="view-desc">{VIEW_DESC[activeView]}</p>
		<p class="view-count">{countLabel}</p>

		<div class="year">
			<label class="range-label">
				Year <span class="range-val">{yearLo}&ndash;{yearHi}</span>
			</label>
			<div class="range-dual">
				<div class="track"></div>
				<div class="track-fill" style="left: {yearLoPct}%; right: {100 - yearHiPct}%"></div>
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

		<div class="read-more" bind:this={footerEl}>
			<a
				href="https://austinsteinhart.com/metro-diary-map/about"
				target="_blank"
				rel="noopener noreferrer"
			>
				Read more about this project
			</a>
		</div>
		{#if debug}
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
		{/if}
	</aside>

	<!-- mobile-only: the tapped entry slides up from the bottom of the screen -->
	<div class="mobile-sheet" class:open={sheetOpen} bind:this={sheetEl}></div>
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

	/* the chrome (white panel + its contents, footer, nav) is hidden under the intro,
	   then cross-fades in as a single unit while the intro dissolves — so the whole
	   box fades in together rather than popping in blank */
	.panel,
	.map-shell :global(.maplibregl-ctrl) {
		transition: opacity 0.4s ease;
	}

	.map-shell.hide-chrome .panel,
	.map-shell.hide-chrome :global(.maplibregl-ctrl) {
		opacity: 0;
		pointer-events: none;
	}

	.panel {
		position: absolute;
		top: 16px;
		left: 16px;
		width: 330px;
		max-width: calc(100vw - 32px);
		background: #ffffff;
		color: #111111;
		border: 1px solid rgba(0, 0, 0, 0.5);
		border-radius: var(--panel-radius);
		padding: var(--panel-pad);
		box-shadow: 0 8px 30px rgba(0, 0, 0, 0.15);
		z-index: 5;
	}

	.panel-header h1 {
		font-size: 1.4rem;
		line-height: 1.15;
		font-weight: 700;
		letter-spacing: -0.01em;
	}

	.subtitle {
		margin: 10px 0 0;
		font-size: 0.8rem;
		font-weight: 600;
		color: #111111;
	}

	.byline {
		margin: 8px 0 0;
		font-size: 0.78rem;
		color: #555555;
	}

	.views {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin-top: 16px;
	}

	/* the three category filters share one row beneath the full-width "All" button */
	.view-row {
		display: flex;
		gap: 6px;
	}

	.view-row .view {
		flex: 1;
	}

	.view {
		padding: 7px 0;
		border: 1px solid rgba(0, 0, 0, 0.3);
		border-radius: 4px;
		background: #ffffff;
		color: #111111;
		font-family: var(--font-sans);
		font-size: 0.76rem;
		font-weight: 600;
		cursor: pointer;
		transition:
			background 0.12s ease,
			color 0.12s ease,
			border-color 0.12s ease;
	}

	.view:hover {
		background: #f0f0f0;
	}

	.view.active {
		background: #111111;
		border-color: #111111;
		color: #ffffff;
	}

	.view-desc {
		margin: 10px 0 0;
		font-size: 0.78rem;
		line-height: 1.35;
		color: #555555;
	}

	.view-count {
		margin: 4px 0 0;
		font-size: 0.74rem;
		color: #777777;
	}

	.year {
		margin-top: 16px;
	}

	.debug {
		margin-top: 16px;
		padding-top: 14px;
		border-top: 1px solid rgba(0, 0, 0, 0.12);
	}

	.range-label {
		display: flex;
		justify-content: space-between;
		font-size: 0.78rem;
		font-weight: 600;
		color: #333333;
	}

	.range-val {
		color: #111111;
		font-variant-numeric: tabular-nums;
	}

	/* dual-handle slider: two range inputs overlaid on a shared track */
	.range-dual {
		position: relative;
		height: 20px;
		margin-top: 10px;
	}

	.range-dual .track,
	.range-dual .track-fill {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		height: 4px;
		border-radius: 2px;
		pointer-events: none;
	}

	.range-dual .track {
		left: 0;
		right: 0;
		background: rgba(0, 0, 0, 0.15);
	}

	.range-dual .track-fill {
		background: var(--color-accent);
	}

	.range-dual input[type='range'] {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		height: 20px;
		margin: 0;
		background: none;
		pointer-events: none;
		-webkit-appearance: none;
		appearance: none;
	}

	/* only the thumbs should be interactive, so the lower input doesn't swallow clicks */
	.range-dual input[type='range']::-webkit-slider-thumb {
		-webkit-appearance: none;
		appearance: none;
		pointer-events: auto;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: #ffffff;
		border: 2px solid var(--color-accent);
		cursor: pointer;
	}

	.range-dual input[type='range']::-moz-range-thumb {
		pointer-events: auto;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: #ffffff;
		border: 2px solid var(--color-accent);
		cursor: pointer;
	}

	.range-dual input[type='range']::-moz-range-track {
		background: none;
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
		color: #777777;
	}
	.read-more {
		margin-top: 16px;
		font-size: 0.78rem;
	}

	/* the bottom sheet is a mobile-only surface */
	.mobile-sheet {
		display: none;
	}

	/* ---- Mobile: header + filters on top, map in the middle, read-more footer below;
	   a tapped entry rises as a bottom sheet ---- */
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

		.panel-header,
		.panel-header h1 {
			width: 100%;
		}

		.panel-header h1 {
			font-size: 1.3rem;
			line-height: 1.15;
		}

		.subtitle {
			margin-top: 11px;
			font-size: 0.74rem;
		}

		.byline {
			margin-top: 8px;
			font-size: 0.74rem;
		}

		/* filters sit beneath the title, with smaller buttons to save height */
		.views {
			margin-top: 16px;
		}

		.view {
			padding: 5px 0;
			font-size: 0.68rem;
		}

		/* the map exactly fills the gap between the top bar and the read-more footer */
		.map {
			inset: var(--mobile-header-h, 0) 0 var(--mobile-footer-h, 0) 0;
			width: auto;
			height: auto;
		}

		/* hidden on mobile: empty description, entry count, sliders */
		.view-desc,
		.view-count,
		.year,
		.debug {
			display: none;
		}

		/* read-more becomes the slim footer bar pinned to the bottom */
		.read-more {
			position: fixed;
			left: 0;
			right: 0;
			bottom: 0;
			margin: 0;
			padding: 9px 12px;
			padding-bottom: calc(9px + env(safe-area-inset-bottom));
			text-align: center;
			background: #ffffff;
			border-top: 1px solid rgba(0, 0, 0, 0.2);
			z-index: 5;
		}

		/* the tapped entry slides up over the footer as a bottom sheet — a flex column
		   so the header/link stay put and only the story body scrolls within it */
		.mobile-sheet {
			display: flex;
			flex-direction: column;
			position: fixed;
			left: 0;
			right: 0;
			bottom: 0;
			z-index: 7;
			background: #ffffff;
			border-top: 1px solid rgba(0, 0, 0, 0.25);
			border-radius: 14px 14px 0 0;
			box-shadow: 0 -8px 30px rgba(0, 0, 0, 0.18);
			padding: 16px;
			padding-bottom: calc(16px + env(safe-area-inset-bottom));
			max-height: 40vh;
			overflow: hidden;
			transform: translateY(110%);
			transition: transform 0.3s ease;
		}

		.mobile-sheet.open {
			transform: translateY(0);
		}
	}
</style>
