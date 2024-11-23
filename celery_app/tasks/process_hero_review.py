import logging
import time
import json
import requests
from celery import shared_task
from ..prompts.proofreader_system_prompt import proofreader_system
from ..prompts.hero_story_prompt import story_prompt
from ..utils import make_api_call_with_backoff, redis_client
from .fetch_hero_data import fetch_hero_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_hero_review_task(self, hero, channel_id, content):
    global redis_client, OPENAI_API_KEY
    # Retrieve cached data
    cached_data = redis_client.get('hero_data')
    if cached_data is None:
        logger.warning("Hero data not found in cache.")
        return
    
    hero_data = json.loads(cached_data)
    hero = next((h for h in hero_data if h['title'] == hero), None)

    if hero is None:
        logger.warning(f"Hero '{hero}' not found.")
        return

    # Prepare the messages
    messages = [
        {
            "role": "system",
            "content": proofreader_system,
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": '''Hero name: ''' + (hero['title'] or '') + '''\nCurrent information:\n''' + (hero['heroInformation']['analysisFields']['detailedReview'] or '') + '''\nNew information:\n''' + content,
                },
            ],
        },
    ]

    # Prepare the data payload (as JSON)
    payload = {
        "model": "gpt-4o",
        "messages": messages,
        "max_tokens": 2000,
    }

    # Set up headers
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }

    # Make the API call
    response = make_api_call_with_backoff(
        "https://api.openai.com/v1/chat/completions",
        headers,
        payload
    )

    # Parse the JSON content
    response.raise_for_status()
    response_json = response.json()            

    # Access the 'choices' data
    updated_review = response_json['choices'][0]['message']['content']

    logger.info("updated_review: " + updated_review)

    # Attempt to parse the extracted data as JSON
    try:                
        logger.info("Successfully processed AI response")

        # Send poll request to Discord through Redis
        message_data = {
            'channel_id': DISCORD_CHANNEL_ID, 
            'is_embed': False,
            'message': f"Hero review for {hero['title']} has been updated. Please review the changes.",                
        }
        redis_client.rpush('discord_message_queue', json.dumps(message_data))
        logger.info(f"Sent poll to Discord for hero: {hero['title']}")
                                            
        update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/update-hero-review'

        response = requests.post(update_url, json={
            'hero_id': hero.get('databaseId', 0),
            'detailed_review': updated_review,
            'confirmed': True
        })
        response.raise_for_status()
        logger.info("Hero review updated successfully")
        fetch_hero_data.delay()    
    except json.JSONDecodeError as e:
        logger.error("Failed to parse from AI response")
        logger.error(e)