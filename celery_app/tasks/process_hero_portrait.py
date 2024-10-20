import logging
import time
import json
import requests
import io
import tempfile
import boto3
from celery import shared_task
from PIL import Image
from ..utils import encode_image_to_base64, detect_black_bar_width, redis_client, boto3_config
from .fetch_hero_data import fetch_hero_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_hero_portrait_task(self, key, folder, hero_name, region):
    if key == "hero-portraits/": return
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
        cached_data = redis_client.get('hero_data')
        if cached_data is None:
            logger.warning("Hero data not found in cache.")
            return
        
        hero_data = json.loads(cached_data)
        hero = next((h for h in hero_data['data']['heroes']['nodes'] if h['slug'] == hero_name), None)

        if hero is None:
            logger.warning(f"Hero '{hero_name}' not found.")
            return

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

            # Detect the black bar width
            left_bar, right_bar = detect_black_bar_width(image_path)
            print(f"Detected black bar width: Left - {left_bar}px, Right - {right_bar}px")

            # After determining the bar widths, you can crop the image accordingly:
            img = Image.open(image_path)
            cropped_img = img.crop((left_bar, 0, img.width - right_bar, img.height))

            # Rotate the image if cropped width is longer than height
            if cropped_img.width > cropped_img.height:
                cropped_img = cropped_img.rotate(-90, expand=True)

            # Save the cropped image to separate BytesIO objects
            img_byte_arr_base64 = io.BytesIO()
            cropped_img.save(img_byte_arr_base64, format='JPEG')
            image_bytes = img_byte_arr_base64.getvalue()

            img_byte_arr = io.BytesIO()
            cropped_img.save(img_byte_arr, format='JPEG')
            img_byte_arr.seek(0)

            # Prepare and send the poll to Discord
            embed_data = {
                "title": f"Hero Portrait - {hero['title']}",
                "description": "I did my best!",
                "color": 3447003,  # Example blue color
                "fields": [
                    {"name": "Region", "value": region, "inline": True} if region else None,                        
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
                'filename': hero_name + '.jpg',
                'task_id': process_hero_portrait_task.request.id
            }
            redis_client.rpush('discord_message_queue', json.dumps(poll_data))
            logger.info(f"Sent poll to Discord for hero: {hero['title']}")
            
            # Wait for poll result (e.g., 120 seconds)
            result_key = f"discord_poll_result:{process_hero_portrait_task.request.id}"
            
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
                'image': (hero_name + '.jpg', img_byte_arr, 'image/jpeg')
            }
            payload = {
                'hero_id': str(hero['databaseId']),
                'region': str(region),
                'confirmed': '1' if upvotes > downvotes else '0'
            }

            # Log the data being sent
            logger.info(f"Sending data: {payload}")
            logger.info(f"Sending files: {files}")

            # Send the POST request
            try:
                update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/update-portrait'
                response = requests.post(update_url, files=files, data=payload)
                response.raise_for_status()
                logger.info("Hero portrait updated successfully")
            except requests.exceptions.HTTPError as e:
                logger.error(f"HTTP error occurred: {e}")
                logger.error(f"Response content: {response.text}")
                raise

            # Delete the image after processing
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