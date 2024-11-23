import io
import logging
import time
import json
import requests
import boto3
from celery import shared_task
from ..prompts.item_system_prompt import item_system
from ..prompts.weapon_prompt import weapon_prompt
from ..utils import encode_image_to_base64, redis_client, boto3_config
from .fetch_item_data import fetch_item_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_costume_illustration_task(self, key, folder, item_name, hero_name):
    if key == "costume-illustrations/": return    
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a super costume illustration (attempt {attempt_count + 1})")   
    try:
        # Retrieve cached data
        cached_item_data = redis_client.get('item_data')
        cached_hero_data = redis_client.get('hero_data')
        if cached_item_data is None:
            logger.warning("Item data not found in cache.")
            return
        if cached_hero_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        item_data = json.loads(cached_item_data)
        hero_data = json.loads(cached_hero_data)
        hero = next((h for h in hero_data if h['slug'] == hero_name), None)
        item = next((i for i in item_data if i['slug'] == item_name), None)

        if item is None:
            logger.warning(f"Item '{item_name}' not found.")
            return

        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Generate a pre-signed URL for the image
        
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_S3_BUCKET, 'Key': key},
            ExpiresIn=3600  
        )

        # Retrieve and process the image from S3
        s3_response = s3_client.get_object(
            Bucket=AWS_S3_BUCKET,
            Key=key
        )
        image_content = s3_response['Body'].read()

        # Save the image to separate BytesIO objects
        img_byte_arr_base64 = io.BytesIO()
        img_byte_arr_base64.write(image_content)
        image_bytes = img_byte_arr_base64.getvalue()

        img_byte_arr = io.BytesIO()
        img_byte_arr.write(image_content)
        img_byte_arr.seek(0)

        # Prepare and send the poll to Discord
        embed_data = {
            "channel_id": DISCORD_CHANNEL_ID, 
            "is_embed": True,
            "title": f"Super Costume - {item['title']}",
            "description": "Here's what you gave me:",
            "color": 3447003,  
            "fields": [
                {"name": "Hero", "value": hero['title'], "inline": True} if hero else None,                        
            ],
            "footer": {"text": "Does this look correct?"}
        }

        # Remove any None fields (in case some stats are not present)
        embed_data["fields"] = [field for field in embed_data["fields"] if field]

        # Convert the image to Base64
        base64_image = encode_image_to_base64(image_bytes)
        
        # Send poll request to Discord through Redis
        poll_data = {
            'channel_id': DISCORD_CHANNEL_ID, 
            'is_embed': True,
            'embed': embed_data,
            'image': base64_image,
            'filename': item_name + '.png',
            'task_id': process_costume_illustration_task.request.id
        }
        redis_client.rpush('discord_message_queue', json.dumps(poll_data))
        logger.info(f"Sent poll to Discord for costume: {item['title']}")
        
        # Wait for poll result (e.g., 120 seconds)
        result_key = f"discord_poll_result:{process_costume_illustration_task.request.id}"
        
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
        
        # If upvotes are higher than downvotes, post the data to WordPress
        if retry_count > 0:
            logger.info(f"Retrying processing for {item['title']} stats")
            # Reset attempt count
            redis_client.set('attempts:' + key, 0)
            redis_client.delete('lock:' + key)
            return

        # Prepare the files and payload
        img_byte_arr.seek(0)
        files = {
            'image': (item_name + '.png', img_byte_arr, 'image/png')
        }
        payload = {
            'item_id': str(item['databaseId']),
            'confirmed': '1' if upvotes > downvotes else '0'
        }

        # Log the data being sent
        logger.info(f"Sending data: {payload}")
        logger.info(f"Sending files: {files}")

        # Send the POST request
        try:
            update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/update-super-illustration'
            response = requests.post(update_url, files=files, data=payload)
            response.raise_for_status()
            logger.info("Item super illustration updated successfully")
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error occurred: {e}")
            logger.error(f"Response content: {response.text}")
            raise

        # Delete the image after processing
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        logger.info(f"{key} processed successfully, deleting from S3 bucket.") 
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