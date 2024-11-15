import logging
import time
import json
import requests
import boto3
from celery import shared_task
from ..prompts.assistant_prompt import system_prompt
from ..prompts.hero_bio_prompt import bio_prompt
from ..utils import make_api_call_with_backoff, redis_client, boto3_config
from .fetch_hero_data import fetch_hero_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_hero_bio_task(self, key, folder, hero_name):
    if key == "hero-bios/": return
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as hero bio information (attempt {attempt_count + 1})")
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

        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_S3_BUCKET, 'Key': key},
            ExpiresIn=3600  
        )

        # Prepare the messages
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
                        "text": bio_prompt + json.dumps(hero['heroInformation']['bioFields']), 
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

        # Prepare the data payload (as JSON)
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 1000,
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
        extracted_data = response_json['choices'][0]['message']['content']
        cleaned_data = extracted_data.strip('```json').strip('```')

        # Attempt to parse the extracted data as JSON
        try:                
            hero_bio = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")
            
            payload = {
                'hero_id': hero.get('databaseId', 0),
                'age': hero_bio.get('age', 0),
                'height': hero_bio.get('height', 0),
                'weight': hero_bio.get('weight', 0),
                'species': hero_bio.get('species', 0),
                'role': hero_bio.get('role', 0),
                'element': hero_bio.get('element', 0),
                'rarity': hero_bio.get('rarity', 0),
            }

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Hero Bio - {hero['title']}",
                "description": "Here's what I found in your image:",
                "color": 3447003,  # Example blue color
                "fields": [
                    {"name": "Age", "value": payload["age"], "inline": True} if payload["age"] != 0 else None,
                    {"name": "Height", "value": payload["height"], "inline": True} if payload["height"] != 0 else None,
                    {"name": "Weight", "value": payload["weight"], "inline": True} if payload["weight"] != 0 else None,
                    {"name": "Species", "value": payload["species"], "inline": True} if payload["species"] != 0 else None,
                    {"name": "Role", "value": payload["role"], "inline": True} if payload["role"] != 0 else None,
                    {"name": "Element", "value": payload["element"], "inline": True} if payload["element"] != 0 else None,
                    {"name": "Rarity", "value": payload["rarity"], "inline": True} if payload["rarity"] != 0 else None,
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
                'task_id': process_hero_bio_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for hero: {hero['title']}")
            
            # Wait for poll result (e.g., 60 seconds)
            result_key = f"discord_poll_result:{process_hero_bio_task.request.id}"
            
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
            
            update_url = f"{WORDPRESS_SITE}/wp-json/heavenhold/v1/update-bio"
            
            # If upvotes are higher than downvotes, post the data to WordPress
            if retry_count > 0:
                logger.info(f"Retrying processing for {hero['title']} bio")
                # Reset attempt count
                redis_client.set('attempts:' + key, 0)
                redis_client.delete('lock:' + key)
                return
            elif upvotes > downvotes:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'age':payload['age'],
                    'height': payload['height'],
                    'weight': payload['weight'],
                    'species': payload['species'],
                    'role': payload['role'],
                    'element': payload['element'],
                    'rarity': payload['rarity'],
                    'confirmed': True
                })
                response.raise_for_status()
                logger.info(f"Hero bio updated successfully for hero {hero['title']}")
            elif upvotes == 0 and downvotes == 0:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'age':payload['age'],
                    'height': payload['height'],
                    'weight': payload['weight'],
                    'species': payload['species'],
                    'role': payload['role'],
                    'element': payload['element'],
                    'rarity': payload['rarity'],
                    'confirmed': False
                })
                response.raise_for_status()
                logger.info(f"Hero bio updated successfully for hero {hero['title']}")
            else:
                logger.info(f"Aborting bio update for {hero['title']}")
            # Delete the image after processing (if desired)
            s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)     
            redis_client.delete('attempts:' + key)          
            redis_client.delete('lock:' + key)
            logger.info(f"{key} processed successfully, deleting from S3 bucket.")
            fetch_hero_data.delay()    
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from AI response")
            logger.error(e)
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