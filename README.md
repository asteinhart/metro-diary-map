# Metro Diary Map

An interactive map of 50 years of the New York Times' [Metropolitan Diary](https://www.nytimes.com/column/metropolitan-diary) column, placing everyday New York stories on the street corners, restaurants, subway lines, and neighborhoods where they happened. Articles were identified via the NYT API, parsed into ~10,000 individual diary entries, run through a locally-hosted open-weight LLM to extract the main location mentioned, geocoded, and plotted. Read the full write-up on the [About page](diary-map/src/routes/about/about.md).

## Tech

- [Svelte](https://svelte.dev/) / SvelteKit — frontend
- [MapLibre GL](https://maplibre.org/) — mapping
- [Stamen Watercolor](https://maps.stamen.com/watercolor/) — basemap
- NYT Article Search API — corpus
- LM Studio + Qwen (open-weight LLM) — location extraction
- [Nominatim](https://nominatim.org/), [NYC Geosearch](https://geosearch.planninglabs.nyc/), NYC Open Data — geocoding & shapefiles

## Possible Improvements

- **Better entity matching** — more accurate extraction of the main location from each diary entry
- **Better geocoding** — cleaner, more precise placement of locations on the map
- **Save favorites** — let visitors save diary entries using local session storage
