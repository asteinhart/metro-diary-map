# An Ode to New York City

*Disclaimer: This project is not affiliated or endorsed by the New York Times*

This year marks the 50th anniversary of the Metropolitan Diary, a New York Times column that has been called the [“the city’s daily poetry."](https://www.nytimes.com/2026/06/21/nyregion/metropolitan-diary-50th-anniversary.html)

I am a recent New Yorker, but the city has enamored me. Long before I started reading the Metropolitan Diary, daily moments of humanity, both kindness and sourness, caught my attention in the city. Strangers helping each other out (more specific here) were common stories to share with your friends on the commute to meeting up.

These entries often name specific locations, neighborhoods or intersections in the city. I envisioned these stories physically placed around the city as each day I witnessed similar encounters on the streets the ones I read about. So I made a map to place as many of hte 50 year history of the Metropolitan Diary at the spot or neighborhood it took place. Enjoy exploring the beauty of New York City, block by block.

Map button

This was a project that brought some interesting challenges and is a process that I think could have lots of different applications.

1. Define a corpus of documents (Use NYT API to identity Metro Diary articles)
2. Extract and parse text from documents (Pull text from each article and separate out individual diary entries)
3. Use a local open weight LLM model to (Determine primary location mentioned in each diary)
4. Geocode aforementioned locations (Geocode named locations, neighborhood, and subway lines)
5. Place these locations on a map 

*Disclaimer: This was a project to bring an idea to life and this should be thought of as more of a concept and exploration than a high quality data. While I learned a lot from this project, if I were to do it again, I would probably trash most of the code.*  

## 1. Determining the corpus of Metropolitan Diary articles

Thankfully, the New York Times has a [great API](https://developer.nytimes.com/apis) that gives you great metadata on every article they have every published. However, the Metropolitan Diary has moved around locations in the paper over the years so after a bit of exploration, a combination of what the NYT calls "kicker" for newer articles (which is the keyword or category for the article sich as Opinion or Modern Love) and searching the headeline for older articles. I used the Article Search API over the Archive API to let the server do the filtering. 

Some quirks of the data. For about three years from 2012 to 2015, the Metropolitan Diary moved to the City Room blog. These dent look to be available on the API so I have not included diaries from those years. For most of the columns life, it was weekly with a few different diary entries in each article. Around 2016, they started posting each diary individually. Then went back to the weekly, multi-diary articles in 2019. 

In total, I was able to identify 2,690 Metropolitan Diary articles from 1976 to 2026 ( with 2013 and 2014 missing entirely).

## 2. Extacting the diaries and parsing articles

For legal reasons, lets say I, a NYT subscriber, "copy and pasted" the text from each of these articles. I then had to parse out each individual diary for most of the articles that contained multiple. Recent years were well structured with titles in h2 tags and authors were followed by a hyphen and italicized. Earlier years are much more messy with some diaries not even having titles. There was only so much that could be done here and I wasnt striving for perfecting so the map contains missing titles and authors.

In total I parsed out 10,460 diary entries from the 2,690 article. I was only able to identity authors from about two third of diaries (7,050) get titles from about 45% (4,648).

## 3. Determine any location mentioned in the diary

Now I needed to determine what the "main" location mentioned was in each diary. I considered a named-entity recognition but realized I didnt want *every* location but ideally just the *main* location. Rhis felt like prime LLM territory. Although a small cheap model like Haiku probably could have burned through this small list for a few dollars very quickly, this presented a good excuse try out an open weight model running on my laptop (and I didnt want to give Anthropic any of my money). After some research using whichllm and a few blogs, I used LMStudio and downloaded QWEN 9.5B (which was only about 6.5Gb). I then used a small python script to connect to LMStudio and repeatedly send a prompt and each diary text and have it return structed output. To be complete, I had it return if any specific location, a subway line, neighborhood, and borough was mentioned. 

This worked alright but is definitely an area of the project that could be improved. If you want to do this in a exact methodology, I would highly recommend Ben Welsh's [First LLM Classifier](https://palewi.re/docs/first-llm-classifier/index.html) 

## 4. Geocode the locations

For specific locations I used a few different free services to try to get a location including [Nominatim](https://nominatim.org/) from Open Street Maps, [Geosupport](https://geoservice.planning.nyc.gov) and [NYC Geosearch](https://geosearch.planninglabs.nyc/), both from the City Planning Department in NYC.

For diaries that mention subway lines, I pulled the shape file of subway line from NYC Open Data and randomly placed each diary somewhere along the subway line mentioned.

For diaries that mentioned a neighborhood, I downloaded neighborhood map from a [popular Reddit post](https://www.reddit.com/r/nyc/comments/f6ybzu/nyc_neighborhood_map/) as an official map doesn't exist from the city and the Reddit map mostly aligns with my understanding of each NYC neighborhood. I then simply randomly placed any diary entry that mentioned a neighborhood or borough inside that area with as much specifically as I was able to. 
 
Geocoding is another area that could definitely use some improvement. I manually made some adjustments to clean up some weird issues and you will likely find some weird/incorrect placements on the map. 

5. Placing the diaries on the map

I enlisted my trusted web dev stack for the website and map. The website uses [Svelte](https://svelte.dev/) as a frontend framework and [Maplibre](https://maplibre.org/), an open source JS mapping library.

For the basemap, I used [Stamen's lovely watercolor basemap](https://maps.stamen.com/watercolor/) and the illustrations at the start come from the the articles. I believe Agnes Lee is the longtime illustrator for this column.

This is an open source project. [Check out most of the code](https://github.com/asteinhart?tab=repositories) include the data work and the web app. 

Have any feedback, improvements, or new ideas? Send me a message at asteinhart3 at gmail.com!