import logging
import time
import json
import requests
import boto3
from celery import shared_task
from ..prompts.assistant_prompt import system_prompt
from ..prompts.hero_story_prompt import story_prompt
from ..utils import make_api_call_with_backoff, redis_client, boto3_config
from .fetch_hero_data import fetch_hero_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_hero_story_task(self, key, folder, hero_name):
    if key == "hero-stories/": return
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero story (attempt {attempt_count + 1})")     
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
                        "text": story_prompt + json.dumps(hero['heroInformation']['bioFields']),
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
            hero_story = json.loads(cleaned_data)
            logger.info("Successfully processed JSON from AI response")

            payload = {
                'hero_id': hero.get('databaseId', 0),
                'story': hero_story.get('story', 0),
            }

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Hero Story - {hero['title']}",
                "description": "Here's what I found in your image:\n\n" + (payload['story'] + "").replace("<br />", "\n"),
                "color": 3447003,                
                "footer": {"text": "Does this look correct?"}
            }
            
            # Send poll request to Discord through Redis
            poll_data = {
                'channel_id': DISCORD_CHANNEL_ID, 
                'is_embed': True,
                'embed': embed_data,
                'task_id': process_hero_story_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for hero: {hero['title']}")
            
            # Wait for poll result (e.g., 60 seconds)
            result_key = f"discord_poll_result:{process_hero_story_task.request.id}"
            
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
            
            update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/update-story'
            
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
                    'story': payload['story'],
                    'confirmed': True
                })
                response.raise_for_status()
                logger.info("Hero story updated successfully")
            elif upvotes == 0 and downvotes == 0:
                response = requests.post(update_url, json={
                    'hero_id': hero['databaseId'],
                    'story': payload['story'],
                    'confirmed': True
                })
                response.raise_for_status()
                logger.info("Hero story updated successfully")
            else:
                logger.info(f"Aborting story update for {hero['title']}")

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