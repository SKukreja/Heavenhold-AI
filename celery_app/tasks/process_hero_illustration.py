import logging
import time
import json
import requests
import io
import os
import tempfile
import boto3
from celery import shared_task
from PIL import Image
from ..prompts.assistant_prompt import system_prompt
from ..prompts.hero_illustration_prompt import illustration_prompt
from ..utils import make_api_call_with_backoff, encode_image_to_base64, redis_client, boto3_config
from .fetch_hero_data import fetch_hero_data
from config import DISCORD_CHANNEL_ID, WORDPRESS_SITE, AWS_S3_BUCKET, OPENAI_API_KEY



logger = logging.getLogger(__name__)

@shared_task(bind=True)
def process_hero_illustration_task(self, key, folder, hero_name, region):
    if key == "hero-illustrations/": return
    global redis_client, AWS_S3_BUCKET, boto3_config, OPENAI_API_KEY
    attempt_count = int(redis_client.get('attempts:' + key) or 0)
    # Check the attempt count    
    if attempt_count >= 3:
        logger.info(f"Image {key} has reached maximum attempts. Deleting image.")
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
        redis_client.delete('attempts:' + key)
        redis_client.delete('lock:' + key)
        return
    logger.info(f"Processing image: {key} from folder '{folder}' as a hero illustration and thumbnail (attempt {attempt_count + 1})")   
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

        # Save image to a temporary location
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "image.png")
            with open(image_path, 'wb') as f:
                f.write(image_content)
            original_img = Image.open(image_path)

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
                            "text": illustration_prompt, 
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

            # Log the response JSON
            logger.info(cleaned_data)
            
            # Save the image to separate BytesIO objects
            img_byte_arr_base64 = io.BytesIO()
            original_img.save(img_byte_arr_base64, format='PNG')
            image_bytes = img_byte_arr_base64.getvalue()

            img_byte_arr = io.BytesIO()
            original_img.save(img_byte_arr, format='PNG')
            img_byte_arr.seek(0)
            
            # Attempt to parse the extracted data as JSON
            try:  
                crop_data = json.loads(cleaned_data)

                # Prepare and send the poll to Discord
                embed_data = {
                    "title": f"Hero Illustration - {hero['title']}",
                    "description": "Here's what you gave me:",
                    "color": 3447003,  # Example blue color
                    "fields": [
                        {"name": "Region", "value": region, "inline": True} if region else None,
                        {"name": "Crop Data", "value": f"x: {crop_data['x']}, y: {crop_data['y']}, width: {crop_data['width']}, height: {crop_data['height']}", "inline": False},
                    ],
                    "footer": {"text": "Does this look correct?"}
                }

                # Remove any None fields
                embed_data["fields"] = [field for field in embed_data["fields"] if field]

                # Convert the image to Base64
                base64_image = encode_image_to_base64(image_bytes)

                # Send poll request to Discord through Redis
                poll_data = {
                    'channel_id': DISCORD_CHANNEL_ID,
                    'is_embed': True,
                    'embed': embed_data,
                    'image': base64_image,
                    'filename': hero_name + '.png',
                    'task_id': process_hero_illustration_task.request.id
                }
                redis_client.rpush('discord_message_queue', json.dumps(poll_data))
                logger.info(f"Sent poll to Discord for hero: {hero['title']}")
                
                # Wait for poll result
                result_key = f"discord_poll_result:{process_hero_illustration_task.request.id}"
                
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
                    'image': (hero_name + '.png', img_byte_arr, 'image/png')
                }
                payload = {
                    'hero_id': str(hero['databaseId']),
                    'region': str(region),
                    'x': str(crop_data.get('x', 0)),
                    'y': str(crop_data.get('y', 0)),
                    'width': str(crop_data.get('width', 0)),
                    'height': str(crop_data.get('height', 0)),
                    'confirmed': '1' if upvotes > downvotes else '0'
                }

                # Log the data being sent
                logger.info(f"Sending data: {payload}")
                logger.info(f"Sending files: {files}")

                # Send the POST request with form-data
                try:
                    update_url = WORDPRESS_SITE + '/wp-json/heavenhold/v1/update-illustration'
                    response = requests.post(update_url, files=files, data=payload)
                    response.raise_for_status()
                    logger.info("Hero illustration/thumbnail updated successfully")
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