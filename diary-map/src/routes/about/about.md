# An Ode to New York City

By [Austin Steinhart](https://austinsteinhart.com)

*Disclaimer: This project is not affiliated or endorsed by the New York Times*

This year marks the 50th anniversary of the Metropolitan Diary, a New York Times column that has been called [“the city’s daily poetry.”](https://www.nytimes.com/2026/06/21/nyregion/metropolitan-diary-50th-anniversary.html)

I am a recent New Yorker, but the city has enamored me. Long before I started reading the Metropolitan Diary, daily moments of humanity, both kindness and sourness, caught my attention in the city. Each day, my friends and exchange funny, tragic, and heaerwarming stories of people we either witness or experience in the streets.

Just like my experiences in New York, Metropolitan Diary stories are often tied to a specific street corner, resturatant, or neighborhoods. I envisioned these stories physically placed around the city and wanted to bring this map in my head to life. Enjoy exploring the beauty of New York City, block by block, through stories of everyday interactions.

[Map button]

This was a project that brought some interesting challenges and is a process that I think has lots of different applications.

1. Define a corpus of documents (Use NYT API to identify Metro Diary articles)
2. Extract and parse text from documents (Pull text from each article and separate out individual diary entries)
3. Use a local open weight LLM model (Determine primary location mentioned in each diary)
4. Geocode aforementioned locations (Geocode named locations, neighborhood, and subway lines)
5. Place these locations on a map 

*Disclaimer: This was a project to bring an idea to life and this should be thought of as more of a concept and exploration than high-quality data. While I learned a lot from this project, if I were to do it again, I would probably trash most of the code.*  

## 1. Determining the corpus of Metropolitan Diary articles

Thankfully, the New York Times has a [great API](https://developer.nytimes.com/apis) that gives you great metadata on every article they have ever published. However, the Metropolitan Diary has moved around locations in the paper over the years, so after a bit of exploration, I used a combination of what the NYT calls the "kicker" for newer articles (which is the keyword or category for the article, such as Opinion or Modern Love) and searching the headline for older articles. I used the Article Search API over the Archive API to let the server do the filtering. 

Some quirks of the data. For about three years from 2012 to 2015, the Metropolitan Diary moved to the City Room blog. These don't look to be available on the API, so I have not included diaries from those years. For most of the column's life, it was weekly with a few different diary entries in each article. Around 2016, they started posting each diary individually. Then went back to the weekly, multi-diary articles in 2019. 

In total, I was able to identify 2,690 Metropolitan Diary articles from 1976 to 2026 (with 2013 and 2014 missing entirely).

## 2. Extracting the diaries and parsing articles

For legal reasons, let's say I, a NYT subscriber, "copied and pasted" the text from each of these articles. I then had to parse out each individual diary for most of the articles that contained multiple. Recent years were well structured with titles in h2 tags and authors were followed by a hyphen and italicized. Earlier years are much messier, with some diaries not even having titles. There was only so much that could be done here, and I wasn't striving for perfection, so the map contains missing titles and authors.

In total, I parsed out 10,460 diary entries from the 2,690 articles. I was only able to identify authors for about two thirds of diaries (7,050) and titles for about 45% (4,648).

## 3. Determine any location mentioned in the diary

Now I needed to determine what the "main" location mentioned was in each diary. I considered named-entity recognition but realized I didn't want *every* location, just the *main* location. This felt like prime LLM territory. Although a small, cheap model like Haiku probably could have burned through this small list for a few dollars very quickly, this presented a good excuse to try out an open weight model running on my laptop (and I didn't want to give Anthropic any of my money). After some research using whichllm and a few blogs, I used LMStudio and downloaded QWEN 9.5B (which was only about 6.5GB). I then used a small Python script to connect to LMStudio and repeatedly send a prompt and each diary text, and have it return structured output. To be complete, I had it return whether any specific location, subway line, neighborhood, and borough was mentioned. 

This worked alright but is definitely an area of the project that could be improved. I was able to identify a location, neighborhood, subway, or borough in about 69% (7,213) of diaries but the quality varies. If you want to do this with a more exact methodology, I would highly recommend Ben Welsh's [First LLM Classifier](https://palewi.re/docs/first-llm-classifier/index.html). 

## 4. Geocode the locations

For specific locations I used a few different free services to try to get a location including [Nominatim](https://nominatim.org/) from Open Street Maps, [Geosupport](https://geoservice.planning.nyc.gov) and [NYC Geosearch](https://geosearch.planninglabs.nyc/), both from the City Planning Department in NYC.

For diaries that mention subway lines, I pulled the shapefile of subway lines from NYC Open Data and randomly placed each diary somewhere along the subway line mentioned.

For diaries that mentioned a neighborhood, I downloaded a neighborhood map from a [popular Reddit post](https://www.reddit.com/r/nyc/comments/f6ybzu/nyc_neighborhood_map/), as an official map doesn't exist from the city and the Reddit map mostly aligns with my understanding of each NYC neighborhood. I then simply randomly placed any diary entry that mentioned a neighborhood or borough inside that area with as much specificity as I was able to. Between specific locations, placing dots on subways and on neighborhoods, I ended up with 6,533 diaries that I could map. 
 
Geocoding is another area that could definitely use some improvement. I manually made some adjustments to clean up some weird issues and you will likely find some weird/incorrect placements on the map. 

## 5. Placing the diaries on the map

I enlisted my trusted web dev stack for the website and map. The website uses [Svelte](https://svelte.dev/) as a frontend framework and [Maplibre](https://maplibre.org/), an open source JS mapping library.

For the basemap, I used [Stamen's lovely watercolor basemap](https://maps.stamen.com/watercolor/) and the illustrations at the start come from the articles. I believe Agnes Lee is the longtime illustrator for this column.

This is an open source project. [Check out most of the code](https://github.com/asteinhart?tab=repositories), including the data work and the web app. 

Have any feedback, improvements, or new ideas? Send me a message at asteinhart3 at gmail.com!