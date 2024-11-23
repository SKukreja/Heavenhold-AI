import logging
import math
import time
import json
import requests
import io
import tempfile
import boto3
import os
from celery import shared_task
from PIL import Image
from ..utils import encode_image_to_base64, redis_client, boto3_config
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET

logger = logging.getLogger(__name__)

item_types = [
    { 'id': 'filter-mobile-category-1hsword', 'value': 'one-handed-sword', 'label': 'One-Handed Sword', 'icon': '/icons/equipment/1hsword.webp' },
    { 'id': 'filter-mobile-category-2hsword', 'value': 'two-handed-sword', 'label': 'Two-Handed Sword', 'icon': '/icons/equipment/2hsword.webp' },
    { 'id': 'filter-mobile-category-rifle', 'value': 'rifle', 'label': 'Rifle', 'icon': '/icons/equipment/rifle.webp' },
    { 'id': 'filter-mobile-category-bow', 'value': 'bow', 'label': 'Bow', 'icon': '/icons/equipment/bow.webp' },
    { 'id': 'filter-mobile-category-basket', 'value': 'basket', 'label': 'Basket', 'icon': '/icons/equipment/basket.webp' },
    { 'id': 'filter-mobile-category-staff', 'value': 'staff', 'label': 'Staff', 'icon': '/icons/equipment/staff.webp' },
    { 'id': 'filter-mobile-category-gauntlet', 'value': 'gauntlet', 'label': 'Gauntlet', 'icon': '/icons/equipment/gauntlet.webp' },
    { 'id': 'filter-mobile-category-claw', 'value': 'claw', 'label': 'Claw', 'icon': '/icons/equipment/claw.webp' },
    { 'id': 'filter-mobile-category-shield', 'value': 'shield', 'label': 'Shield', 'icon': '/icons/equipment/shield.webp' },
    { 'id': 'filter-mobile-category-accessory', 'value': 'accessory', 'label': 'Accessory', 'icon': '/icons/equipment/accessory.webp' },
    { 'id': 'filter-mobile-category-costume', 'value': 'costume', 'label': 'Hero Costume', 'icon': '/icons/equipment/herocostume.webp' },
    { 'id': 'filter-mobile-category-equipmentcostume', 'value': 'equipment-costume', 'label': 'Equipment Costume', 'icon': '/icons/equipment/equipmentcostume.webp' },
    { 'id': 'filter-mobile-category-illustrationcostume', 'value': 'illustration-costume', 'label': 'Illustration Costume', 'icon': '/icons/equipment/illustrationcostume.webp' },
    { 'id': 'filter-mobile-category-card', 'value': 'card', 'label': 'Card', 'icon': '/icons/equipment/card.webp' },
    { 'id': 'filter-mobile-category-merch', 'value': 'merch', 'label': 'Merch', 'icon': '/icons/equipment/merch.webp' },
    { 'id': 'filter-mobile-category-relic', 'value': 'relic', 'label': 'Relic', 'icon': '/icons/equipment/relic.webp' },
]

@shared_task(bind=True)
def process_costume_task(self, key, folder, item_name, hero_name, item_type):
    if key == "costumes/": return
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero portrait (attempt {attempt_count + 1})")    
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
        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

        equipment_costume_type = next((i for i in item_types if i['value'] == item_type), None)

        # Initialize S3 client using app.config variables
        s3_client = boto3.client('s3', **boto3_config)

        # Retrieve and process the image from S3
        response = s3_client.get_object(
            Bucket=AWS_S3_BUCKET,
            Key=key
        )
        image_content = response['Body'].read()

        # Create a temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save the image in the temporary directory
            image_path = os.path.join(temp_dir, "image.jpg")
            with open(image_path, 'wb') as f:
                f.write(image_content)

            # After determining the bar widths, you can crop the image accordingly:
            img = Image.open(image_path)
            crop_dimension = math.floor(img.width * 0.3759)
            crop_left = math.floor(img.width * 0.3111)
            crop_top = math.floor(img.height * 0.2796)
            cropped_img = img.crop((crop_left, crop_top, crop_left + crop_dimension, crop_top + crop_dimension))

            # Save the cropped image to separate BytesIO objects
            img_byte_arr_base64 = io.BytesIO()
            cropped_img.save(img_byte_arr_base64, format='JPEG')
            image_bytes = img_byte_arr_base64.getvalue()

            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='JPEG')
            img_byte_arr.seek(0)

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Costume - {item['title']}",
                "description": "I did my best!",
                "color": 3447003,  # Example blue color
                "fields": [
                    {"hero" if hero else "type": hero['title'] if hero else equipment_costume_type['label'], "inline": True},                        
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
                'filename': item_name + '.jpg',
                'task_id': process_costume_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for costume: {item['title']}")
            
            # Wait for poll result (e.g., 120 seconds)
            result_key = f"discord_poll_result:{process_costume_task.request.id}"
            
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
                logger.info(f"Retrying processing for {hero['title']} stats")
                # Reset attempt count
                redis_client.set('attempts:' + key, 0)
                redis_client.delete('lock:' + key)
                return

            # Prepare the files and payload
            img_byte_arr.seek(0)
            files = {
                'image': (item_name + '.jpg', img_byte_arr, 'image/jpeg')
            }
            payload = {
                'hero_id': str(hero['databaseId']),
                'item_id': str(item['databaseId']),
                'item_type': equipment_costume_type['label'] if equipment_costume_type else '',
                'confirmed': '1' if upvotes > downvotes else '0'
            }

            # Log the data being sent
            logger.info(f"Sending data: {payload}")
            logger.info(f"Sending files: {files}")

            # Send the POST request
            try:
                update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/update-costume'
                response = requests.post(update_url, files=files, data=payload)
                response.raise_for_status()
                logger.info("Costume updated successfully")
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