# --- main.py (WEEKLY GAMES REPORT ENGINE - FINAL) ---

import html
import os
import datetime
import base64
import json
import time
import re
import markdown
import argparse
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import formataddr

import google.generativeai as genai
import requests
import feedparser
from difflib import SequenceMatcher

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import secretmanager
from google.cloud import storage
import vertexai
from vertexai.vision_models import ImageGenerationModel, Image

# --- CONFIGURATION & CONSTANTS ---
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/documents"]

# --- UNIVERSAL HELPER & AUTH FUNCTIONS ---
def get_secret(secret_id, project_id, version="latest"):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        print(f"Could not fetch secret: {secret_id}. Error: {e}")
        return None

def send_email(service, subject, html_body, recipient_email):
    print("Creating and sending email...")
    try:
        profile = service.users().getProfile(userId='me').execute()
        sender_email = profile['emailAddress']
        to_addresses = {sender_email.lower()}
        if recipient_email:
            additional_recipients = recipient_email.split(',')
            for email in additional_recipients:
                clean_email = email.strip().lower()
                if clean_email: to_addresses.add(clean_email)
        message = MIMEMultipart("alternative")
        message["To"] = ", ".join(sorted(list(to_addresses)))
        message["From"] = formataddr(("Your Automated Report", sender_email))
        message["Subject"] = subject
        message.attach(MIMEText(html_body, "html"))
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent_message = service.users().messages().send(userId='me', body={'raw': raw_message}).execute()
        print(f"Successfully sent email! Message ID: {sent_message['id']}")
    except HttpError as error:
        print(f"An error occurred while sending the email: {error}")
        
def format_sources_for_email(sources_map):
    """
    Takes a dictionary of sources and formats them into a Markdown string for the email footer.
    """
    sources_section = ["## Sources Used in This Report"]
    sources_section.append("This report was synthesized from the following primary sources, categorized by the sections they primarily inform.")

    for section_title, sources in sources_map.items():
        sources_section.append(f"\n### {section_title}")
        if isinstance(sources, dict): # For dictionaries like YouTube channels
            for name, identifier in sources.items():
                if "UC" in identifier: # It's a YouTube Channel ID
                    sources_section.append(f"- {name}: [youtube.com/channel/{identifier}](https://www.youtube.com/channel/{identifier})")
                else:
                    sources_section.append(f"- {name}: {identifier}")
        else: # For lists of URLs or subreddits
            for source in sources:
                if source.startswith("https://") or source.startswith("http://"):
                    sources_section.append(f"- {source}")
                else: # Assumes it's a subreddit name
                    sources_section.append(f"- r/{source}")
    
    return "\n".join(sources_section)

def generate_hero_image(project_id, location, gcs_bucket_name, prompt_text):
    """
    Generates an image using the modern Vertex AI SDK and saves it to a public GCS bucket.
    """
    print("--- Starting Hero Image Generation using Vertex AI SDK ---")
    try:
        vertexai.init(project=project_id, location=location)

        image_prompt = (
            "Digital art, concept art, a visually interesting triptych or collage representing "
            "the following themes from the video game industry this week: "
            f"'{prompt_text}'. Cinematic lighting, high detail, epic fantasy style."
        )

        model = ImageGenerationModel.from_pretrained("imagegeneration@006")
        
        response = model.generate_images(
            prompt=image_prompt,
            number_of_images=1,
            aspect_ratio="16:9",
            negative_prompt="text, words, blurry, low quality, watermark, person, character"
        )
        
        temp_filename = "/tmp/hero_image.png"
        response[0].save(location=temp_filename)
        print("Successfully generated image.")

        storage_client = storage.Client()
        bucket = storage_client.bucket(gcs_bucket_name)
        destination_blob_name = f"hero-{int(time.time())}.png"
        blob = bucket.blob(destination_blob_name)

        blob.upload_from_filename(temp_filename)
        blob.make_public()
        
        print(f"Image uploaded to GCS. Public URL: {blob.public_url}")
        return blob.public_url

    except Exception as e:
        print(f"FATAL: Hero image generation failed: {e}")
        return "https://storage.googleapis.com/gemini-generative-ai-python-static/placeholder.png"


def deduplicate_articles(articles, threshold=0.7):
    """
    Filters a list of article dictionaries, removing those with titles similar to already accepted articles.
    """
    unique_articles = []
    print(f"Deduplicating {len(articles)} articles...")
    
    for article in articles:
        is_duplicate = False
        for unique in unique_articles:
            similarity = SequenceMatcher(None, article['title'], unique['title']).ratio()
            if similarity > threshold:
                print(f"Duplicate found (Similarity: {similarity:.2f}):\n  - New: {article['title']}\n  - Existing: {unique['title']}")
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique_articles.append(article)
            
    print(f"Reduced from {len(articles)} to {len(unique_articles)} articles.")
    return unique_articles

# --- ### WEEKLY RSS/API FUNCTIONS ### ---

def fetch_rss_feed_for_weekly(feed_urls):
    """
    MODIFIED: Now returns a list of dictionaries, including a potential image URL.
    """
    print(f"Fetching weekly RSS feeds from: {feed_urls}")
    all_articles = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url, agent='Python Weekly Games Report Bot v1.0')
            if feed.entries:
                print(f"Successfully fetched {len(feed.entries)} articles from {url}")
                for entry in feed.entries[:7]:
                    image_url = ""
                    if 'media_content' in entry and entry.media_content:
                        image_url = entry.media_content[0].get('url', '')
                    elif 'enclosures' in entry and entry.enclosures:
                        image_url = entry.enclosures[0].get('href', '')
                    
                    all_articles.append({
                        "title": entry.title,
                        "summary": entry.get('summary', 'No summary available.'),
                        "source": feed.feed.title,
                        "image_url": image_url
                    })
        except Exception as e:
            print(f"Could not parse feed from {url}. Error: {e}")
    return all_articles

def fetch_youtube_channel_videos(api_key, channel_id):
    """
    MODIFIED: Fetches recent videos and returns a list of dictionaries with title, description, and thumbnail URL.
    """
    print(f"Fetching recent videos from YouTube channel: {channel_id}")
    if not api_key: return [] # Return an empty list on error
    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
        one_week_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
        request = youtube.search().list(part="snippet", channelId=channel_id, maxResults=5, order="date", publishedAfter=one_week_ago, type="video")
        response = request.execute()
        videos = response.get("items", [])
        
        video_list = []
        for item in videos:
            video_list.append({
                "title": item['snippet']['title'],
                "description": item['snippet']['description'],
                "image_url": item['snippet']['thumbnails']['high']['url'],
                "video_url": f"https://www.youtube.com/watch?v={item['id']['videoId']}"
            })
        if not video_list: print(f"No new videos found on channel {channel_id} in the last week.")
        return video_list
    except Exception as e:
        print(f"Could not fetch YouTube videos: {e}")
        return [] # Return an empty list on error

def fetch_reddit_hot_posts(subreddits):
    print(f"Fetching hot Reddit posts from: {subreddits}")
    all_posts = []
    for subreddit in subreddits:
        subreddit = subreddit.strip()
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=5"
        try:
            response = requests.get(url, headers={'User-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'})
            response.raise_for_status()
            data = response.json()
            posts = data['data']['children']
            if posts:
                all_posts.append(f"--- Subreddit: r/{subreddit} ---")
                for post in posts:
                    all_posts.append(f"Title: {post['data']['title']}\nScore: {post['data']['score']}")
        except Exception as e:
             print(f"Could not fetch hot posts from r/{subreddit}. Error: {e}")
    return "\n\n".join(all_posts) if all_posts else "Could not fetch any Reddit posts."

def fetch_upcoming_releases_from_rawg(api_key):
    """
    MODIFIED: Now fetches upcoming games and returns a list of dictionaries including image URLs.
    """
    print("Fetching upcoming game releases from RAWG.io...")
    if not api_key:
        return []

    try:
        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=28)
        dates_query = f"{today.strftime('%Y-%m-%d')},{end_date.strftime('%Y-%m-%d')}"
        url = f"https://api.rawg.io/api/games?key={api_key}&dates={dates_query}&ordering=released"
        
        response = requests.get(url, headers={'User-agent': 'Python Weekly Games Report Bot v1.0'})
        response.raise_for_status()
        data = response.json()
        games = data.get("results", [])
        
        if not games:
            print("No upcoming game releases found on RAWG.io for the next 4 weeks.")
            return []
            
        game_list = []
        for game in games:
            platforms = [p.get('platform', {}).get('name', '') for p in game.get('platforms') or []]
            genres = [g.get('name', '') for g in game.get('genres') or []]
            game_list.append({
                "name": game.get('name', 'Unknown Game'),
                "release_date": game.get('released', 'Unknown Date'),
                "platforms": ", ".join(filter(None, platforms)),
                "genres": ", ".join(filter(None, genres)),
                "image_url": game.get('background_image', '')
            })
        return game_list

    except Exception as e:
        print(f"An error occurred while processing upcoming game releases: {e}")
        return []

def fetch_tentpole_releases_from_rawg(api_key, start_days, end_days):
    """
    MODIFIED: Fetches tentpole releases and returns a list of dictionaries including the background image URL.
    """
    print(f"Fetching tentpole game releases ({start_days}-{end_days} days out) from RAWG.io...")
    if not api_key: return [] # Return an empty list on error

    try:
        today = datetime.date.today()
        start_date = today + datetime.timedelta(days=start_days)
        end_date = today + datetime.timedelta(days=end_days)
        dates_query = f"{start_date.strftime('%Y-%m-%d')},{end_date.strftime('%Y-%m-%d')}"
        url = f"https://api.rawg.io/api/games?key={api_key}&dates={dates_query}&ordering=-added"
        
        response = requests.get(url, headers={'User-agent': 'Python Weekly Games Report Bot v1.0'})
        response.raise_for_status()
        data = response.json()
        games = data.get("results", [])
        
        if not games:
            print(f"No major releases found on RAWG.io between {start_days} and {end_days} days from now.")
            return []
            
        game_list = []
        for game in games[:15]: 
            platforms = [p.get('platform', {}).get('name', '') for p in game.get('platforms') or []]
            genres = [g.get('name', '') for g in game.get('genres') or []]
            game_list.append({
                "name": game.get('name', 'Unknown Game'),
                "release_date": game.get('released', 'Unknown Date'),
                "platforms": ", ".join(filter(None, platforms)),
                "genres": ", ".join(filter(None, genres)),
                "image_url": game.get('background_image', '')
            })
        return game_list

    except Exception as e:
        print(f"An error occurred while processing tentpole game releases: {e}")
        return []

# --- ### WEEKLY REPORT FUNCTION ### ---

def run_weekly_games_report(config):

    print("--- Starting Weekly Games Industry Report ---")
    
    # Setup services
    creds = config['creds_account_1']
    gmail_service = build("gmail", "v1", credentials=creds)
    genai_model = config['genai_model']

    # --- ### DATA FETCHING ### ---
    print("--- Fetching Industry News & Market Analysis ---")
    core_analysis_feeds = ["https://www.gamesindustry.biz/feed", "https://www.gamedeveloper.com/rss.xml", "http://feeds.feedburner.com/venturebeat/games", "https://esportsinsider.com/feed", "https://investgame.net/feed/"]
    player_insight_feeds = ["https://www.pocketgamer.biz/rss/", "https://kotaku.com/rss", "http://feeds.feedburner.com/ign/all", "https://www.eurogamer.net/feed", "https://www.polygon.com/rss/index.xml", "https://www.vg247.com/feed", "https://www.gamespot.com/feeds/mashup", "https://www.pcgamer.com/rss/", "https://news.xbox.com/en-us/feed/", "https://mynintendonews.com/feed/", "https://store.steampowered.com/feeds/news.xml", "https://feeds.feedburner.com/psblog"]
    
    core_news_structured = deduplicate_articles(fetch_rss_feed_for_weekly(core_analysis_feeds))
    player_news_structured = deduplicate_articles(fetch_rss_feed_for_weekly(player_insight_feeds))
    
    core_news_raw = "\n\n".join([f"Title: {article['title']}\nSummary: {article['summary']}" for article in core_news_structured])
    player_news_raw = "\n\n".join([f"Title: {article['title']}\nSummary: {article['summary']}" for article in player_news_structured])
    market_news_raw = core_news_raw + "\n\n---\n\n" + player_news_raw

    print("--- Fetching Developer Learnings & Design ---")
    learning_feeds = ["https://www.gamedeveloper.com/rss.xml", "https://80.lv/articles/feed.xml", "https://howtomarketagame.com/feed/"]
    learning_news_structured = deduplicate_articles(fetch_rss_feed_for_weekly(learning_feeds))
    learning_news_raw = "\n\n".join([f"Title: {article['title']}\nSummary: {article['summary']}" for article in learning_news_structured])

    learning_youtube_channels = {"GDC": "UC0JB7TSe4MAgOdGSh5QZ2aQ", "Game Maker's Toolkit": "UCqJ-Xo29CKyLTB3A_p2qE6A", "AI and Games": "UCov_51F0betb6hJ6Gumxg3Q"}
    all_videos = []
    for name, channel_id in learning_youtube_channels.items():
        all_videos.extend(fetch_youtube_channel_videos(config['YOUTUBE_API_KEY'], channel_id))
    videos_for_prompt = "\n\n".join([f"Video Title: {v['title']}\nDescription: {v['description']}" for v in all_videos])

    print("--- Fetching Technology & Tools Watch ---")
    engine_feeds = ["https://www.unrealengine.com/en-US/rss", "https://blog.unity.com/rss", "https://godotengine.org/rss.xml", "https://aras-p.info/atom.xml", "https://uploadvr.com/feed/", "https://www.roadtovr.com/feed/", "https://blogs.nvidia.com/blog/category/gaming/feed/"]
    engine_news_structured = deduplicate_articles(fetch_rss_feed_for_weekly(engine_feeds))
    engine_news_raw = "\n\n".join([f"Title: {article['title']}\nSummary: {article['summary']}" for article in engine_news_structured])

    print("--- Fetching Community & Release Data ---")
    reddit_subreddits = ["gamedev", "truegamedev", "unity3d", "unrealengine", "godot", "GraphicsProgramming", "proceduralgeneration"]
    reddit_raw = fetch_reddit_hot_posts(reddit_subreddits)

    upcoming_releases_structured = fetch_upcoming_releases_from_rawg(config['RAWG_API_KEY'])
    tentpole_releases_structured = fetch_tentpole_releases_from_rawg(config['RAWG_API_KEY'], start_days=31, end_days=180)

    upcoming_for_prompt = "\n\n".join([f"Game: {g['name']}\nRelease Date: {g['release_date']}\nPlatforms: {g['platforms']}\nGenre: {g['genres']}" for g in upcoming_releases_structured])
    tentpoles_for_prompt = "\n\n".join([f"Game: {g['name']}\nRelease Date: {g['release_date']}" for g in tentpole_releases_structured])

    # --- ### DEFINITIVE PROMPT (WITH HERO IMAGE SELECTION & FULL INSTRUCTIONS) ### ---
    prompt = f"""
    You are a senior games industry analyst. Your task is to provide the textual analysis for a weekly report (the last 7 days), following the specific instructions for each section.

    # The State of Play: Weekly Games Industry Analysis
    
    ## Hero Image Prompt Generation
    **Instructions:** From the **CORE ANALYSIS DATA**, identify the top 2-3 most significant stories. Write a single, concise phrase (10-15 words) that artistically summarizes their core themes. This will be used to generate a piece of concept art. Example: "A shattered company logo, a glowing VR headset, and a triumphant indie character."
    ---
    ## This Week's Key Takeaways
    **Instructions:** FIRST, review all the raw data provided below. Synthesize the entire week's news into 3-4 high-level bullet points that capture the most important overarching themes (e.g., major market trends, prevalent industry challenges, key technology shifts), end each point with an italicized line like '*_Why this matters:_*' that summarizes what this means for the industry as a whole.
    ---
    ## Top Stories & Market Analysis (The Signal)
    **Instructions:** Using the **CORE ANALYSIS DATA**, identify the 5-6 most significant business events. Focus strictly on the strategic and financial implications (M&A, major strategy shifts, financial results). For each:
    1. Create an impactful, bolded headline.
    2. Write a paragraph explaining the event.
    3. Add an italicized line: '*_Why it Matters:_*' to provide your expert analysis. **Defer all player reaction and review summaries to the 'Community Pulse' section.**
    **Do not use a numbered list.**
    **CORE ANALYSIS DATA:**
    {core_news_raw}
    ---
    ## Funding & Investment Tracker (The Signal)
    **Instructions:** From the **CORE ANALYSIS DATA**, identify 2-4 key funding announcements or acquisitions. For each, state the companies, deal size, and goals. Conclude with an italicized line: '*_The Takeaway:_*' analyzing the strategic rationale.
    **Do not use a numbered list.**
    **CORE ANALYSIS DATA:**
    {core_news_raw}
    ---
    ## Community Pulse & Player Reception (The Noise)
    **Instructions:** This section is crucial for understanding the player perspective. Using the **PLAYER INSIGHT DATA** and **REDDIT DATA**, synthesize the biggest trends in player communities this week. Do not repeat business news from the sections above; focus only on the player reaction to it.
    - Identify 3-5 major themes. For each, create a bolded headline (e.g., **"Positive Reception for 'Game X' Launch,"** or **"Debate Over 'Game Y' Monetization"**).
    - Under each headline, summarize the general player sentiment. What are the common points of praise or criticism found in reviews and community threads?
    - Conclude each theme with an italicized line: '*_Developer Takeaway:_*' translating the player sentiment into a concrete lesson for developers.
    **Do not use a numbered list.**
    **PLAYER INSIGHT DATA:**
    {player_news_raw}
    **REDDIT DATA:**
    {reddit_raw}
    ---
    ## Insights for Developers
    **Instructions:** Review the provided articles and video descriptions. Extract 3-4 key design principles, post-mortem learnings, or innovative techniques. For each, use a bolded headline followed by a paragraph explaining the concept. End each point with an italicized line like '*_The Takeaway:_*' that summarizes the actionable advice for developers.
    **Do not use a numbered list.**
    **RAW LEARNINGS DATA:**
    {learning_news_raw}
    **RAW VIDEO DATA FOR INSIGHTS:**
    {videos_for_prompt}
    ---
    ## Technology, Hardware and Tools Updates
    **Instructions:** Review the provided engine news and Reddit data. Identify the 3-4 most important new tools, engine features, or emerging technologies. For each, use a bolded headline, explain what the technology does, and then add an italicized line like '*_Why it's exciting:_*' to explain the practical benefit for developers.
    **Do not use a numbered list.**
    **RAW ENGINE NEWS:**
    {engine_news_raw}
    **RAW REDDIT DATA:**
    {reddit_raw}
    ---
    ## New Game Announcements
    **Instructions:** From the raw market news, identify any newly announced games upcoming in the next 6-48 months. Include the game name, platforms, and genre.
    **RAW MARKET NEWS DATA:**
    **Do not use a numbered list.**
    {market_news_raw}
    ---
    ## Upcoming Releases (Next 30 days)
    **Instructions:** Review the list of upcoming game releases. Present them in a simple, chronological list including name, release date, genre(s), and platforms.
    **RAW UPCOMING RELEASES DATA:**
    **Do not use a numbered list.**
    {upcoming_for_prompt} 
    ---
    ## Tentpole Releases (1+ Months)
    **Instructions:** Review the list of upcoming tentpole games. Write a short paragraph identifying which 2-3 of these titles you believe are the most significant and why they are important for developers to watch. **Mention the selected games by their exact name in your analysis.**
    **RAW TENTPOLE RELEASES DATA:**
    **Do not use a numbered list.**
    {tentpoles_for_prompt}
    ---
    """

    # --- ### AI SYNTHESIS ### ---
    print("--- Synthesizing Weekly Report Text ---")
    try:
        response = genai_model.generate_content(prompt)
        report_text = response.text
    except Exception as e:
        report_text = f"An error occurred during AI synthesis: {e}"
        print(f"FATAL: AI Synthesis failed. Aborting. Error: {e}")
        return

    # --- ### POST-AI ASSEMBLY & HTML GENERATION ### ---
    print("\n--- Assembling Final Email with Generated Hero Image ---")
    
    report_sections = {}
    # CORRECTED: This list now correctly includes all 10 sections from the prompt
    section_titles = [
        "Hero Image Prompt Generation", "This Week's Key Takeaways", "Top Stories & Market Analysis (The Signal)", "Funding & Investment Tracker (The Signal)",
        "Community Pulse & Player Reception (The Noise)", "Insights for Developers", 
        "Technology, Hardware and Tools Updates", "New Game Announcements", "Upcoming Releases (Next 30 days)", 
        "Tentpole Releases (1+ Months)"
    ]
    split_text = report_text.split('---')
    for i, block in enumerate(split_text):
        if i < len(section_titles):
            content = re.sub(r'^\s*#+\s*.*?\n', '', block, count=1).strip()
            report_sections[section_titles[i]] = content

    # --- Generate the Hero Image ---
    image_prompt_text = report_sections.get("Hero Image Prompt Generation", "general video game industry news")
    
    gcs_bucket_name = "weekly-report-hero-images-keen-life-464422-t0" 
    
    hero_img_1 = generate_hero_image(
        project_id=config['project_id'], 
        location="us-central1", 
        gcs_bucket_name=gcs_bucket_name,
        prompt_text=image_prompt_text
    )

    # --- Build the Hero Image HTML Block ---
    placeholder_image = "https://storage.googleapis.com/gemini-generative-ai-python-static/placeholder.png"
    
    # Get game object for upcoming release
    upcoming_hero_game = upcoming_releases_structured[0] if upcoming_releases_structured else None
    hero_img_2 = upcoming_hero_game['image_url'] if upcoming_hero_game and upcoming_hero_game.get('image_url') else placeholder_image
    
    # Get game object for tentpole release
    tentpole_analysis_text = report_sections.get("Tentpole Releases (1+ Months)", "")
    tentpole_hero_game = next((game for game in tentpole_releases_structured if game['name'] in tentpole_analysis_text), None)
    hero_img_3 = tentpole_hero_game['image_url'] if tentpole_hero_game and tentpole_hero_game.get('image_url') else placeholder_image

    # This HTML now includes captions
    hero_image_html = f"""
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 20px;">
        <tr>
            <td style="padding-right: 5px; width: 33.33%; text-align: center; vertical-align: top;">
                <img src="{hero_img_1}" alt="AI Art of Weekly Themes" style="width: 100%; height: auto; display: block; border-radius: 4px;">
                <p style="font-size: 11px; color: #666; margin: 4px 0 0 0;">This Week's Themes</p>
            </td>
            <td style="padding-left: 5px; padding-right: 5px; width: 33.33%; text-align: center; vertical-align: top;">
                <img src="{hero_img_2}" alt="Upcoming Release" style="width: 100%; height: auto; display: block; border-radius: 4px;">
                <p style="font-size: 11px; color: #666; margin: 4px 0 0 0;">Upcoming: {upcoming_hero_game['name'] if upcoming_hero_game else ''}</p>
            </td>
            <td style="padding-left: 5px; width: 33.33%; text-align: center; vertical-align: top;">
                <img src="{hero_img_3}" alt="Tentpole Release" style="width: 100%; height: auto; display: block; border-radius: 4px;">
                <p style="font-size: 11px; color: #666; margin: 4px 0 0 0;">Tentpole: {tentpole_hero_game['name'] if tentpole_hero_game else ''}</p>
            </td>
        </tr>
    </table>
    """

    # --- (The rest of the assembly logic is the same) ---
    tentpole_html_images = ""
    for game in tentpole_releases_structured:
        if game['name'] in tentpole_analysis_text:
            tentpole_html_images += f"""
            <div style="margin-bottom: 25px; padding-bottom: 15px; border-bottom: 1px solid #eee;">
                <img src="{game['image_url']}" alt="Cover art for {game['name']}" style="width:100%; height:auto; border-radius: 8px; margin-bottom: 12px;">
                <h3 style="margin: 0 0 5px 0; font-size: 18px;">{game['name']}</h3>
                <p style="margin: 0 0 4px 0;"><strong>Release Date:</strong> {game['release_date']}</p>
                <p style="margin: 0 0 4px 0;"><strong>Platforms:</strong> {game['platforms']}</p>
                <p style="margin: 0;"><strong>Genre:</strong> {game['genres']}</p>
            </div>
            """

    sources_map = {
        "Top Stories & Funding/Investment": core_analysis_feeds,
        "Community Pulse & Player Reception": player_insight_feeds,
        "Developer Learnings & Design": learning_feeds,
        "Developer Video Insights": learning_youtube_channels,
        "Technology, Tools & Hardware": engine_feeds,
        "Community & Developer Discussion": reddit_subreddits,
        "Upcoming Game Release Data": ["https://rawg.io/api"],
    }
    sources_markdown = format_sources_for_email(sources_map)
    
    today_str = datetime.date.today().strftime("%B %d, %Y")
    email_subject = f"Your Weekly Games Industry Report: {today_str}"

    for key, value in report_sections.items():
        report_sections[key] = markdown.markdown(value, extensions=['extra'])
    
    final_html_content = f"""
        <h1>{email_subject}</h1>
        {hero_image_html}
        <h2>This Week's Key Takeaways</h2>
        {report_sections.get("This Week's Key Takeaways", "")}
        <h2>Top Stories & Market Analysis (The Signal)</h2>
        {report_sections.get("Top Stories & Market Analysis (The Signal)", "")}
        <h2>Funding & Investment Tracker (The Signal)</h2>
        {report_sections.get("Funding & Investment Tracker (The Signal)", "")}
        <h2>Community Pulse & Player Reception</h2>
        {report_sections.get('Community Pulse & Player Reception (The "Noise" as Insight)', "")}
        <h2>Insights for Developers</h2>
        {report_sections.get("Insights for Developers", "")}
        <h2>Technology, Hardware and Tools Updates</h2>
        {report_sections.get("Technology, Hardware and Tools Updates", "")}
        <h2>New Game Announcements</h2>
        {report_sections.get("New Game Announcements", "")}
        <h2>Upcoming Releases (Next 30 days)</h2>
        {report_sections.get("Upcoming Releases (Next 30 days)", "")}
        <h2>Tentpole Releases (1+ Months)</h2>
        {tentpole_html_images}
        {report_sections.get("Tentpole Releases (1+ Months)", "")}
        <hr>
        {markdown.markdown(sources_markdown)}
    """
        
    html_body = f"""
    <html><head><style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: auto; padding: 20px; }}
        h1, h2, h3 {{ font-weight: 500; color: #111; }} h1 {{ font-size: 24px; }}
        h2 {{ font-size: 20px; border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 30px; }}
        h3 {{font-size: 16px;}}
    </style></head><body>
        {final_html_content}
    </body></html>
    """
    send_email(gmail_service, email_subject, html_body, config['RECIPIENT_EMAIL'])
    
    print("Weekly games report finished successfully.")

# --- MAIN ROUTER ---
if __name__ == "__main__":
    print("Starting Weekly Games Report...")
    
    from google.auth import default as google_auth_default
    _, project_id = google_auth_default()
    if not project_id: raise RuntimeError("Could not determine Google Cloud Project ID.")

    config = {
        "project_id": project_id,
        "GEMINI_API_KEY": get_secret("GEMINI_API_KEY", project_id),
        "OAUTH_TOKEN_JSON": get_secret("OAUTH_TOKEN_JSON", project_id),
        "YOUTUBE_API_KEY": get_secret("YOUTUBE_API_KEY", project_id),
        "RAWG_API_KEY": get_secret("RAWG_API_KEY", project_id),
        "RECIPIENT_EMAIL": get_secret("RECIPIENT_EMAIL_WEEKLY", project_id),
    }

    genai.configure(api_key=config['GEMINI_API_KEY'])
    config['genai_model'] = genai.GenerativeModel('gemini-2.0-flash')
    
    token_info_1 = json.loads(config['OAUTH_TOKEN_JSON'])
    config['creds_account_1'] = Credentials.from_authorized_user_info(token_info_1, SCOPES)
    
    run_weekly_games_report(config)
    print("Script finished successfully.")