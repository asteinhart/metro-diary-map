<script>
	import { onMount, onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import STYLE_URL from './toner_style.json';

	// Manhattan-ish center
	const CENTER = [-73.97, 40.758];
	const ZOOM = 11.5;

	// Placeholder neighborhood filters — colors map to the pill tokens in global.css
	const filters = [
		{ name: 'Upper West Side', color: 'var(--pill-green)' },
		{ name: 'East Village', color: 'var(--pill-yellow)' },
		{ name: 'Harlem', color: 'var(--pill-orange)' },
		{ name: 'SoHo', color: 'var(--pill-blue)' },
		{ name: 'Chelsea', color: 'var(--pill-green)' }
	];

	// Placeholder diary points across Manhattan
	const points = {
		type: 'FeatureCollection',
		features: [
			{ coords: [-73.9855, 40.758], title: 'Times Square' },
			{ coords: [-73.9665, 40.7812], title: 'Central Park' },
			{ coords: [-74.0027, 40.7336], title: 'Greenwich Village' },
			{ coords: [-73.9626, 40.7736], title: 'Upper East Side' },
			{ coords: [-74.0089, 40.7075], title: 'Financial District' },
			{ coords: [-73.9465, 40.8116], title: 'Harlem' }
		].map((p) => ({
			type: 'Feature',
			geometry: { type: 'Point', coordinates: p.coords },
			properties: { title: p.title }
		}))
	};

	let mapEl;
	let map;
	let activeFilter = $state(null);
	let activeView = $state('Blocks');

	onMount(() => {
		map = new maplibregl.Map({
			container: mapEl,
			style: STYLE_URL,
			center: CENTER,
			zoom: ZOOM,
			attributionControl: { compact: true }
		});

		map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

		map.on('load', () => {
			map.addSource('diary-points', { type: 'geojson', data: points });

			map.addLayer({
				id: 'diary-points-glow',
				type: 'circle',
				source: 'diary-points',
				paint: {
					'circle-radius': 12,
					'circle-color': '#f5c518',
					'circle-opacity': 0.18
				}
			});

			map.addLayer({
				id: 'diary-points-dot',
				type: 'circle',
				source: 'diary-points',
				paint: {
					'circle-radius': 5,
					'circle-color': '#f5c518',
					'circle-stroke-width': 1.5,
					'circle-stroke-color': '#0b0b0b'
				}
			});

			const popup = new maplibregl.Popup({
				closeButton: false,
				closeOnClick: false,
				offset: 10
			});

			map.on('mouseenter', 'diary-points-dot', (e) => {
				map.getCanvas().style.cursor = 'pointer';
				const f = e.features[0];
				popup
					.setLngLat(f.geometry.coordinates)
					.setHTML(`<strong>${f.properties.title}</strong>`)
					.addTo(map);
			});
			map.on('mouseleave', 'diary-points-dot', () => {
				map.getCanvas().style.cursor = '';
				popup.remove();
			});
		});
	});

	onDestroy(() => {
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

		<div class="search">
			<svg viewBox="0 0 24 24" class="search-icon" aria-hidden="true">
				<path
					d="M21 21l-4.3-4.3M11 19a8 8 0 110-16 8 8 0 010 16z"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
				/>
			</svg>
			<input type="text" placeholder="Find a neighborhood or address" />
		</div>

		<div class="filters">
			{#each filters as f}
				<button
					class="pill"
					class:active={activeFilter === f.name}
					style="--pill-bg: {f.color}"
					onclick={() => (activeFilter = activeFilter === f.name ? null : f.name)}
				>
					{f.name}
				</button>
			{/each}
		</div>

		<div class="toggles">
			{#each ['Blocks', 'Borders'] as v}
				<button class="toggle" class:active={activeView === v} onclick={() => (activeView = v)}>
					{v}
				</button>
			{/each}
		</div>
		<div class="toggles">
			{#each ['Stats', 'Comments'] as v}
				<button class="toggle" class:active={activeView === v} onclick={() => (activeView = v)}>
					{v}
				</button>
			{/each}
		</div>

		<a class="add-link" href="#add">Add your neighborhood &rsaquo;</a>
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

	.search {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 16px;
		padding: 9px 11px;
		background: var(--color-input-bg);
		border: 1px solid var(--color-panel-border);
		border-radius: 4px;
	}

	.search-icon {
		width: 16px;
		height: 16px;
		flex: none;
		color: var(--color-text-dim);
	}

	.search input {
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		outline: none;
		color: var(--color-text);
		font-family: var(--font-sans);
		font-size: 0.85rem;
	}

	.search input::placeholder {
		color: var(--color-text-dim);
	}

	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 12px;
	}

	.pill {
		border: none;
		border-radius: 3px;
		padding: 4px 9px;
		font-family: var(--font-sans);
		font-size: 0.76rem;
		font-weight: 600;
		color: #fff;
		background: var(--pill-bg, var(--pill-green));
		cursor: pointer;
		opacity: 0.85;
		transition:
			opacity 0.12s ease,
			box-shadow 0.12s ease;
	}

	.pill:hover {
		opacity: 1;
	}

	.pill.active {
		opacity: 1;
		box-shadow: 0 0 0 2px var(--color-text);
	}

	.toggles {
		display: flex;
		gap: 6px;
		margin-top: 8px;
	}

	.toggles:first-of-type {
		margin-top: 16px;
	}

	.toggle {
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

	.toggle:hover {
		color: var(--color-text);
	}

	.toggle.active {
		background: #ffffff;
		color: #000;
	}

	.add-link {
		display: block;
		margin-top: 16px;
		font-size: 0.8rem;
		color: var(--color-text-muted);
		text-decoration: none;
	}

	.add-link:hover {
		color: var(--color-text);
	}

	/* ---- Mobile: panel spans the top, filters scroll horizontally ---- */
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

		.filters {
			flex-wrap: nowrap;
			overflow-x: auto;
			-webkit-overflow-scrolling: touch;
			scrollbar-width: none;
		}

		.filters::-webkit-scrollbar {
			display: none;
		}

		.pill {
			flex: none;
			white-space: nowrap;
		}
	}
</style>
