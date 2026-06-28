<script>
	import { onMount } from 'svelte';
	import { base } from '$app/paths';

	// onLeaveStart fires the instant the visitor clicks, so the parent can begin
	// cross-fading the map chrome in; onEnter fires once the fade-out has played,
	// so the parent can unmount this overlay.
	let { onEnter, onLeaveStart } = $props();

	// the same copy the nav panel carries, so the intro and the map agree
	const TITLE = 'Stories of New York City, Mapped';
	const SUBTITLE = "As told by the New York Times' Metropolitan Diary column from 1976 to 2026";

	let tiles = $state([]); // the scattered thumbnails
	let leaving = $state(false); // true while the fade-out is running

	// Fisher–Yates — pick a different slice of the corpus on every visit
	function shuffle(arr) {
		const a = arr.slice();
		for (let i = a.length - 1; i > 0; i--) {
			const j = Math.floor(Math.random() * (i + 1));
			[a[i], a[j]] = [a[j], a[i]];
		}
		return a;
	}

	// The modern Metropolitan Diary (2019 on) is consistently the charcoal/ink
	// illustration; earlier years mix in actual photos and cruder scans. The filename
	// can't tell them apart, but the publish year — present in every NYT image URL
	// (/images/YYYY/…) — can, so the collage keeps illustrations only. Lower this to
	// pull in the older art too (and the occasional 2008–2012 photo with it).
	const ILLUSTRATION_FROM = 2019;
	const urlYear = (u) => {
		const m = u.match(/\/images\/(\d{4})\//);
		return m ? +m[1] : 0;
	};

	// 2,515 thumbs is far more than fits — figure out how many it takes to
	// blanket *this* viewport (with overlap, so there are no gaps) and stop there
	function targetCount(w, h) {
		const avgEdge = 62; // ≈ average displayed thumb edge, px
		const density = 1.6; // > 1 so tiles overlap rather than tile cleanly
		return Math.ceil(((w * h) / (avgEdge * avgEdge)) * density);
	}

	function buildScatter(urls) {
		const w = window.innerWidth;
		const h = window.innerHeight;
		const n = Math.min(targetCount(w, h), urls.length);
		return shuffle(urls)
			.slice(0, n)
			.map((url) => ({
				url,
				// position as a % of the viewport so a resize doesn't relayout
				top: Math.random() * 100,
				left: Math.random() * 100,
				size: 42 + Math.random() * 56, // px edge
				rot: -12 + Math.random() * 24, // deg
				delay: Math.random() * 2.2, // s — staggers the fade-in
				shown: false // flipped true on load, which fades the tile in
			}));
	}

	// reveal must run through reactive state, not classList.add — a class only ever
	// set in JS gets pruned by the compiler as "unused", killing the fade. The onload
	// handler covers normal loads; this action covers thumbs already cached (and thus
	// `complete`) before their load event could fire.
	function reveal(node, t) {
		if (node.complete && node.naturalWidth > 0) t.shown = true;
	}

	onMount(async () => {
		try {
			const map = await fetch(`${base}/data/thumbnails.json`).then((r) => r.json());
			const urls = Object.values(map).filter((u) => urlYear(u) >= ILLUSTRATION_FROM);
			tiles = buildScatter(urls);
		} catch {
			tiles = []; // no thumbs is fine — the title box still stands alone
		}
	});

	function enter() {
		if (leaving) return;
		leaving = true;
		// fade the map chrome in as the overlay fades out (a cross-dissolve), then
		// unmount the overlay once it's gone
		onLeaveStart?.();
		setTimeout(() => onEnter?.(), 400);
	}
</script>

<!-- click anywhere enters the map; the explicit button carries the keyboard path -->
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="intro" class:leaving aria-hidden={leaving} onclick={enter}>
	<div class="scatter">
		{#each tiles as t (t.url + t.top + t.left)}
			<img
				class="tile"
				class:shown={t.shown}
				src={t.url}
				alt=""
				loading="eager"
				decoding="async"
				style="top:{t.top}%; left:{t.left}%; width:{t.size}px; height:{t.size}px; --rot:{t.rot}deg; transition-delay:{t.delay}s;"
				use:reveal={t}
				onload={() => (t.shown = true)}
			/>
		{/each}
	</div>

	<div class="title-box">
		<h1>{TITLE}</h1>
		<p class="subtitle">{SUBTITLE}</p>
		<!-- the byline link navigates away, so it must not also trigger "enter" -->
		<p class="byline">
			By <a href="https://austinsteinhart.com" onclick={(e) => e.stopPropagation()}
				>Austin Steinhart</a
			>
		</p>
		<button class="enter" type="button" onclick={enter}> Enter the map → </button>
	</div>
</div>

<style>
	.intro {
		position: fixed;
		inset: 0;
		z-index: 50;
		overflow: hidden;
		/* translucent paper so the b/w watercolor map stays faintly visible behind */
		background: rgba(245, 243, 238, 0.62);
		backdrop-filter: blur(2px);
		-webkit-backdrop-filter: blur(2px);
		opacity: 1;
		cursor: pointer;
		transition: opacity 0.4s ease;
	}

	/* the click-to-enter fade: everything dissolves to reveal the map */
	.intro.leaving {
		opacity: 0;
		pointer-events: none;
	}

	.scatter {
		position: absolute;
		inset: 0;
	}

	/* each thumbnail starts invisible + slightly small, then eases in once it
	   loads; the per-tile transition-delay (set inline) staggers the wave */
	.tile {
		position: absolute;
		object-fit: cover;
		border-radius: 2px;
		box-shadow: 0 3px 12px rgba(0, 0, 0, 0.18);
		opacity: 0;
		transform: translate(-50%, -50%) rotate(var(--rot)) scale(0.9);
		transition:
			opacity 0.9s ease,
			transform 0.9s ease;
		pointer-events: none;
		user-select: none;
	}

	.tile.shown {
		opacity: 1;
		transform: translate(-50%, -50%) rotate(var(--rot)) scale(1);
	}

	/* center title card — white with the same slight black stroke as the nav */
	.title-box {
		position: absolute;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		width: min(440px, calc(100vw - 32px));
		background: #ffffff;
		color: #111111;
		border: 1px solid rgba(0, 0, 0, 0.5);
		border-radius: var(--panel-radius);
		padding: 28px 26px;
		box-shadow: 0 12px 40px rgba(0, 0, 0, 0.22);
		text-align: center;
		z-index: 2;
	}

	.title-box h1 {
		font-size: 1.7rem;
		line-height: 1.15;
		font-weight: 700;
		letter-spacing: -0.01em;
	}

	.subtitle {
		margin: 12px 0 0;
		font-size: 0.9rem;
		line-height: 1.4;
		font-weight: 600;
		color: #111111;
	}

	.byline {
		margin: 8px 0 0;
		font-size: 0.8rem;
		color: #555555;
	}

	.enter {
		margin-top: 22px;
		padding: 11px 22px;
		border: 1px solid #111111;
		border-radius: 4px;
		background: #111111;
		color: #ffffff;
		font-family: var(--font-sans);
		font-size: 0.85rem;
		font-weight: 600;
		cursor: pointer;
		transition:
			background 0.15s ease,
			color 0.15s ease;
	}

	.enter:hover {
		background: #ffffff;
		color: #111111;
	}

	@media (max-width: 640px) {
		.title-box {
			padding: 22px 20px;
		}
		.title-box h1 {
			font-size: 1.35rem;
		}
	}

	/* respect reduced-motion: drop the slow fades, keep the reveal instant */
	@media (prefers-reduced-motion: reduce) {
		.intro,
		.tile {
			transition: none;
		}
	}
</style>
