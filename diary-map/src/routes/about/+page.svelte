<script>
	import { base } from '$app/paths';

	// --- SEO / social-share metadata --------------------------------------------
	// og:image and twitter:image must be absolute URLs for scrapers to resolve them,
	// so these are pinned to the deployed GitHub Pages origin rather than `base`.
	const SITE_URL = 'https://austinsteinhart.com/metro-diary-map';
	const PAGE_URL = `${SITE_URL}/about`;
	const PAGE_TITLE = 'About — Stories of New York City, Mapped';
	const PAGE_DESC =
		"How an interactive map of the New York Times' Metropolitan Diary came together — from corpus to LLM to geocoding to the map.";
	const SHARE_IMAGE = `${SITE_URL}/share.png`;
	const SHARE_IMAGE_ALT = 'Charcoal sketch of New Yorkers waiting on a subway platform.';
</script>

<svelte:head>
	<title>{PAGE_TITLE}</title>
	<meta name="description" content={PAGE_DESC} />
	<link rel="canonical" href={PAGE_URL} />
	<meta name="theme-color" content="#111111" />

	<!-- Open Graph (Facebook, LinkedIn, Slack, iMessage, …) -->
	<meta property="og:type" content="article" />
	<meta property="og:site_name" content="Stories of New York City, Mapped" />
	<meta property="og:title" content={PAGE_TITLE} />
	<meta property="og:description" content={PAGE_DESC} />
	<meta property="og:url" content={PAGE_URL} />
	<meta property="og:image" content={SHARE_IMAGE} />
	<meta property="og:image:type" content="image/png" />
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

<main class="about">
	<article>
		<img class="header-image" src="{base}/header.png" alt="" />

		<h1>An Ode to New York City</h1>

		<p class="byline">
			By <a href="https://austinsteinhart.com" target="_blank" rel="noopener noreferrer"
				>Austin Steinhart</a
			>
		</p>

		<p class="disclaimer">
			<em>Disclaimer: This project is not affiliated or endorsed by the New York Times.</em>
		</p>

		<p>
			This year marks the 50th anniversary of the Metropolitan Diary, a New York Times column that
			has been called
			<a
				href="https://www.nytimes.com/2026/06/21/nyregion/metropolitan-diary-50th-anniversary.html"
				target="_blank"
				rel="noopener noreferrer">“the city's daily poetry.”</a
			>
		</p>

		<p>
			I am a recent New Yorker, but the city has enamored me. Long before I started reading the
			Metropolitan Diary, daily moments of humanity, both kindness and sourness, caught my attention
			in this city. Each day, my friends and I would exchange funny, tragic, and heartwarming stories of
			people we witness or experience in the streets.
		</p>

		<p>
			Just like my experiences in New York, Metropolitan Diary stories are often tied to a specific
			street corner, restaurant, or neighborhoods. I envisioned these stories physically placed
			around the city and wanted to bring this map in my head to life. Enjoy exploring the beauty of
			New York City, block by block, through stories of everyday interactions.
		</p>

		<p class="map-cta">
			<a class="map-button" href="{base}/">Explore the map →</a>
		</p>

		<p>
			This was a project that brought some interesting challenges and is a process that I think has
			lots of other applications.
		</p>

		<ol>
			<li>Define a corpus of documents (Used NYT API to identify Metro Diary articles)</li>
			<li>
				Extract and parse text from documents (Pulled text from each article and separated out
				individual diary entries)
			</li>
			<li>
				Use a local open weight LLM model (Determined primary location mentioned in each diary)
			</li>
			<li>
				Geocode aforementioned locations (Geocoded named locations, neighborhood, and subway lines)
			</li>
			<li>Place these locations on a map</li>
		</ol>

		<p class="disclaimer">
			<em
				>Disclaimer: This was a project to bring an idea to life and thus should be thought of as
				more of a concept and exploration than a high-quality data pipeline. Almost all of the code was written by an LLM, with my guidance. While I learned a lot from this project, if I were to do it again, I would probably trash most of the code.</em
			>
		</p>

		<h2>1. Determining the corpus of Metropolitan Diary articles</h2>

		<p>
			Thankfully, the New York Times has a
			<a href="https://developer.nytimes.com/apis" target="_blank" rel="noopener noreferrer"
				>great API</a
			>
			that gives you detailed metadata on every article they have ever published. However, the Metropolitan
			Diary has moved around locations in the paper over the years, so after a bit of exploration, I used
			a combination of what the NYT calls the "kicker" for newer articles (which is the keyword or category
			for the article, such as Opinion or Modern Love) and searching the headline for older articles.
			I used the Article Search API over the Archive API to let the server do the filtering.
		</p>

		<p>
			Some quirks of the data. For about three years from 2012 to 2015, the Metropolitan Diary moved
			to the City Room blog. These don't look to be available on the API, so I have not included
			diaries from those years. For most of the column's life, it was weekly with a few different
			diary entries in each article. Around 2016, they started posting each diary individually. Then
			went back to the weekly, multi-diary articles in 2019.
		</p>

		<p>
			In total, I was able to identify 2,690 Metropolitan Diary articles from 1976 to 2026 (with
			2013 and 2014 missing entirely).
		</p>

		<h2>2. Extracting the diaries and parsing articles</h2>

		<p>
			For legal reasons, let's say I, a NYT subscriber, "copied and pasted" the text from each of
			these articles. I then had to parse out each individual diary for most of the articles that
			contained multiple. Recent years were well structured and easy to parse with titles in h2 tags and authors
			followed by a hyphen and italicized. Earlier years are much messier, with some diaries not
			even having titles. There was only so much that could be done here, and I wasn't striving for
			perfection, so the map contains missing titles and authors.
		</p>

		<p>
			In total, I parsed out 10,460 diary entries from the 2,690 articles. I was only able to
			identify authors for about two thirds of diaries (7,050) and titles for about 45% (4,648).
		</p>

		<h2>3. Determine any location mentioned in the diary</h2>

		<p>
			Now I needed to determine what the "main" location mentioned was in each diary. I considered
			named-entity recognition but realized I didn't want <em>every</em> location, just the
			<em>main</em> location. This felt like prime LLM territory. Although a small, cheap model like Haiku
			probably could have burned through this small list for a few dollars very quickly, this presented
			a good excuse to try out an open weight model running on my laptop (and I didn't want to give Anthropic
			any of my money). After some research using <a href="https://github.com/Andyyyy64/whichllm" target="_blank" rel="noopener noreferrer">whichllm</a> and a few blogs, I used <a href="https://lmstudio.ai/" target="_blank" rel="noopener noreferrer">LM Studio</a> and downloaded
			QWEN 9.5B (which was only about 6GB). I then used a small Python script to connect to LM Studio
			and repeatedly send a prompt and each diary text, and have it return structured output. To be complete,
			I had it return whether any specific location, subway line, neighborhood, and borough was mentioned.
		</p>

		<p>
			This worked alright but was incredibly slow. I had a few workers going and it still took ~16 hours for the 10k entries on my MacBook with an M1 Pro chip and 16GB RAM. This is definitely an area of the project that could be improved. I was able to identify a location, neighborhood, subway, or borough in about 69% (7,213) of diaries but the quality varies. If you want to do this with a more exact methodology, I would highly
			recommend Ben Welsh's
			<a
				href="https://palewi.re/docs/first-llm-classifier/index.html"
				target="_blank"
				rel="noopener noreferrer">First LLM Classifier</a
			>.
		</p>

		<h2>4. Geocode the locations</h2>

		<p>
			For specific locations, I used a few different free services to try to get a latitude and longitude including
			<a href="https://nominatim.org/" target="_blank" rel="noopener noreferrer">Nominatim</a> from
			Open Street Maps,
			<a href="https://geoservice.planning.nyc.gov" target="_blank" rel="noopener noreferrer"
				>Geosupport</a
			>
			and
			<a href="https://geosearch.planninglabs.nyc/" target="_blank" rel="noopener noreferrer"
				>NYC Geosearch</a
			>, both from the City Planning Department in NYC.
		</p>

		<p>
			For diaries that mention subway lines, I pulled the spatial data of subway lines from NYC Open
			Data and randomly placed each diary somewhere along the subway line mentioned.
		</p>

		<p>
			For diaries that mentioned a neighborhood, I downloaded a neighborhood map from a
			<a
				href="https://www.reddit.com/r/nyc/comments/f6ybzu/nyc_neighborhood_map/"
				target="_blank"
				rel="noopener noreferrer">popular Reddit post</a
			>, as an official map doesn't exist from the city and the Reddit map mostly aligns with my
			understanding of each NYC neighborhood. I then simply randomly placed any diary entry that
			mentioned a neighborhood or borough and not a specific location inside that area with as much specificity as I was able to. Between specific locations, placing dots on subways and in neighborhoods, I ended up with
			6,533 diaries that I could map.
		</p>

		<p>
			Geocoding is another area that could definitely use some improvement. I manually made some
			adjustments to clean up some weird issues and you will likely find some weird/incorrect
			placements on the map.
		</p>

		<h2>5. Placing the diaries on the map</h2>

		<p>
			I enlisted my trusted web dev stack for the website and map. The website uses
			<a href="https://svelte.dev/" target="_blank" rel="noopener noreferrer">Svelte</a> as a
			frontend framework and
			<a href="https://maplibre.org/" target="_blank" rel="noopener noreferrer">Maplibre</a>, an
			open source JS mapping library. Its hosted for free with <a href="https://docs.github.com/en/pages" target="_blank" rel="noopener noreferrer">GitHub Pages</a>. 
		</p>

		<p>
			For the basemap, I used
			<a href="https://maps.stamen.com/watercolor/" target="_blank" rel="noopener noreferrer"
				>Stamen's lovely watercolor basemap</a
			>
			and the illustrations at the start come from the articles. I believe Agnes Lee is the longtime illustrator
			for this column.
		</p>

		<hr class="divider" />

		<p>
			This is an open source project.
			<a
				href="https://github.com/asteinhart/metro-diary-map"
				target="_blank"
				rel="noopener noreferrer">Check out most of the code</a
			>, including the data work and the web app.
		</p>

		<p>
			Have any feedback, improvements, or new ideas? Send me a message at asteinhart3 at gmail.com!
		</p>

		<p class="map-cta">
			<a class="map-button" href="{base}/">Explore the map →</a>
		</p>
	</article>
</main>

<style>
	.about {
		background: #ffffff;
		color: #111111;
		min-height: 100vh;
		padding: 10px 24px 96px;
	}

	article {
		max-width: 680px;
		margin: 0 auto;
	}

	.header-image {
		display: block;
		width: 100%;
		height: auto;
		margin: 0 0 16px;
		border-radius: 4px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
	}

	h1 {
		font-family: var(--font-serif);
		font-size: 2.45rem;
		line-height: 1.15;
		font-weight: 700;
		letter-spacing: -0.01em;
	}

	h2 {
		font-family: var(--font-serif);
		font-size: 1.55rem;
		line-height: 1.2;
		font-weight: 700;
		margin: 44px 0 0;
	}

	.byline {
		margin: 12px 0 0;
		font-size: 0.95rem;
		color: #555555;
	}

	p {
		font-family: var(--font-sans);
		font-size: 1.05rem;
		line-height: 1.65;
		margin: 18px 0 0;
	}

	.disclaimer {
		color: #555555;
		font-size: 0.95rem;
	}

	ol {
		font-family: var(--font-sans);
		font-size: 1.05rem;
		line-height: 1.6;
		margin: 18px 0 0;
		padding-left: 1.4em;
	}

	ol li {
		margin-top: 8px;
	}

	a {
		color: #111111;
		text-decoration: underline;
		text-underline-offset: 2px;
	}

	a:hover {
		color: #555555;
	}

	.divider {
		border: none;
		border-top: 1px solid #dddddd;
		margin: 44px 0 0;
	}

	/* signature button — black fill, inverts on hover, matching the intro/panel */
	.map-cta {
		margin: 28px 0 0;
		text-align: center;
	}

	.map-button {
		display: inline-block;
		padding: 11px 22px;
		border: 1px solid #111111;
		border-radius: 4px;
		background: #111111;
		color: #ffffff;
		font-family: var(--font-sans);
		font-size: 0.9rem;
		font-weight: 600;
		text-decoration: none;
		cursor: pointer;
		transition:
			background 0.15s ease,
			color 0.15s ease;
	}

	.map-button:hover {
		background: #ffffff;
		color: #111111;
	}

	@media (max-width: 640px) {
		.about {
			padding: 40px 20px 72px;
		}

		h1 {
			font-size: 1.9rem;
		}

		h2 {
			font-size: 1.4rem;
		}

		p,
		ol {
			font-size: 1rem;
		}
	}
</style>
