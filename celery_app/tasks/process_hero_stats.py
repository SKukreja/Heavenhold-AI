import logging
import time
import json
import requests
import boto3
from celery import shared_task
from ..prompts.assistant_prompt import system_prompt
from ..prompts.stat_prompt import stat_prompt
from ..utils import make_api_call_with_backoff, redis_client, boto3_config
from .fetch_hero_data import fetch_hero_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_hero_stats_task(self, key, folder, hero_name):
    if key == "hero-stats/": return    
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero stat information (attempt {attempt_count + 1})")
    try:        
        # Retrieve cached data
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image    
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_S3_BUCKET, 'Key': key},
            ExpiresIn=3600
        )

        # AI processing: Preparing the AI payload
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": stat_prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": pre_signed_url
                        },
                    }
                ],
            },
        ]
        
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 1000,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }

        # Make the API call to OpenAI
        response = make_api_call_with_backoff(
            "https://api.openai.com/v1/chat/completions",
            headers,
            payload
        )
        response.raise_for_status()
        response_json = response.json()

        # Process the AI response
        extracted_data = response_json['choices'][0]['message']['content']
        cleaned_data = extracted_data.strip('```json').strip('```')

        # Attempt to parse the extracted data as JSON
        try:
            hero_stats = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {e}")
            return

        # Example payload for hero stats from AI response
        payload = {
            "atk": hero_stats.get("atk", 0),
            "def": hero_stats.get("def", 0),
            "hp": hero_stats.get("hp", 0),
            "crit": hero_stats.get("crit", 0),
            "heal": hero_stats.get("heal", 0),
            "damage_reduction": hero_stats.get("damage_reduction", 0),
            "basic_resistance": hero_stats.get("basic_resistance", 0),
            "light_resistance": hero_stats.get("light_resistance", 0),
            "dark_resistance": hero_stats.get("dark_resistance", 0),
            "fire_resistance": hero_stats.get("fire_resistance", 0),
            "earth_resistance": hero_stats.get("earth_resistance", 0),
            "water_resistance": hero_stats.get("water_resistance", 0),
            "compatible_equipment": hero_stats.get("compatible_equipment", []),
            "passives": hero_stats.get("passives", [])
        }

        # Fix: Extract string values from passives if they are dictionaries
        passives_list = []
        if 'passives' in payload:
            passives_list = [
                f"{'[Party] ' if passive['affects_party'] else ''}{passive['stat']} +{passive['value']:.1f}%"
                for passive in payload['passives']
            ]

        # Prepare and send the poll to Discord
        embed_data = {
            "title": f"Hero Stats - {hero['title']}",
            "description": "Here's what I found in your image:",
            "color": 3447003,  # Example blue color
            "fields": [
                {"name": "Atk", "value": payload["atk"], "inline": True} if payload["atk"] != 0 else None,
                {"name": "Def", "value": payload["def"], "inline": True} if payload["def"] != 0 else None,
                {"name": "HP", "value": payload["hp"], "inline": True} if payload["hp"] != 0 else None,
                {"name": "Crit", "value": payload["crit"], "inline": True} if payload["crit"] != 0 else None,
                {"name": "Heal", "value": payload["heal"], "inline": True} if payload["heal"] != 0 else None,
                {"name": "Damage Reduction", "value": payload["damage_reduction"], "inline": True} if payload["damage_reduction"] != 0 else None,
                {"name": "Basic Resistance", "value": payload["basic_resistance"], "inline": True} if payload["basic_resistance"] != 0 else None,
                {"name": "Light Resistance", "value": payload["light_resistance"], "inline": True} if payload["light_resistance"] != 0 else None,
                {"name": "Dark Resistance", "value": payload["dark_resistance"], "inline": True} if payload["dark_resistance"] != 0 else None,
                {"name": "Fire Resistance", "value": payload["fire_resistance"], "inline": True} if payload["fire_resistance"] != 0 else None,
                {"name": "Earth Resistance", "value": payload["earth_resistance"], "inline": True} if payload["earth_resistance"] != 0 else None,
                {"name": "Water Resistance", "value": payload["water_resistance"], "inline": True} if payload["water_resistance"] != 0 else None,
                {"name": "Compatible Equipment", "value": "\n".join(payload["compatible_equipment"]), "inline": False},
                {"name": "Passives", "value": "\n".join(passives_list), "inline": False},
            ],
            "footer": {"text": "Does this look correct?"}
        }

        # Remove any None fields (in case some stats are not present)
        embed_data["fields"] = [field for field in embed_data["fields"] if field]

        # Send poll request to Discord through Redis
        poll_data = {
            'channel_id': DISCORD_CHANNEL_ID, 
            'is_embed': True,
            'embed': embed_data,
            'task_id': process_hero_stats_task.request.id
        }
        redis_client.rpush('discord_message_queue', json.dumps(poll_data))
        logger.info(f"Sent poll to Discord for hero: {hero['title']}")

        # Wait for poll result (e.g., 60 seconds)
        result_key = f"discord_poll_result:{process_hero_stats_task.request.id}"
        
        # If upvotes are higher than downvotes, post the data to WordPress
        upvotes, downvotes, retry_count = 0, 0, 0

        for _ in range(100):  # Check every second, up to 120 seconds
            poll_result = redis_client.get(result_key)
            if poll_result:
                poll_result_data = json.loads(poll_result)
                upvotes = poll_result_data.get('upvotes', 0)
                downvotes = poll_result_data.get('downvotes', 0)
                retry_count = poll_result_data.get('retry', 0)
                redis_client.delete(result_key)
                break
            time.sleep(1)

        logger.info("Checking poll results: Upvotes - %d, Downvotes - %d", upvotes, downvotes)
        
        update_url = f"{WORDPRESS_SITE}/wp-json/heavenhold/v1/update-stats"
        
        # If upvotes are higher than downvotes, post the data to WordPress
        if retry_count > 0:
            logger.info(f"Retrying processing for {hero['title']} stats")
            # Reset attempt count
            redis_client.set('attempts:' + key, 0)
            redis_client.delete('lock:' + key)
            return
        elif upvotes > downvotes:
            response = requests.post(update_url, json={
                'hero_id': hero['databaseId'],
                'atk': payload['atk'],
                'def': payload['def'],
                'hp': payload['hp'],
                'crit': payload['crit'],
                'heal': payload['heal'],
                'damage_reduction': payload['damage_reduction'],
                'basic_resistance': payload['basic_resistance'],
                'light_resistance': payload['light_resistance'],
                'dark_resistance': payload['dark_resistance'],
                'fire_resistance': payload['fire_resistance'],
                'earth_resistance': payload['earth_resistance'],
                'water_resistance': payload['water_resistance'],
                'compatible_equipment': payload['compatible_equipment'],
                'passives': payload['passives'],
                'confirmed': True
            })
            response.raise_for_status()
            logger.info(f"Hero stats updated successfully for hero {hero['title']}")
        elif upvotes == 0 and downvotes == 0:
            response = requests.post(update_url, json={
                'hero_id': hero['databaseId'],
                'atk': payload['atk'],
                'def': payload['def'],
                'hp': payload['hp'],
                'crit': payload['crit'],
                'heal': payload['heal'],
                'damage_reduction': payload['damage_reduction'],
                'basic_resistance': payload['basic_resistance'],
                'light_resistance': payload['light_resistance'],
                'dark_resistance': payload['dark_resistance'],
                'fire_resistance': payload['fire_resistance'],
                'earth_resistance': payload['earth_resistance'],
                'water_resistance': payload['water_resistance'],
                'compatible_equipment': payload['compatible_equipment'],
                'passives': payload['passives'],
                'confirmed': False
            })
            response.raise_for_status()
            logger.info(f"Hero stats updated successfully for hero {hero['title']}")
        else:
            logger.info(f"Aborting stat update for {hero['title']}")
        # Delete the image after processing (if desired)
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        logger.info(f"{key} processed successfully, deleting from S3 bucket.")
        fetch_hero_data.delay()    
    except Exception as e:
        # Increment the attempt count
        attempt_count = redis_client.incr('attempts:' + key)
        if attempt_count >= 3:
            logger.exception(f"Error processing image {key}. Max attempts reached. Deleting image.")
            s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
            redis_client.delete('attempts:' + key)
            redis_client.delete('lock:' + key)
        else:
            logger.exception(f"Error processing image {key}. Retrying after 180 seconds.")
            redis_client.delete('lock:' + key)
            raise self.retry(exc=e, countdown=180)